# UAT 角色与工作空间 HTTP 验收

日期：2026-09-23（Asia/Shanghai）
环境：`https://studio-uat.quicrobot.xyz`
本次验收对应部署输入 `8ec5569`。服务端健康接口没有返回 commit 字段，因此下面以 HTTP 健康结果和静态资源实际响应作为部署证据，不把版本字符串当成 commit 证明。

## 探测方法

所有请求通过 Python 标准库 HTTP 客户端完成，没有使用浏览器。登录后凭据只保存在进程内，没有写入报告、fixture 或日志；没有输出密码、会话 token、API Key 或 OSS 签名地址。除创建隔离 workspace、分配补采包、审核补采包和创建补采 batch 外，没有发送会改变既有工作项的写请求；没有调用训练接口。

部署探测结果：

- `GET /health`：`200`，`ready=true`，database、redis、celery、storage 均 ready。
- `GET /`：`200`。HTML 静态脚本包含 `app-bootstrap.js` 与 `mining-utils.js`，未静态包含 `demo-data.js` 或 `mining-console.js`。
- `GET /js/api.js?v=28`：`200`，包含 intake review draft 与 `/catalog-datasets` 客户端路径。
- `GET /js/app.js`：`200`，包含 package workbench 与秒级时长字段标记。

## 角色与资源结果

已有验收空间为 workspace=3，package=4/5，batch=2，annotation work item=2。专用隔离空间为 workspace=4，只创建者 admin user=5 是成员；没有把其他四个账号加入 workspace=4。工作空间 ID 同步写入 `/tmp/quicstudio-uat-fixture.json` 的非秘密字段，文件权限为 `0600`。

| 角色 | 工作空间选项 | package=4/5 | 目标工作台 | 资产与目录 | 结果 |
| --- | --- | --- | --- | --- | --- |
| admin user=5 | `GET /workspace/options` `200`，含 4、3、2、1 | 两包详情 `200` | 可读审核草稿；package=4 草稿版本=4 | `GET /data-assets?source_workspace_id=3` `200`；全局目录读取 `200` | 通过 |
| annotator user=7 | `200`，仅含 3 | 两包详情 `403 only an admin can manage collection resources` | item=2 标注列表和工作台 `200`；提交后 `status=submitted`、`submission_id=1`、`generation=2` | `dataset:read` 下资产接口 `200`；显式 source workspace=3 返回空列表 | 通过 |
| auditor user=8 | `200`，仅含 3 | 两包详情 `403 only an admin can manage collection resources` | item=2 审核列表和工作台 `200`，读取到 `submission_id=1`、`generation=2` | `dataset:read` 下资产接口 `200`；显式 source workspace=3 返回空列表 | 通过 |
| operator user=6 | 无有效 RBAC 角色，工作空间选项 `403` | `403` | `403` | 资产读取 `403 权限不足`；目录写入 `403 only an admin can manage catalog datasets` | 作为退役/未配置角色的负例通过 |
| viewer user=9 | 无有效 RBAC 角色，工作空间选项 `403` | `403` | 工作台读取 `403`；审核批准写入 `403` | 资产与目录读取 `403` | 作为退役/未配置角色的负例通过 |

admin 查询 `GET /auth/roles` 的实际结果只有 admin、annotator、auditor 三个有效角色。operator 和 viewer 账号保留为拒绝路径，不能据此宣称其拥有资产读取权限。目录数据集写入在当前契约下是 admin-only，operator 的 `403` 是预期结果。

`/data-assets` 是全局资产读取接口，按接口契约不会隐式按采集 workspace 裁剪。annotator/auditor 使用不带 workspace 过滤的请求时都能读到既有全局资产；传 `source_workspace_id=3` 时返回 `200` 空列表。这与工作项、数据包的成员隔离是两套明确范围，不能把资产全局读取误报为跨空间工作项泄露。

审核工作项在提交前，auditor 请求 `GET /review-work-items/2/workbench?workspace_id=3` 得到 `409 annotation_historical_result_uncertain`；root 完成 item=2 提交后同一请求返回 `200`。这证明未提交状态被等待处理，不能用未提交历史结果冒充可读审核内容。viewer 的工作台读取和 `POST /review-work-items/2/approve` 均为 `403`，没有改变 item=2 状态。

## 跨 workspace 隔离

