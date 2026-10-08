# 采集上传接口（Plan 3）

> 更新：2026-09-18。本文描述当前实现，不代表三桶改造已全部验收。新的客户端应使用逐来源 `oss_multipart`；服务端接收字节的 SDK/chunked 路径尚保留兼容，计划移除。MinIO provider 已存在，但采集上传调用者尚未全部切换到统一 provider，不能把配置 MinIO 等同于已打通整条上传链路。

所有接口以 `/api/v1` 为前缀，使用管理员身份，并验证采集工作空间成员关系。
采集包必须已经分配，不能由上传会话自动创建；归档项目不接受新上传会话。

## SDK / 分片上传（遗留兼容链路）

1. `POST /upload-sessions`：提交 `workspace_id`、`collection_project_id`、
   `package_uids`、`upload_mode`（`duance_sdk` 或 `chunked`）。
2. `POST /upload-sessions/{id}/declarations`：提交 `workspace_id`、`items`。
   每项包括 `package_uid`、`source`、`metadata_text`、`data_file`。
   必须声明会话的全部数据包，一个包可以包含多个 Episode。
3. 响应 `data.sources` 返回每个文件的 `source_id`、`package_uid`、`episode_id`。
   `source_id` 由服务端生成；客户端不能提交 staging 路径、桶或对象键。
4. 对每个文件调用 `POST /upload-sessions/{id}/chunked/init`，提交
   `workspace_id`、`source_id`、`total_chunks`。响应同时返回 `uploaded_chunks`（块数）与 `uploaded_chunk_indices`（已上传的零基编号数组），恢复时必须按编号补传，不能用块数推断连续区间。
5. `PUT /upload-sessions/{id}/chunked/{chunk_index}?source_id={source_id}`：
   请求体是该文件的原始 MCAP 字节分片，索引从 0 开始。允许重传相同分片。
   每个文件的分片计数与大小分别校验，不能超过声明大小及服务端限额。
6. 全部文件上传后调用 `POST /upload-sessions/{id}/chunked/complete`，
   请求体仅含 `workspace_id`。服务端合并每个文件并核对大小、SHA-256；
   任一文件缺失或校验失败，会话不会进入解析，可补传后重新完成。
7. 完成后生成持久化 `collection_upload_parse` 作业，由 ingest worker 执行。
   `GET /upload-sessions/{id}?workspace_id=...` 查询最终状态。
   成功后数据包进入 `pending_intake_review`，不会自动触发 QC 或标注。
   此状态不保证可立即审核：提交审核还会检查完整性和 Preview。

`source` 字段：`episode_id`、`start_ns`、`end_ns`、`metadata_sha256`、`data_mcap_sha256`。
`data_file` 字段：`path`（QRDF 中的相对路径）、`size_bytes`、`sha256`。
时间戳按纳秒十进制字符串传输。`metadata_text` 必须保留计算哈希时的原始文本。
同一包内不同 Episode 可以使用相同相对路径（例如都为 `data.mcap`），服务端以来源身份隔离落盘；重复来源声明仍会被拒绝。多文件**不是 ZIP**。旧会话恢复沿用原有落盘布局。

为兼容已有单文件分片调用，仅声明一个来源时可省略 `source_id`；
同一会话不能混用单文件旧布局和逐文件布局。完成上传后不允许重新初始化或改写字节。

## OSS 直传的范围

`oss_multipart` 使用现有 `/oss/init`、`/oss/sign-part`、`/oss/complete`。
创建会话时使用 `upload_mode=oss_multipart`，先声明全部来源，再针对每个 `source_id`
分别初始化、签名并完成 multipart。多来源调用必须传 `source_id`；单来源可省略。
每个来源的 `total_size_bytes` 必须等于声明大小；仅全部来源完成后才调度解析。
OSS 大小必须与来源声明一致；完成时校验服务端查询的分片清单和大小，解析时再校验 SHA-256。
OSS provider 由测试桩覆盖，尚未实连云上验收。

不提供 OSS 扫描；模型中的 `authorized_import` 仅为扩展预留，创建接口暂不接受该模式。
签名 URL 是短期上传授权，不单独返回原始存储定位字段或 provider upload id。

preview 文件也走这三个接口，用 file_id 代替 source_id，见下文“客户端预检（client_admission）”。

## 客户端预检（client_admission）

