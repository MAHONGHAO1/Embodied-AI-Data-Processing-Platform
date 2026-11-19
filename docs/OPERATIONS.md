# 运维手册：队列、续跑、清理与 Linux

本页只描述 v0.16 已实现的单机单用户能力。多用户、权限、配额、分布式调度和对象存储仍未实现。

## 1. 目录布局

默认根目录是 `work/`；都可以用环境变量覆盖：

| 目录 | 默认 | 环境变量 |
| --- | --- | --- |
| 工作根 | `work/` | `ROBODATA_HOME` |
| 业务批次/登记 | `work/business/` | `ROBODATA_BUSINESS_ROOT` |
| 运行记录 | `work/runs/` | `ROBODATA_RUNS_ROOT` |
| 队列 | `work/business/queue/` | 跟随业务根 |
| 登记表 | `work/business/registry.sqlite3` | 跟随业务根 |
| 导出 | `work/exports/` | 无（跟随 `ROBODATA_HOME`） |
| 样本 | `work/samples/` | 无（跟随 `ROBODATA_HOME`） |

`robodata status` 会打印实际解析路径。

## 2. 队列

### 提交

```powershell
uv run --locked robodata jobs submit --plan work/telecom-500/process-plan.json
```

输出 `job_...`。同一个计划、同一个 `business_root` / `runs_root` 重复提交会返回已有任务，不会重复跑。

### 查看

```powershell
uv run --locked robodata jobs list
uv run --locked robodata jobs show job_...
```

`show` 包含任务状态、结果和追加事件；绝对本机路径在 Web API 中会被过滤。

### 执行

```powershell
# 常驻，默认并发 1
uv run --locked robodata worker --loop

# 只跑一个任务后退出（适合 cron / CI）
uv run --locked robodata worker --once
```

Web 只提交/查询，不执行长任务。队列是单机 SQLite；默认只允许一个 worker 并发处理，避免两个转换同时抢磁盘。

### 取消与重试

```powershell
uv run --locked robodata jobs cancel job_...
uv run --locked robodata jobs retry job_...
```

- 排队中：立即 `canceled`。
- 运行中：写取消标记并杀子进程树；worker 把任务收尾为 `canceled`。已写出的运行目录保留，但不会新增登记版本。
- 失败/取消：可人工重试；成功任务不能重试覆盖。

### worker 崩溃后

- 租约默认 60 秒，worker 每 10 秒续租。
- 租约过期后，新 worker 先检查旧子进程：
  - 还活着：接管并继续等待，不重复启动；
  - 已退出：任务重新入队，由 `process --plan` 的状态与预览 journal 续跑。
- 达到 `max_attempts` 仍失败：任务标记 `failed`，不无限重试。

### API

```text
GET  /api/jobs
POST /api/jobs
GET  /api/jobs/{job_id}
POST /api/jobs/{job_id}/cancel
POST /api/jobs/{job_id}/retry
```

仍是 loopback 本地边界；没有账号、令牌或权限模型。

## 3. 预览续跑

当前只有 Matching Cable 预览做到 Episode 级：

- 每集编码到 `preview.units/episode_<source_id>/`；
- 完成后写 `preview.units/journal.jsonl`；
- 重启时只复用 `status=done` + 输入帧摘要一致 + 产物哈希一致的单位；
- 最终用 LeRobot 官方 `aggregate_datasets` 合并成标准 v3.0 预览；
- 最终 `source_manifest.json` 里有 `resume.reused_episodes` / `encoded_episodes`。

其他阶段（`format_write`、HDF5/SO-100/ALOHA 转换）仍是阶段级重跑。不要把它写成“全格式断点续跑”。

## 4. 磁盘与清理

```powershell
uv run --locked robodata status
uv run --locked robodata gc --keep-last 5          # 只预览
uv run --locked robodata gc --keep-last 5 --apply  # 真删除
```

`gc` 的保护规则：

- 不删 `completed` 运行；
- 不删运行中目录；
- 不删被任何 `batch_*.json` 引用的路径；
- 不删被登记表 `artifact_path` / `zip_path` / `card_path` 引用的路径；
- 只删除失败/中断且未被引用的运行目录；
- `--keep-last N` 保留最近 N 个候选。

`process --plan` 开始前会按输入大小估算空间（约 3 倍输入 + 256 MiB，至少 512 MiB）。不够直接失败，避免写一半。测试或特殊环境可用 `ROBODATA_SKIP_DISK_CHECK=1` 跳过，但不要在生产路径长期设置。

## 5. Linux 与 Docker

### Linux 主机（转换主路径）

```bash
uv sync --locked
uv sync --project tools/conversion --locked
uv run --locked robodata worker --loop
```

转换仍然走 `tools/conversion/.venv` 的固定 LeRobot 环境。CI 的 `linux-conversion` job 会在 Ubuntu 上跑 v2.1 官方转换/加载和预览续跑。

### Docker（Web + 队列）

```bash
docker compose up --build
```

- `web`：FastAPI + 构建后的 React；
- `worker`：一次性执行 `robodata.worker --once`，用于验证容器内 SQLite 队列可打开；
- 共享 `./docker-work:/app/work`。

镜像**不包含** PyTorch/LeRobot 隔离转换环境，因此容器内不做真实转换。需要完整转换时，在 Linux 主机上安装 `tools/conversion`，或另建转换镜像（未在 v0.16 声明）。

## 6. 常见故障

| 现象 | 检查 |
| --- | --- |
| 任务一直 `queued` | 有没有 worker 在跑？`jobs list` / `status` |
| 任务 `running` 但没日志 | 看 `work/business/queue/jobs/<id>/stdout.log` / `stderr.log` |
| worker 被杀后任务不恢复 | 等租约过期（默认 60 s），再启动 worker |
| 任务 `failed`，结果是 `rejected` | 业务拒绝，不是基础设施重试；看 `result.message` |
| 磁盘不足 | `status` 看 free；`gc` 预览可清理目录 |
| Docker worker 立即退出 | 空队列时 `--once` 正常退出 0；不是崩溃 |
