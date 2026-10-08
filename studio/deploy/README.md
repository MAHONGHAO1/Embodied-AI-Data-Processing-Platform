# 部署

本地部署由 PostgreSQL、Redis、MinIO、Alembic migration、FastAPI、Celery workers 和 Celery beat 组成；生产 Compose 使用外部、可达的 provider（示例为 Aliyun OSS），不依赖不存在的生产 MinIO 服务。`make prod-up` 启动 API 以及 control/ingest/media/publish/export/analytics workers；`worker-control` 兼容消费升级前遗留的 `general` 队列。AI worker 使用独立 Compose profile，默认不启动。

## 本地基础设施

前置条件：Docker、GNU `screen`（Ubuntu 可运行 `sudo apt install screen`），以及已安装
依赖的 `backend/.venv`（或仓库根目录 `.venv`）。

```bash
# 可选：同时删除旧版 Compose 遗留容器；这也会启动本地 MinIO 及三类 QuicStudio 桶。
docker compose -f deploy/docker-compose.yml up -d postgres redis minio minio-init --remove-orphans

make dev-up
```

`make dev-up` 会等待 PostgreSQL 和 Redis，升级 Alembic 到当前 head，初始化开发用户和
工作空间，并在 `screen` 中启动 API、control/ingest/media/publish/export/analytics workers 和 Celery
beat。数据库结构见 [数据库设计](../docs/DATABASE_SCHEMA.md)。

常用检查：

```bash
make dev-status
make dev-logs SERVICE=api
curl -sS http://127.0.0.1:8000/health
```

若没有安装 GNU `screen`，`make dev-up` 会在 migration 和 seed 后停止。安装 `screen`，
或分别启动进程：

```bash
make dev-api
make dev-worker-control
make dev-worker-ingest
make dev-worker-media
make dev-worker-publish
make dev-worker-export
make dev-worker-analytics
make dev-beat
```

使用 `make dev-down` 停止应用进程，使用 `make infra-down` 或 `make dev-infra-down` 停止
容器。只有明确需要清空本地运行数据时才可执行
`CONFIRM_DEV_WIPE=DELETE_LOCAL_RUNTIME make dev-wipe`。

本地对象存储使用 MinIO 的 S3 兼容接口：API 在 `127.0.0.1:9000`，控制台在
`127.0.0.1:9001`，并由 `minio-init` 幂等创建 `quicstudio-dev-raw`、
`quicstudio-dev-process` 和 `quicstudio-dev-export`。也可以只执行 `make minio-up`
或停止 `make minio-down`。

若 migration 报告缺少 `down_revision`，说明 `backend/alembic/versions/` 链路已断裂；
必须先修复父 revision，再重试 `make dev-migrate`。

## 生产环境

```bash
cp deploy/.env.example deploy/.env
make prod-init
make prod-up
```

`deploy/.env` 只保存非敏感配置。应用密钥、PostgreSQL/Redis 密码和 storage provider 凭据必须放在 `deploy/secrets/` 的 Docker secret 文件中（`storage_access_key_id`、`storage_secret_access_key`）。

`make prod-up` 与 `scripts/uat-up.sh` 不读取 `.env` 中的镜像标签；它们从当前 Git checkout 和实际 QRDF vendor 内容派生 `quicstudio-api:git-<12 位提交 SHA>-qrdf-<12 位内容指纹>`，用于发布审计和按提交回滚。若 Dockerfile 会复制的代码、前端或部署输入存在未提交改动，部署会拒绝继续；请先提交或还原这些改动，避免镜像内容与标签不一致。

三个逻辑 storage Bucket（raw/process/export）、provider Endpoint、浏览器直读和 worker 容量在 `deploy/.env` 配置。生产环境使用同一可达 provider；不提供本地磁盘或 `official` fallback。工作空间/任务集的外部 OSS 导入授权由设置中心写入数据库，不使用环境变量兜底。具体安全、迁移、备份和恢复要求见 [生产环境配置](../docs/PRODUCTION_SETUP.md)。

## UAT 环境

UAT 与生产复用 `deploy/docker-compose.prod.yml`，但必须使用独立的 Compose project、环境文件、运行目录、Docker secrets、PostgreSQL/Redis 数据目录和 Aliyun OSS buckets。共享 Compose 只共享服务拓扑，不共享状态。

仓库内可以这样准备 UAT 配置：

```bash
cp deploy/uat/.env.example deploy/uat/.env
# 修改 CORS_ORIGINS、OSS bucket、API_PORT 和其他 UAT 参数
make uat-init
make uat-up
```

