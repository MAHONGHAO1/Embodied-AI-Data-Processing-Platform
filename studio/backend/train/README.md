# train

QuicTrain `release_V1.0` 控制面。与 `backend/data/` 平行，通过目录数据集导出件交互，不 `from data.services` 引用 Data 实现。

Python 包在 `src/` 下，保持 `quictrain_*` 原名。迁移、模型配方和 LeRobot 运行时配方仍在本目录，不并入 Data 的 Alembic。

- 对外路径：`/api/train/api/v1/...`（避免和 Data 的 `/api/v1/jobs`、`/api/v1/datasets` 冲突）
- 认证：Studio 访问令牌换成本控制面的项目会话。当前仅管理员（`*`）与数据运维（`train:*`）可进入训练控制面；登记与提交任务同权。
- 数据库：`QUICTRAIN_DATABASE_URL`，默认 SQLite。生产使用独立 PostgreSQL，迁移命令在本目录执行 `alembic upgrade head`
- 调度：API 进程内嵌调度默认开启。独立进程：`PYTHONPATH=src python -m quictrain_scheduler.main`，并设置 `QUICTRAIN_EMBEDDED_SCHEDULER=0`
- 启动仅同步模型与资源配方，不创建示例数据集。仅本地开发或测试可同时设置 `QUICTRAIN_ENV=development`（或 `test`）与 `QUICTRAIN_SEED_EXAMPLE_DATASETS=1`，显式载入未验证的演示目录；已有记录不会被启动过程删除。
- 运行时需要 Python `>=3.11,<3.14`。当前机器若仍是 3.10，Data 可启动，训练路由不会挂载

Catalog Export is a durable worker job. After Data reports a succeeded QRDF or LeRobot attempt, Train receives the immutable `oss_uri`, size, SHA-256 and manifest summary and reads the object with its own read-only OSS client. Data does not proxy downloads, return AK/SK, or issue browser signed URLs. Failed attempts remain recorded and retries use a new attempt; Train must use the URI from the frozen dataset-version export response.

原生 LeRobot 直传产生的目录版本 `source_kind=lerobot_direct` 是全局来源，且不会关联 `DataAsset`；空的 `data_asset_ids` 不表示版本无效。Train 不应读取其 raw 来源或依赖采集工作空间，而应等待 Data 对该固定版本成功创建 `lerobot_3_0` export，再消费 export 返回的 `oss_uri`。

目录导出只有在记录里带稳定 `oss://` URI 时才能登记。占位 checksum 会返回明确失败，不扫描 raw/process 桶，也不下发 AK/SK。

目录登记还要求导出 worker 持久保存、与归档 SHA-256 绑定的实际 LeRobot `meta/info.json`。旧导出缺少该证据时需重新导出。帧数、采样率和相机字段来自该文件；视频样本没有动作或状态时记录维度 `0`，不会补造策略训练输入。tar 归档登记为 `REGISTERED`，当前物化器尚不支持安全解包验证，不能直接将其标成 `READY`。ACT/π0.5 的兼容性检查会拒绝缺失动作或状态的样本。

仅填写名称和 URI 的手动登记仍受支持，默认状态为 `REGISTERED`，尚未验证的元数据为 `null`。显式登记 `READY` 必须提供实际帧数、采样率、相机、动作和状态维度，且 URI 不能指向归档文件。

可选依赖见仓库 `backend/pyproject.toml` 的 `train` extra（阿里云 DLC、OSS、MLflow）。FakeProvider 不需要这些包。
具体字段、启动方式与未验收边界见 [协作基线](../../docs/DEVELOPMENT_HANDOFF.md)。
