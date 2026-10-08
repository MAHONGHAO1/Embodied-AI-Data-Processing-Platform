# 脚本

脚本服务于 PostgreSQL 开发、QRDF 依赖、JobRun workers、部署验证和受控数据重置。

| 脚本 | 用途 |
|---|---|
| `fetch-qrdf.sh` | 获取固定版本的 QRDF SDK |
| `sync-qrdf.sh` | 从本地相邻仓库同步 QRDF，供联调使用 |
| `reset-data.sh` | 清理业务表和当前工作区受限运行时存储 |
| `setup.sh` | 准备 Python 环境与 QRDF 依赖 |
| `start.sh` | 启动 FastAPI 与静态前端 |
| `start-worker.sh` | 按 `control/ingest/media/publish/export/analytics/governance/ai` 队列启动 Celery worker |
| `deployment-image-tag.sh` | 输出当前 Git checkout 与 QRDF vendor 内容对应的部署镜像标签，并拒绝脏的 Docker 构建输入 |
| `init-deployment.sh` | 幂等创建指定部署的 runtime 目录和 Docker secret 占位文件 |
| `uat-up.sh` | 使用 UAT Compose 项目升级并启动当前服务基线 |
| `demo-smoke.mjs` | 启动本地 Mock 预览并用无头 Chrome 逐页校验演示视图（`make demo-check`） |
| `validate_mcap_pipeline.py` | 独立诊断 QRDF/MCAP 格式，不向平台导入数据 |

重建本地环境：

```bash
make dev-wipe CONFIRM_DEV_WIPE=DELETE_LOCAL_RUNTIME
make dev-up
```

重置脚本要求显式确认，并且只允许操作仓库声明的运行时子目录。