- admin 以 `workspace_id=4` 请求既有 `package=4`，返回 `404 data package does not exist in this workspace`，响应没有返回 package UID 或 Episode 内容。
- annotator 以 `workspace_id=4` 请求 item=2 标注工作台，返回 `403 workspace access denied`；auditor 以同一空间请求 item=2 审核工作台，返回 `403 workspace access denied`。
- annotator/auditor 的 workspace 选项不包含 4。对 `data-assets?source_workspace_id=4` 的请求返回空列表，没有返回 workspace=3 的资源；这是全局资产接口的显式过滤结果。

## 补采 package=6 的真实 HTTP/CLI 闭环

本节只使用 package=6，原 voided package=5、package=4、batch=2 和 item=2 均未修改。

1. admin 将 package=6 分配给 collector=3，响应 `200`，状态从 `pending_assignment` 变为 `assigned`。package=6 的 `supplement_for_package_id=5`，workspace=3、collection task=3。
2. 使用现有 CLI 和已批准凭据运行两次 `--episode` 的同一包上传，默认 transport 为 `oss_multipart`。两个 Episode 各上传一个 part；会话最终 `succeeded`，package 进入 `pending_intake_review`。第一次异步查询时 Episode=6 暂时显示 `admission_fact_missing`，约 10 秒后的重读为 `passed`、`preview_available=true`；Episode=7 一直为 `passed`、`preview_available=true`。等待两个都稳定通过后才进行审核。
3. admin 保存 package=6 审核草稿：`base_version=0` 成功推进到 `draft_version=1`，并携带服务端来源指纹。随后以 `verdict=approved`、`rejected_episode_ids=[6]` 和 Episode=6 的原因完成终审，响应 `200`、review=6、包状态 `intake_approved`、`accepted_episode_ids=[7]`、`rejected_episode_ids=[6]`。
4. admin 创建 batch=3，唯一包为 package=6，开启标注配置并指定 annotator=7、reviewer=8。服务端详情明确 `episode_count=1`、`episode_ids=[7]`、`data_package_ids=[6]`；batch 进入 `annotating`，package=6 为 `batched`。
5. API 读取新工作项：annotator 的 item=3 为 `assigned`，`episode_members` 只有 Episode=7；auditor 的 review item=3 为 `assigned`，同样只有 Episode=7。没有对新工作项进行标注、提交或审核，留给后续验收。

上述补采 ID（package=6、Episode=6/7、review=6、batch=3、item=3、workspace=4）已写入 `/tmp/quicstudio-uat-fixture.json` 的 `supplement_uat` 字段；不包含凭据或签名 URL。

## 补采 batch=3 的无有效片段终态

继续使用 package=6 已接受的 Episode=7 完成真实工作台 API 链路，未请求或修改 item=2：

1. annotator=7 读取 item=3 时得到 `status=assigned`、`draft_version=0`、`generation=1`，Episode=7 是唯一冻结成员，来源映射可读。
2. annotator 以 `base_version=0`、`expected_generation=1` 保存 `conclusion=no_valid_segments`、空 `segments` 和原因「UAT 验收：确认无可用于训练的有效片段。」；响应为 `status=in_progress`、`draft_version=1`。随后以 `base_version=1`、`expected_generation=1` 提交，响应为 `status=submitted`、`generation=2`、`submission_id=3`。
3. auditor=8 读取固定 submission=3（`draft_version=1`、`generation=1`），以预期 submission=3、generation=2 执行批准，响应为 `review status=approved`、review generation=3；标注工作项读取为 `status=done`，其 Episode=7 结论仍是 `no_valid_segments`、片段数为 0。
4. batch=3 随后进入终态 `no_publishable_asset`，`valid_duration_hours=0.00`，快照记录 `no_valid_episode_ids=[7]`、`effective_duration_ns=0` 和 submission=3。`GET /data-assets` 按 batch=3 查询没有匹配资产，证明全无有效片段时保留完成记录而不创建伪造资产或范围。
5. package=6 保持 `batched`；package 级入库事实仍为 Episode=6 rejected、Episode=7 accepted，原 package=5、item=2 和 batch=2 未改变。

上述 item=3 的 submission、generation、终态和零资产计数已追加到 `/tmp/quicstudio-uat-fixture.json` 的 `supplement_uat` 字段；报告与 fixture 均不记录凭据或签名地址。