Duance 可以在本地完成 QRDF 结构与媒体校验、生成 preview，然后随声明提交 `client_admission`，
把 preview 文件直传 process 桶。Studio 在客户端预检模式下**不下载 MCAP**：只 HEAD 对象、
按服务端自己的规则重新判定 issue、下载并验证 preview 小文件。无法使用客户端结果时自动改走
完整服务端链路。**不带 `client_admission` 的声明与之前完全一致。**

### 1. 能力协商

`GET /upload-sessions/capabilities?workspace_id={workspace_id}`（`episode:read` 权限与工作空间成员关系）。
响应 `data`（外层仍是 `{"code": 200, "message": "Success", "data": ...}`）：

```json capabilities-response
{
  "client_admission": {
    "enabled": true,
    "accepted_qrdf_versions": ["0.2.1"],
    "accepted_policy_versions": ["v1"],
    "non_blocking_issue_codes": ["NO_ACTION_TOPIC", "NO_STATE_TOPIC", "NO_VALID_EGO_FRAMES", "NO_VALID_TRAINING_FRAMES"]
  }
}
```

- `enabled=false`（部署配置 `ACCEPT_CLIENT_ADMISSION=false`）：客户端不做本地预检，声明不带 `client_admission`。
- `accepted_qrdf_versions` 是 qrdf **SDK 包版本**（`qrdf.__version__`），不是 metadata 里的数据格式版本 `qrdf_version`（`0.2.0`）。
  客户端 SDK 版本不在列表中时走旧链路。
- `accepted_policy_versions`：客户端所用校验策略版本，当前只有 `v1`。
- `non_blocking_issue_codes` 是阻断判定的唯一依据：任一 issue `severity == "ERROR"` 且 `code` 不在表中，
  该 episode 有阻断错误。客户端不附 `client_admission`、不上传 preview，仍按旧路径上传该 episode 的原始数据；
  同包其余 episode 照常上传。服务端用同一张表重新判定，从不采用客户端的 `ok`。
  integrity 失败的 episode 在入库审核时自动排除（`integrity_failed`）。
- 旧版 Studio 没有此路由，`GET /upload-sessions/capabilities` 会被当作会话查询返回 404；客户端把 404 视为不支持。

### 2. 声明

`POST /upload-sessions/{id}/declarations` 的 `items[]` 每项可带可选字段 `client_admission`：

```json declaration-item
{
  "package_uid": "PKG-20260929-0001",
  "source": {
    "episode_id": "episode_000001",
    "start_ns": "1759111200000000000",
    "end_ns": "1759111800000000000",
    "metadata_sha256": "45447b7afbd5e544f7d0f1df0fccd26014d9850130abd3f020b89ff96b82079f",
    "data_mcap_sha256": "d3690131c737f212505f230d86ea322e788db598fd0162f5500f7e22ab7005bc"
  },
  "metadata_text": "{\"qrdf_version\": \"0.2.0\", \"episode_id\": \"episode_000001\", \"data_file\": \"data.mcap\"}",
  "data_file": {
    "path": "data.mcap",
    "size_bytes": 3188624643,
    "sha256": "d3690131c737f212505f230d86ea322e788db598fd0162f5500f7e22ab7005bc"
  },
  "client_admission": {
    "qrdf_version": "0.2.1",
    "policy_version": "v1",
    "report": {
      "ok": true,
      "issues": [
        {"severity": "ERROR", "code": "NO_ACTION_TOPIC", "message": "no action topic registered", "path": null, "topic": null}
      ],
      "media_validation": {"ok": true, "issues": []}
    },
    "files": [
      {"path": "media/preview/manifest.json", "size_bytes": 812, "sha256": "05b3abf2579a5eb66403cd78be557fd860633a1fe2103c7642030defe32c657f"},
      {"path": "media/preview/generations/3c1f0e9a2b7d4c55/head_rgb.mp4", "size_bytes": 154235611, "sha256": "862c4ec62defaadafbf7638961214d286015c38ed4ccc7b10d52bdf434e5bee1"},
      {"path": "media/preview/generations/3c1f0e9a2b7d4c55/head_rgb.timeline.json", "size_bytes": 902113, "sha256": "94d192b3a326be1f019b71ef13ea5a367ffe939c5e9a88f1b270e53753d9569a"}
    ]
  }
}
```

