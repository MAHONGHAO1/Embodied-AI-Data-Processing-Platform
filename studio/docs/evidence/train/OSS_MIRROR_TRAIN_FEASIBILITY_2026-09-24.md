# Studio ↔ QuicTrain OSS 镜像数据集挂载 + 算力链路可行性验证

Date: 2026-09-24  
ECS: `i-2ze11xs8mqln1c3ehrj9`（与 QuicData / QuicTrain / Studio UAT 共置）  
Invoke（干跑）: `t-bj06xycepzqlr0g`  
Invoke（CreateJob）: `t-bj06xycod7608ao` → DLC `dlci01y4qe867tcw`

## 范围

对照 QuicTrain「训练数据库」三层模型（Postgres `dataset_versions` + OSS/CPFS LeRobot 树 + 调度挂载），验证 Studio 侧对应的 **Quota4090 / ECS OSS 镜像挂载路径**，以及共置控制面调用 DLC 算力的可行性。

**不在本轮：** 改写 Studio UAT 的 `fake` provider；Catalog `.tar.gz` 自动解包物化（仍为明确缺口）。

## 关键发现

| 项 | 结论 |
| --- | --- |
| 主机 `/mnt/cpfs` | **未挂真实 CPFS**（空本地目录）。4090 路径本就不依赖主机 CPFS，只依赖 OSS DataSource。 |
| `QUICTRAIN_OSS_CPFS_MIRROR_PREFIX` | `.env` 有值，但 Compose 原先**未注入**容器；本轮已写入 live compose，api/scheduler 现为 `oss://…/quictrain`。 |
| Studio Catalog 导出 | `.tar.gz` → 只能 `REGISTERED`，**不能** READY / 调度。 |
| Studio UAT `/api/train` | `provider=fake` + `provider_disabled`；真实算力走共置 QuicTrain `:8001`。 |
| PushT smoke OSS 树 | 存在且含 `meta/info.json`（ECS RAM Role 可读）。 |
| 4090 mount plan | `oss://…/quictrain-pusht-smoke-v1` → `/quictrain/input/dataset`。 |
| DLC canary dry-run | 资源 `quota87wrnuka7bx`、UserVpc 配置位、mount 列表齐备。 |
| DLC CreateJob | **已接受**：`dlci01y4qe867tcw`（OSS mount + UserVpc，非 ResourceAllocateFailed）。Master 副本随后 Failed（运行时/训练失败，属下一层问题）。 |
| Compose `.env` UserVpc | 注入容器后 **JSON 引号被剥掉**；提交作业需从 compose YAML / 恢复器读取（脚本已处理）。 |

## 干跑检查结果（全部通过）

脚本：`backend/train/scripts/verify_oss_mirror_train_feasibility.py`  
编排：`backend/train/scripts/ecs_verify_studio_mirror_train.sh`

| Check | OK | 备注 |
| --- | --- | --- |
| `catalog_archive_gate` | yes | Catalog tar 不可调度；目录 OSS URI 可 |
| `oss_mirror_mapping` | yes | `bmcpfs://…/quictrain/...` → `oss://…/quictrain/...` |
| `ecs_4090_mount_plan` | yes | mount → `/quictrain/input/dataset` |
| `control_plane_health` | yes | QuicTrain `aliyun_dlc` / scheduler external |
| `dlc_canary` (dry_run) | yes | 未 CreateJob |
| `dlc_canary` (submit) | yes | `dlci01y4qe867tcw`；`feasibility=ACCEPTED`；终态 Failed（Master replica） |
| `studio_uat_train_boundary` | yes | fake；算力边界已记录 |
| `oss_dataset_probe` | yes | PushT smoke + `meta/info.json` |

数据集 URI：

`oss://oss-pai-1183v1b6du4vkucj3h-cn-beijing/quictrain/datasets/lerobot/quictrain-pusht-smoke-v1`

## 仓库改动

- `backend/train/infra/ecs/docker-compose.yml`：注入 `QUICTRAIN_OSS_CPFS_MIRROR_PREFIX`
- `backend/train/tests/contract/test_ecs_deployment.py`：断言上述注入
- `backend/train/scripts/verify_oss_mirror_train_feasibility.py`：可行性检查 + 可选 DLC canary
- `backend/train/scripts/ecs_verify_studio_mirror_train.sh`：ECS 编排（Cloud Assistant / 主机）
- `backend/train/tests/unit/test_oss_mirror_train_feasibility.py`：挂载/镜像映射单测

Live QuicTrain compose（`/opt/quictrain/current`）已同步注入 mirror 环境变量。

## Studio Catalog → 可训练 READY 的后续条件

1. 导出 **目录型** `oss://` LeRobot 树（或实现安全 tar 解包物化），再登记为 `READY`；或  
2. 将展开后的树同步到 `QUICTRAIN_OSS_CPFS_MIRROR_PREFIX` 下，经 QuicTrain `POST /api/v1/datasets` 登记。  
3. 真正训练提交走共置 QuicTrain（`aliyun_dlc`），不要在当前 UAT fake 嵌入面上硬开。  
4. 修复 `.env` 中 `QUICTRAIN_DLC_USER_VPC_JSON` 的引号（建议单引号包裹整段 JSON），避免容器内 UserVpc 损坏。

## 复跑

```bash
# 干跑（默认）
REPORT_DIR=/tmp/studio-oss-mirror-feasibility \
  bash backend/train/scripts/ecs_verify_studio_mirror_train.sh

# 有界 CreateJob（会占用 4090 配额）
SUBMIT=1 REPORT_DIR=/tmp/studio-oss-mirror-feasibility-submit \
  bash backend/train/scripts/ecs_verify_studio_mirror_train.sh
```
