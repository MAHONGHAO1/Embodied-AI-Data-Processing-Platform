# QRDF Episode 上传 CLI

工具：`scripts/studio_upload.py`。Python 3.10+，macOS/Linux，仅标准库。前端不提供文件上传入口。

CLI 把一个或多个原始 Episode 上传到同一个已经分配的数据包。默认使用 `oss_multipart`：metadata 和 MCAP 先声明到 Studio，再由 Studio 为每个来源签发短期 HTTPS 分片 URL，MCAP 字节直接进入对象存储。`chunked` 是兼容传输方式，必须显式选择。多 Episode 不是 ZIP；每个目录的 `metadata.json` 可以都指向 `data.mcap`，服务端按来源 ID 分开保存。

## 使用

1. 在 Studio 创建采集任务并分配数采员，取得工作空间 ID、采集项目 ID 和包 UID。
2. 在 API 凭据页面签发 `qs_` Key。上传服务要求 Key 所属用户是管理员且属于该工作空间。不要使用浏览器会话 token。
3. 将 Key 放入权限为 `0600` 的本地文件，或者设置 `STUDIO_API_KEY` 环境变量。不要把 Key 写到命令行参数、代码仓库或共享日志中。
4. 保留每个原始 Episode 的 `metadata.json` 和 metadata 指向的 MCAP。运行：

```sh
python3 scripts/studio_upload.py \
  --url https://studio-uat.quicrobot.xyz \
  --key-file /private/path/studio-key \
  --workspace 3 --project 2 \
  --package pkg_your_assigned_package \
  --episode /path/to/episode_001 \
  --episode /path/to/episode_002 \
  --state /path/to/upload-state/package.json
```

`--episode` 可以重复；所有目录必须属于同一个包。Episode ID 需要各不相同。示例 ID 必须替换为目标任务的真实 ID。状态文件放在所有原始数据目录以外。脚本不会修改原始文件；metadata 按原始 UTF-8 字节计算哈希，纳秒时间戳作为十进制字符串传输。

默认 OSS 直传使用服务端返回的 part 大小。`--wait-seconds 0` 只等待上传会话接受字节；默认最多等待解析调度 1800 秒。进度写 stderr，最终会话 JSON 写 stdout。

显式使用旧的 API 接收字节链路时：

```sh
python3 scripts/studio_upload.py \
  --transport chunked --chunk-mib 8 \
  --url https://studio-uat.quicrobot.xyz \
  --key-file /private/path/studio-key \
  --workspace 3 --project 2 --package pkg_your_assigned_package \
  --episode /path/to/episode_001 \
  --episode /path/to/episode_002 \
  --state /path/to/upload-state/package.json
```

## 凭据与字节路径

Studio API 请求使用 `Authorization: Bearer qs_...`。API Key 只发给 Studio HTTPS origin，不会添加到 OSS 请求。OSS part PUT 只使用服务端返回的 HTTPS 签名 URL 和签名 headers；脚本拒绝带用户名、密码、非 HTTPS 或 credential-bearing headers 的 URL，并且禁用重定向。签名 URL 不写入状态文件或进度日志。

读取 MCAP 时脚本使用文件流和服务端声明的分片大小，内存中只保留当前分片。上传前检查 `metadata.data_file` 位于 Episode 目录内、文件非空，并计算 metadata 和 MCAP SHA-256。服务端在 OSS 完成时核对 provider 的分片清单和大小，解析 worker 仍会核对来源 SHA-256。

## 中断恢复

再次运行完全相同的命令，使用原状态文件。状态身份绑定 Studio origin、工作空间、项目、包、传输方式、Episode source 声明和 MCAP/metadata 哈希；文件内容或参数改变会拒绝复用旧状态。

每个 Episode 在状态文件中独立保存服务端 `source_id`、part 大小、part 数量以及已经成功收到的 ETag。OSS 接口没有给 CLI 提供 list-parts 查询；因此恢复时只跳过状态文件中已经确认成功的 part，缺少或不确定的 part 按原 part number 重新 PUT。相同 multipart upload 的同编号 part 可安全覆盖，随后仍把完整 ETag 清单提交给 `/oss/complete`，由服务端再次向 provider 查询并核对。状态文件丢失后，仍可用 `--session-id` 对活动会话重建声明并安全重传全部未完成 part；如果会话处于 provider 已完成但 Studio 状态尚未落库的极端模糊状态，必须保留原状态文件中的 ETag 清单才能完成恢复，不能凭序号猜测 provider 清单。

`chunked` 恢复始终以服务端 `/chunked/init` 返回的 `uploaded_chunk_indices` 为准，不能把已上传块数当成连续前缀。服务端未返回编号时，脚本会安全重传全部分片。

同一状态文件有本地进程锁，避免并发创建会话。不要删除仍在上传的状态文件，也不要同时用多个状态文件上传同一个包。创建会话请求的响应丢失时，状态会保留 `creating` 标记且不会盲目重试；查询 `GET /api/v1/upload-sessions?workspace_id=...` 确认会话后，用 `--session-id` 指定它恢复。完成请求不自动重放：重新运行先查询会话状态，已上传或已解析的会话不会重新创建或重复上传。

已完成、失败或取消的会话不会再次上传；失败返回非零退出码。文件在上传中发生修改时不提交完成。会话 `succeeded` 只表示解析调度完成，不表示完整性检查、预览生成或入库审核通过；后续在数采审核页面查看逐 Episode 准入结果，任何审核操作仍需人工执行。

## 验证

```sh
python3 -m unittest discover -s scripts/tests -p test_studio_upload.py -v
```

测试覆盖同包多 Episode（包括相同 `data.mcap` 相对路径）、每个来源独立恢复、OSS part ETag 和大小清单、签名 URL 凭据隔离、HTTPS/重定向门禁、chunked 兼容链路、源文件哈希、创建结果不明和完成结果不明的恢复。

UAT 实采验证记录见 [连续验收记录](FRONTEND_UAT_EXECUTION_2026-09-22.md)。测试数据不会改变原始采集目录。