| 字段 | 规则 |
| --- | --- |
| `qrdf_version` | 客户端 qrdf SDK 版本，1–32 字符 |
| `policy_version` | 校验策略版本，1–32 字符 |
| `report.ok` | 必填布尔；只记录，不参与判定 |
| `report.issues[]` | 最多 1000 项；每项必填 `severity`（`ERROR`/`WARNING`/`INFO`）与 `code`（1–128 字符），可选 `message`（最长 4096）、`path`（最长 1024）、`topic`（最长 512），均可为 `null`；允许其他字段 |
| `report.media_validation` | 必填，对象或 `null`（媒体校验报告原样） |
| `report` 整体 | JSON 序列化后不超过 1 MiB；允许其他字段，原样保存 |
| `files[]` | 最多 256 项；非 RGB episode 可为空数组；非空时必须包含 `media/preview/manifest.json` |
| `files[].path` | 最长 512 字符；episode 目录内相对路径，必须以 `media/preview/` 开头；按 `/` 分段，每段匹配 `[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}`；同一来源内不能重复 |
| `files[].size_bytes` | 正整数，必须等于上传字节数 |
| `files[].sha256` | 64 位小写十六进制 |

`client_admission` 只允许在 `upload_mode=oss_multipart` 的会话中使用。

响应 `data`：

```json declaration-response
{
  "id": "0b7c6f5e-1a2b-4c3d-8e9f-001122334455",
  "status": "init",
  "declared_packages": 1,
  "declared_sources": 1,
  "sources": [
    {
      "source_id": "5d41402abc4b2a76b9719d911017c5925d41402abc4b2a76b9719d911017c592",
      "package_uid": "PKG-20260929-0001",
      "episode_id": "episode_000001",
      "preview_files": [
        {"file_id": "cb12586399787bab0f86ec8cdf8a091e1d44da02d99379c9c95746d9825f7886", "path": "media/preview/generations/3c1f0e9a2b7d4c55/head_rgb.mp4", "size_bytes": 154235611},
        {"file_id": "02ce403b8fd16d8937b6141809d361a2c7389ca3ef1e64737a11b2f56b925e58", "path": "media/preview/generations/3c1f0e9a2b7d4c55/head_rgb.timeline.json", "size_bytes": 902113},
        {"file_id": "fb5994d098e7489a7b11c8a5498139f66c4fa4dd3d174eaa8c40710194f99c1f", "path": "media/preview/manifest.json", "size_bytes": 812}
      ]
    }
  ]
}
```

- 声明响应中的 `episode_id` 是声明的 QRDF episode 标识（字符串，如 `episode_000001`）；
  第 5 节 `source_admission.sources[].episode_id` 是 Studio 内部整数 id。二者不是同一个值，客户端必须用 `source_id` 关联。
- `preview_files` 只出现在声明了 `client_admission` 的来源上（`files` 为空时是空数组），按 `path` 升序；未声明时响应中没有该键。
- `file_id = sha256("{source_id}:{path}")` 的小写十六进制，由服务端生成。客户端必须使用响应中的值，不要自行计算。
- `source_id` 与之前相同，仍用于上传该来源的 MCAP；每个 preview 文件用自己的 `file_id` 上传。
- 完全相同的声明可以重放，返回相同的 `source_id` 与 `file_id`。内容（含 `client_admission`）有任何变化返回 409
  `package_declarations_conflict`，需要取消会话后新建。

### 3. 声明时拒绝与 worker 回退

声明时拒绝（整批声明不生效，可修正后重发）：

