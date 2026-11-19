# v0.16.0 — 可运维：单机队列、预览级续跑、目录治理

**这一版不扩格式，把“能跑通一次”变成“中断可恢复、任务可排队、目录可治理”。**

## 改了什么

### 1. 持久任务队列（单机单用户）

- 新增 SQLite 队列 `work/business/queue/jobs.sqlite3`，任务状态 `queued / running / succeeded / failed / canceled`。
- 新增独立 worker：`robodata worker --loop`，默认并发 1；不把长任务塞进 Web 进程。
- 租约 + 心跳：worker 崩溃后租约过期；若子进程还活着，新 worker **接管**而不是重复启动；若已退出，则按计划状态重新入队。
- 取消：排队中立即取消；运行中杀进程树，保留运行目录但不登记。
- 重试：失败/取消任务可人工重试；成功任务不能重试覆盖。
- 幂等键：`plan_digest + business_root + runs_root`；重复提交返回已有任务。
- CLI：`jobs submit/list/show/cancel/retry`、`worker`。
- API：`GET/POST /api/jobs`、`GET /api/jobs/{id}`、`POST /api/jobs/{id}/cancel|retry`；仍是本机只读/本机写入边界，没有鉴权。

### 2. Matching Cable 预览 Episode 级续跑

- 预览改为先按 Episode 编码到 `preview.units/episode_<id>`，再用 LeRobot 官方 `aggregate_datasets` 合并；每完成一集写 `journal.jsonl`。
- 续跑只复用同时满足以下条件的单元：`status=done`、输入帧摘要一致、产物存在且哈希一致。
- 被中断后重启，已完成的 Episode 不重新编码；最终 `source_manifest` 里记录 `reused_episodes` / `encoded_episodes`。
- 最终预览仍是 LeRobot v3.0，下游转换复用同一套 MP4；`verify_matching_optional.py` 的完整复用/子集/篡改回退 18 阶段全部通过。
- **边界**：`format_write`、老 HDF5/SO-100/ALOHA 转换仍是阶段级重跑，不是全格式续跑。

### 3. 目录、磁盘与清理

- 新增 `robodata/layout.py`：统一 `home/business/runs/queue/registry/exports/samples/cache`，保留旧环境变量。
- `robodata status` 与 `GET /api/status`：队列、运行状态、磁盘、可清理空间。
- `robodata gc --dry-run|--apply --keep-last N`：只删失败/中断且未被批次或登记表引用的运行目录；默认 dry-run；绝不删完成记录、当前产物或登记卡。
- `process --plan` 启动前磁盘预检：预计空间不足直接失败，不写半成品；可用 `ROBODATA_SKIP_DISK_CHECK=1` 显式跳过。
- `doctor` 新增队列数据库检查。

### 4. Linux / Docker

- `tests` 的 Python 测试改为 Ubuntu + Windows 矩阵；Linux 成为主证据面。
- `acceptance` 新增 `linux-conversion`：Ubuntu 上安装隔离转换环境，跑 v2.1 官方转换/加载测试与 Episode 级预览续跑。
- Docker 增加 `docker-compose.yml`（web + 一次性 worker）；`docker-linux` CI 里启动容器内 worker 验证 SQLite 队列可打开。
- **边界**：镜像仍不含 PyTorch/LeRobot 隔离转换环境；容器内不做真实转换。转换主路径是 Linux 主机 + `tools/conversion`。

## 实测

### 500 帧电信 Matching Cable，经队列跑完整业务链

| 项 | 结果 |
| --- | --- |
| `jobs submit` + `worker --once` 全链路（导入、质检、标注、转换、验证、打包、交付、登记） | **218.27 s** |
| 重复提交同计划 | `status=existing`，不新建运行 |
| 队列空闲时再次 `worker --once` | 立即退出，无新增运行 |
| 数据卡 | `telecom-matching-cable-500` v1，`card_digest=ae3aa64022b6e6f3f59036840f58bba33fe879db6e096dd982c869b462546893` |
| Train 交接脚本 | `status=passed`、`card_valid=true`、500 行 / 1 Episode |
| 预览续跑记录 | `reused_episodes=0`、`encoded_episodes=1`（首次完整运行） |

### 预览续跑（2×2 合成夹具）

- 预编码 Episode 68 后运行真实预览：`reused_episodes=1`、`encoded_episodes=1`、2 个 Episode / 4 帧；只编码 Episode 70。
- 报告：`work/preview-resume-v016/report.json`。

### 队列取消

- 运行 12 s 后取消：任务进入 `canceled`，子进程树被终止，未新增登记版本；运行目录保留供诊断。

## 验证

- Python 全量：**374 passed / 16 skipped / 13 subtests passed**
- `robodata verify-fixtures`：**6/6**
- `robodata doctor`：`ok`
- 前端与 Chromium 流程：见 `docs/EVIDENCE.md`
- CI：见 `docs/EVIDENCE.md`

## 不声明

- 不声明全格式节点级续跑；当前只有 Matching Cable 预览做到 Episode 级。
- 不声明 Docker 内可做真实转换；镜像不含隔离转换环境。
- 不声明多用户、权限、配额、分布式调度或对象存储；这些仍未实现。
- 不声明全量 7 集打包/交付在本版跑过；本版证据是 500 帧电信样本与合成夹具。
- 不声明外部试点或采用；外部复现记录仍为 **0**。
- 旧 tag 不移动；`v0.12.1`、`v0.13.0`、`v0.14.0`、`v0.15.0` 保持原指向。

完整证据、命令、计数和未验证边界见 [docs/EVIDENCE.md](docs/EVIDENCE.md)。
