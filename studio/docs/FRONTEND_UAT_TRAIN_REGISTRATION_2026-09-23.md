# 训练前登记与验收清理

本轮主链终点是数据沉淀、不可变数据集版本和真实导出。训练边界验证只检查登记、读取和不满足条件时的阻断，不提交训练作业。

## 发现与修复

- 嵌入训练控制面需要 Python 3.11，UAT API 原运行时为 3.10。独立 API 环境已修复，运行配置与回滚见 [运行记录](UAT_TRAIN_CONTROL_PLANE_PY311_2026-09-23.md)。API 使用独立 UAT 项目 `prj_other`；训练 provider 提交禁用，scheduler 未启动。
- 启动种子曾自动写入四个示例数据集。修复后，示例必须在 development/test 环境显式开启，正式环境默认不注入；真实模型配方与资源配置仍保留。
- 旧训练登记只传名称和 URI，却默认生成 `1 episode / 1 frame / 30 fps / laptop camera / 7 维动作与状态`，并标记 READY。修复后，手动仅登记地址为 REGISTERED，未验证字段为未知；Catalog 导出从归档内实际 `meta/info.json` 固定元数据和校验值。
- `.tar.gz` 导出归档不是已展开、可调度的数据集目录。登记不自动标记 READY，也不以复制归档文件冒充成功物化。当前不包含归档解包物化功能。
- 本次 iPhone EGO 样本具有视频和语言描述，没有机器人 action / observation.state。训练兼容性校验应如实阻断需要这些字段的模型。

## 实际样本预期

数据集 `2` / 版本 `2` 来自审核通过的 submission `2`、资产 `2`。有效区间总长 `19399389184 ns`。LeRobot 按 10 fps 导出，包含 2 个 Episode、194 帧、两条语言描述，相机键为 `observation.images.head`，action_dim 与 state_dim 均为 0；没有 robot_type 时保持未知。

旧导出 `5` 已通过内容验收，但不带新增的、绑定归档校验值的持久元数据。使用部署后的新导出进行登记，不手写旧导出结果补证据。

## 部署后证据

- `ef74e67` 部署后重启独立 UAT API / export worker；`f09921f` 修正“重新导出”复用旧幂等 key 的问题，网络未确认时保留同 key 重发，确认成功后的新动作创建新任务。`cc74255` 将预检失败原因放在当前步骤直接显示。
- 实际浏览器点击重新导出，产生 export `6` / attempt `3`，状态 queued → succeeded。旧成功 export `5` 及失败 attempt 均保留，数据集 version `2` 不变。
- 新归档共 8 个文件、1,022,822 字节，SHA-256 `98d3eead82a717a6c37d5cfda16248757f12b855b01e0285be939b6351dc8712`。下载后校验归档哈希、LeRobot SDK、194 个视频帧、parquet、两段范围与语言描述均通过。默认交付内网、TTL 3600 秒，显式公网下载 HTTP 200。
- 浏览器点击“登记到训练”后实际进入目录，版本 `v1-export-6`，训练数据版本 ID `dsv_d812c0a85701b958e1e1`，状态“已登记 · 尚未就绪”。重复调用返回同 ID、`created=false`，没有重复目录记录。真实元数据逐项符合上文预期，robot_type 为 null。
- 旧 export `5` 的登记请求明确 HTTP 409，提示重新导出以取得验证元数据；没有悄悄填入默认值。
- `jobs/validate` 对 ACT 返回 `valid=false`，含 DATASET_NOT_READY 及 action_dim/state_dim 的 POLICY_FEATURE_MISSING。浏览器向导停留在资源步骤，直接显示这些原因，没有进入提交步骤。最终 jobs=0、attempts=0、materialization_attempts=0；provider_disabled=true。
- 训练目录中文和英文随全站语言切换，项目范围显示 `prj_other`，可选元数据折叠；窄窗口的 OSS 地址在卡片内换行。
- 四个旧示例 seed 与修复前源代码的完整 manifest、URI、校验值、版本及原始创建时间逐项一致。先备份，再在加锁事务中重验记录未变且 jobs/attempts/materialization/audit 无引用，精确删除这四行。重启后未重建，其他项目、模型与资源配置未删除。
- 本批本地回归：前端全套 359 项；导出及恢复 15 项；训练 123 项。训练套件未覆盖本机缺少 DLC SDK 的 provider contract；另排除了三项已有独立 ECS 部署 fixture 不一致，不能把本次验证等同于真实云端训练验收。

## 临时身份与密钥

已撤销上传 Key `2`，其 revoked_at 已写入；由既有非测试管理员 actor `1` 停用 `fe-uat-20260922` 下五个身份（用户 `5–9`），会话 epoch 均推进至 `1`，撤销 53 个刷新会话，无待处理会话清理、无取消中的作业。

五个身份分别使用原密码登录、原 access token 调用 `/auth/me`，均 HTTP 401；原 Key 读取实际存在的数据集版本接口也 HTTP 401。浏览器刷新后回到登录页。业务数据包、审核、资产、版本和导出保留，未修改原始样本目录。

本机与 ECS 的临时密码、Key 明文文件，以及本机用于失效校验的 JWT 文件已删除。精确 seed 备份与无凭据的验收结果留档。