UAT 的 `SESSION_COOKIE_SECURE=false` 和 `HSTS_ENABLED=false` 是有意的 HTTP 测试配置；生产的 `deploy/.env` 必须保持这两个配置为 `true`。UAT 仍然使用 `ENVIRONMENT=production`，以保留强密钥、供应链和 OSS 部署校验，不等于关闭生产级服务端安全基线。

UAT 使用 Aliyun OSS 时，先检查再按需应用只允许当前 UAT HTTP Origin 的 CORS 规则：

```bash
make uat-cors-check
make uat-cors-apply
```

`--allow-http` 只会由 UAT CORS 入口显式传入；生产的 `oss-browser-cors-*` 入口不允许 HTTP Origin。UAT 的 bucket 名称应使用 `quicstudio-uat-raw`、`quicstudio-uat-process`、`quicstudio-uat-export` 或团队批准的独立命名，不能与生产 bucket 混用。

常用 UAT 运维入口：

```bash
make uat-status
make uat-logs
make uat-down
make uat-bootstrap-admin ADMIN_EMAIL=admin@example.com
```

如果 UAT 部署在单独 checkout 或主机目录，可通过 `UAT_DEPLOY_DIR`、`UAT_ENV_FILE`、`UAT_COMPOSE_PROJECT` 和 `UAT_HEALTH_URL` 覆盖默认值；不要让 UAT 复用生产的 `.env`、secrets 或 runtime。

## 原生 LeRobot 复制与 ZIP

`OSS_BUCKET_EXPORT` 是平台拥有的 LeRobot 交付 Bucket：原生 LeRobot 接入会从已授权
source scope 中读取经 `complete.json` 验证的精确对象，再写入服务端推导的目标目录。
单个 source scope 前缀必须覆盖完整 LeRobot 数据集根目录，不能只授权完成标记或某个
payload 子目录。
它不是来源 Bucket，也不能通过浏览器提交或替换。标准目录 API 不返回来源 URI、对象 key
或临时签名 URL；只有复制成功的 active 数据集才能由有权限的用户读取平台目录 URI。

DSW 挂载优先使用该平台目录，保持 `data/`、`meta/`、`videos/` 和 `complete.json` 的原生
结构。ZIP 是按需生成的临时下载物：`NATIVE_LEROBOT_BUNDLE_MAX_GB` 限制单个 ZIP 的输入
总量，`NATIVE_LEROBOT_BUNDLE_RETENTION_DAYS` 控制其保留期。ZIP 暂存始终位于
`scratch_root`，定时清理只删除已过期 bundle 记录对应的精确对象，不会删除平台目录或
外部来源。

从受控运维机执行以下 UAT 更新与检查。第一条只更新 `/opt/quicstudio/uat`，后两条分别
检查 UAT health 和服务状态；它们都不触碰生产环境：

```bash
ssh ecs-data 'cd /opt/quicstudio/uat && ./uat-up.sh'
ssh ecs-data 'curl --fail --silent --show-error http://127.0.0.1:18080/health'
ssh ecs-data 'docker compose --project-name quicstudio-uat --env-file /opt/quicstudio/uat/quic_studio/deploy/.env -f /opt/quicstudio/uat/quic_studio/deploy/docker-compose.prod.yml ps'
```

验收时以脱敏的已授权 LeRobot marker 验证：复制完成前没有 URI/ZIP 交付；完成后可复制
平台目录 URI；ZIP 下载 descriptor 不返回来源 URI；失效 bundle 的清理不影响平台目录。

## UAT 静态资源检查

应用只会对 `/js/`、`/css/`、`/vendor/` 下的静态文本资源提供 gzip；认证 API、
下载、artifact 流和实时连接不参与压缩，静态文件仍使用 `Cache-Control: no-cache`。
在 UAT 发布后，从 UAT 主机执行：

```bash
curl --fail --silent --show-error http://127.0.0.1:18080/health
curl --silent --show-error -H 'Accept-Encoding: gzip, br' \
  -D /tmp/quicstudio-uat-static.headers -o /dev/null \
  http://127.0.0.1:18080/js/app.js
curl --silent --show-error -H 'Accept-Encoding: gzip, br' \
  -D /tmp/quicstudio-uat-health.headers -o /dev/null \
  http://127.0.0.1:18080/health
```

前一份响应头应包含 `Content-Encoding: gzip`、`Vary: Accept-Encoding` 与
`Cache-Control: no-cache`；health 响应不应包含 `Content-Encoding`。这些命令只读取
本地 UAT 服务，不读取环境文件或修改生产入口。
