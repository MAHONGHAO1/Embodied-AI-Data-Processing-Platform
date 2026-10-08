# UAT 源码直推部署与训练流程确认

Date: 2026-09-24  
Target: `/opt/quic_studio/uat/quic_studio`（`deploy/uat` + systemd `quicstudio-uat-source@*`，端口 `18081`）  
Archive: `quictrain/tmp/studio-uat-deploy/quic_studio_uat_src_20260923173128.tar.gz`  
Backup: `/opt/quic_studio/uat/backups/quic_studio_20260923173128`

## 部署方式

当前 Studio UAT **不是**纯 `make uat-up` Docker API 栈，而是：

- Compose 仅提供 `quicstudio-uat` 的 Postgres/Redis
- 应用进程为源码 + systemd（`/usr/local/sbin/quicstudio-uat-source`）
- 配置与密钥：`deploy/uat/.env`、`deploy/uat/secrets/`、`deploy/uat/runtime/`

本次将本地工作区（含未提交的训练可行性脚本）打包直推 ECS，rsync 时保留 `.env` / secrets / runtime / `.venv*`。

## 中间问题与处理

| 问题 | 处理 |
| --- | --- |
| `rsync --delete` 冲掉 `backend/vendor/qrdf`（本地仓库无 vendor） | 从备份恢复 vendor，并对 `.venv311` / `.venv` 执行 `pip install -e` |
| oneshot migrate 未重跑 | 直接执行 `quicstudio-uat-source migrate` → revision `0007_dashboard_package_lifecycle` |
| API Worker `No module named qrdf` | 恢复 vendor + editable 安装后恢复 |

## 服务状态（部署后）

- `GET http://127.0.0.1:18081/health` → `ready=true`，QRDF OK，UAT buckets OK  
- `GET /api/train/health` → `provider=fake`，`auth_mode=studio`，`provider_disabled` 边界保持  
- workers：api / beat / control / ingest / export / governance **active**  
- 共置 QuicTrain `:8001` → `healthy` / `aliyun_dlc` / `1.1.0`

## 训练流程确认

| 步骤 | 结果 |
| --- | --- |
| Studio JWT（含 `rte`）→ `/api/train/api/v1/auth/me` | 200，`project_id=prj_other` |
| Catalog 登记 `version_id=2, export_id=6` | 200，幂等 `created=false`，`dsv_d812c0a85701b958e1e1`，`REGISTERED` |
| 列出 datasets | 1 条 Catalog tar 归档 URI |
| `jobs/validate` ACT + `act-4090-standard` | `valid=false`，明确 `DATASET_NOT_READY` + `POLICY_FEATURE_MISSING`（action/state） |
| 真实算力 | UAT 嵌入面仍禁用；共置 QuicTrain DLC 控制面健康 |

这与既有 UAT 训练边界一致：登记可读可预检，**不会**在 fake provider 上误提交作业。

## 复盘注意

后续源码直推请 **不要**对 `backend/vendor/qrdf` 使用会删掉它的 `--delete`，或先在本地执行 `bash scripts/fetch-qrdf.sh` 再打包。