| 情况 | HTTP | `detail` |
| --- | --- | --- |
| 缺字段、类型错误、未知字段（`client_admission`、`files[]` 不允许多余字段）、`sha256` 格式错误、`size_bytes <= 0`、`severity` 非法、issue 缺 `code`、超出数量上限 | 422 | FastAPI 校验错误数组 |
| 会话不是 `oss_multipart` | 422 | `client_admission_requires_oss_multipart` |
| `files[].path` 含 `\`、以 `/` 开头、有空段、`.`、`..` 或不合规字符 | 422 | `client_admission_path_unsafe` |
| 路径合法但不在 `media/preview/` 之下 | 422 | `client_admission_path_outside_preview` |
| 同一来源 `path` 重复 | 422 | `client_admission_path_duplicate` |
| `files` 非空但缺 `media/preview/manifest.json` | 422 | `client_admission_manifest_missing` |
| `report` 超过 1 MiB | 422 | `client_admission_report_too_large` |

以下情况声明照常接受，admission worker 回退完整服务端模式（下载 MCAP、校验、重新生成 preview），
原因写入 `client_admission_fallback`：

| `client_admission_fallback` | 含义 |
| --- | --- |
| `client_admission_disabled` | 处理时服务端已关闭客户端预检 |
| `client_qrdf_version_not_accepted` | `qrdf_version` 不在处理时的白名单 |
| `client_policy_version_not_accepted` | `policy_version` 不在处理时的白名单 |
| `client_report_invalid` | 保存的报告无法解析（防御性检查） |
| `client_source_object_mismatch` | raw MCAP 对象 HEAD 与上传完成时的身份不一致 |
| `client_preview_missing` | metadata 声明了 RGB 流，但 `files` 为空 |
| `client_preview_object_missing` | preview 对象不存在或无法下载 |
| `client_preview_object_mismatch` | preview 对象大小或 etag 与声明不符 |
| `client_preview_hash_mismatch` | 下载的 preview 字节 SHA-256 与声明不符 |
| `client_preview_invalid` | preview 未通过服务端验证（指纹与声明的 `data_mcap_sha256`/`metadata_sha256` 不符、帧数、faststart、H.264/yuv420p 等）；细节在报告 `client_admission_fallback_detail.error_code`（如 `PREVIEW_MEDIA_STALE`） |
| `client_preview_files_mismatch` | 声明的文件集合与 `manifest.json` 引用的文件集合不相等 |

服务端重新判定为 `integrity_status=failed`（例如客户端 `ok=true` 但含 `MCAP_UNREADABLE`）时**不回退**，
直接写 failed 事实，`integrity_source="client"`，`error_code` 为第一个阻断 code。

### 4. preview 上传

- 仍用 `/oss/init`、`/oss/sign-part`、`/oss/complete`，请求体用 `file_id` 代替 `source_id`；两者同时出现返回 422。
  `file_id` 不属于本会话返回 422 `file_id does not belong to this upload session`。
- `oss/init` 的 `total_size_bytes` 必须等于声明的 `size_bytes`，否则 422。响应与 MCAP 相同
  （`total_size_bytes`、`part_size_bytes`、`total_parts`）。用同一 `file_id` 重复 init 返回相同参数，用于断点续传。
- `oss/sign-part` 新增 `content_md5`：该分片字节 MD5 的标准 Base64（24 个字符，以 `==` 结尾）。
  preview 分片必填；声明了 `client_admission` 的来源，其 MCAP 分片同样必填，缺失返回 422 `content_md5_required`；
  仅遗留声明（未带 `client_admission`）的 MCAP 分片可选，Duance 应一律携带。
  服务端把它纳入签名，响应 `headers` 中包含 `Content-MD5`；客户端必须原样发送响应 `headers` 中的全部头。
  字节损坏时存储端拒收该分片（HTTP 400），客户端重新签名并重传该分片。
- development 环境下，分片签名 URL 可以指向进程已配置的 loopback 端点：主机为 `127.0.0.1`、`localhost` 或 `::1`，并且 scheme 与端口必须和 `storage_browser_endpoint`（否则 `oss_browser_endpoint`，否则 `storage_endpoint`）完全一致。同一签名函数同时用于采集上传和浏览器导入（`import_intake.sign_direct_multipart_part`）。production 与 test mode 仍只接受公网 HTTPS；局域网地址或其他主机一律拒绝。

```json sign-part-request
{"workspace_id": 7, "file_id": "cb12586399787bab0f86ec8cdf8a091e1d44da02d99379c9c95746d9825f7886", "part_number": 1, "content_md5": "1B2M2Y8AsgTpgAmY7PhCfg=="}
```

```json sign-part-response
{
  "id": "0b7c6f5e-1a2b-4c3d-8e9f-001122334455",
  "status": "uploading",
  "part_number": 1,
  "content_length": 67108864,
  "method": "PUT",
  "url": "https://quicstudio-process.oss-cn-beijing.aliyuncs.com/...",
  "expires_in": 900,
  "headers": {"Content-Type": "application/octet-stream", "Content-MD5": "1B2M2Y8AsgTpgAmY7PhCfg=="}
}
```

- `oss/complete` 与 MCAP 相同，提交全部 `{part_number, etag}`。
- 会话只有在**全部 MCAP 与全部 preview 文件**都 complete 后才进入 `uploaded` 并触发解析；此前 complete 响应的 `status` 为 `uploading`。
- 对象位置由服务端决定、不返回给客户端：process 桶
  `process/v2/workspaces/{workspace_id}/collection-uploads/{upload_session_id}/{32 位随机十六进制}/{path 的文件名}`。

### 5. 观察结果

解析与 admission 异步执行。客户端轮询 `GET /data-packages/{data_package_id}?workspace_id=...`：

- `qrdf_facts.source_admission.sources[]`，按声明响应中的 `source_id` 对应：

```json package-source-admission
[
  {"source_id": "5d41402abc4b2a76b9719d911017c5925d41402abc4b2a76b9719d911017c592", "episode_id": 101, "status": "ready", "attempt": 1, "error_code": "", "integrity_source": "client"},
  {"source_id": "0b6f3e2c9a4d8e17f5c2b0a9d8e7f6a50b6f3e2c9a4d8e17f5c2b0a9d8e7f6a5", "episode_id": 102, "status": "ready", "attempt": 1, "error_code": "", "integrity_source": "server", "client_admission_fallback": "client_preview_invalid"},
  {"source_id": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "episode_id": 103, "status": "failed", "attempt": 1, "error_code": "MCAP_UNREADABLE", "integrity_source": "client"}
]
```

  - `status`：`queued` / `running` / `ready` / `failed`；`integrity_source` 在 worker 结束后出现（`client` 或 `server`）。
  - `client_admission_fallback` 只在回退时出现，取值见第 3 节。
- `episodes[]` 每项新增 `integrity_source`（当前 admission 事实的来源；尚无事实时为 `null`）。
- admission 报告（`admission-report.json`，即事实的 `report_ref`）在客户端模式下含 `client_admission`
  （客户端原始报告与服务端判定 `judgement`），回退时含 `client_admission_fallback` 及可选的 `client_admission_fallback_detail`。

## 取消与审核

- `POST /upload-sessions/{id}/cancel`：恢复仍被该会话占用的数据包到进入会话前的
  `assigned` / `parse_failed` 状态，允许新建会话重传。已经作废的包不会被恢复。
  正在解析或终态会话不可取消。
- 入库审核提交只要求每个 episode 的 admission 事实 `integrity_status=passed`（自 `31b0be9` 起）；
  preview 与输出校验在建批时检查。admission 事实以对象清单 `objects_json` 描述 episode 的全部存储对象
  （raw 的 `data` 对象、process 的 `metadata.json`、`admission-report.json` 与 `media/preview/` 下的
  preview 文件），不再生成 process tar。`data` 对象的 `path` 就是声明上传时 `data_file` 的相对路径
  （通常为 `data.mcap`）。
- 逐包审核：`POST /data-packages/{id}/intake-review`，包含 `workspace_id`、
  `verdict`（`approved` / `rejected`）、`rejected_episode_ids`（可选）、`reason`。
  整包拒绝必须填写原因。
- 批量通过：`POST /data-packages/intake-review/bulk-approve`，包含
  `workspace_id` 和 `data_package_ids`。任一包状态不符合则整批拒绝，不部分提交。

## 升级说明

执行迁移 `0009_drop_admission_process_ref` 后，历史 admission 事实（迁移前生成、仍以
process tar 描述产物）的 `objects_json` 为空；在对应 episode 重新入库（re-admitted）之前，
预览、发布、标注取数与导出均会按对象清单缺失而 fail closed。引用旧 `process.tar` 的既有
快照与目录版本（catalog version）也不能直接复用，必须重新冻结（re-frozen）成对象清单形式。

真实字节链路回归见 `backend/tests/test_collection_upload_bytes.py`；它通过 HTTP 上传
有效 MCAP 文件并调用真实解析 service，再通过 HTTP 入库审核，不使用 `parse_fixture_mode`。
Celery 作业派发、SDK 客户端适配、云 OSS 和预览播放仍需联调验收。

命令行客户端使用方法见 [上传 CLI](STUDIO_UPLOAD_CLI.md)。当前 CLI 采用 chunked 兼容接口，OSS 直传 CLI 尚未实现。
