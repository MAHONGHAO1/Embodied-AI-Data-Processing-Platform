# QuicData 生产环境配置

生产基线使用 PostgreSQL、Redis、FastAPI、Celery JobRun workers 和同源静态前端。部署升级前必须备份数据库与对象存储状态，先运行 Alembic migration，再启动应用。SQLite 和进程内共享状态回退不受支持。

## 准备主机

复制部署样例：

```bash
cp deploy/.env.example deploy/.env
```

在 `deploy/.env` 设置镜像仓库、软件源、准确的前端 Origin 和非敏感运行参数：

```dotenv
DOCKER_REGISTRY_PREFIX=
APT_MIRROR_URL=
APT_SECURITY_MIRROR_URL=
PYPI_INDEX_URL=
NPM_REGISTRY_URL=
CORS_ORIGINS=https://data.example.com
```

生产密钥写入 `deploy/secrets/` 对应文件，并限制为 `0600`。不要把密钥放入 `deploy/.env`，也不要提交 `.env`、生产凭据或带密钥的 `data/runtime_config.json`。

每个 Celery worker 容器都有独立的内存和 memory-plus-swap 上限，默认 12 GiB：

```dotenv
WORKER_MEMORY_LIMIT=12g
```

该限制用于故障隔离，不代替 worker 内部的有界内存处理。主机容量应按可能同时运行的 worker 数量计算。

## 认证与 HTTPS

生产环境必须把 API 和静态前端放在同一个 HTTPS Origin 下。API 端口默认只绑定回环地址；不要公开 PostgreSQL、Redis 或存储 Bucket。

登录后，短期 Access Token 仅保存在页面内存并通过 Bearer Header 使用。后端还会签发不透明浏览器会话 Cookie，属性为 `HttpOnly`、`SameSite=Lax`、Host-only、`Secure`，路径限制为 `/api/v1/auth`。新标签页通过该 Cookie 调用刷新接口恢复 Access Token，前端不持久化认证令牌。

反向代理必须保留正确的 `Host`、`Origin` 和转发协议语义。`CORS_ORIGINS` 只能列出完整、可信的前端 Origin，不允许 `*`。Cookie 鉴权的刷新和注销接口会拒绝跨站来源。

`redis-state` 保存浏览器会话摘要、限流、Socket.IO、任务锁、上传状态和标注会话；`redis-broker` 独立保存 Celery broker/result 状态。两者都启用 AOF、自动 rewrite 和 `noeviction`，避免任务风暴挤掉登录或锁状态。API、worker 和 beat 在依赖未配置或不可用时必须显式失败，不能回退到进程内状态。

## 配置对象存储

浏览器不能提供 OSS Key、Bucket 或凭据。后端根据授权数据库 ID 生成对象 Key。启用云存储时，在部署配置中设置四个逻辑 Bucket：

```dotenv
STORAGE_MODE=cloud
OSS_CLOUD_ENABLED=true
OSS_ENDPOINT=oss-cn-beijing-internal.aliyuncs.com
OSS_REGION=cn-beijing
OSS_BUCKET_RAW=your-raw-bucket
OSS_BUCKET_PROCESS=your-process-bucket
OSS_BUCKET_OFFICIAL=your-official-bucket
OSS_BUCKET_EXPORT=your-export-bucket

PUBLICATION_SOURCE_CACHE_TTL_SECONDS=86400
PUBLICATION_SOURCE_CACHE_MAX_BYTES=21474836480

IMPORT_MAX_UPLOAD_GB=100
IMPORT_API_UPLOAD_MAX_MB=256
IMPORT_DIRECT_UPLOAD_PART_MB=64
IMPORT_DIRECT_UPLOAD_EXPIRE_HOURS=24
IMPORT_DIRECT_UPLOAD_CONCURRENCY=4

RUNTIME_REALTIME_EVENT_RETENTION_DAYS=7
RUNTIME_JOB_RETENTION_DAYS=30
RUNTIME_IMPORT_SESSION_RETENTION_DAYS=30
RUNTIME_RETENTION_BATCH_SIZE=500

NATIVE_LEROBOT_BUNDLE_MAX_GB=100
NATIVE_LEROBOT_BUNDLE_RETENTION_DAYS=7

OSS_BROWSER_DIRECT_ENABLED=true
OSS_BROWSER_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
OSS_BROWSER_URL_TTL_SECONDS=900
```

Celery Beat 每小时执行一次有界清理：只删除超过保留期的已发布实时事件、终态任务记录，以及没有 Episode/资产依赖的失败、取消或被替代导入会话。待发布事件、运行中任务、成功导入和安全审计日志不会被自动删除。

`OSS_BUCKET_EXPORT` 同时承载服务端生成的 QRDF 导出和平台拥有的原生 LeRobot 交付目录；
它不能配置为外部 LeRobot 来源 Bucket。原生目录和 ZIP key 均由已验证 marker、数据库
归属和服务端模板推导，浏览器不能提供 key、前缀或来源 URI。原生 LeRobot 的默认交付物是
目录，便于 DSW 挂载后直接读取 `data/`、`meta/`、`videos/` 和 `complete.json`。ZIP 仅按需
生成，输入总量受 `NATIVE_LEROBOT_BUNDLE_MAX_GB` 限制，临时文件位于 `storage_root`；保留
清理只按 NativeLerobotBundle 记录的 exact target key 删除过期 ZIP，绝不删除平台目录或
来源对象。

对原生 LeRobot，启用中的外部 OSS scope 必须以一个前缀覆盖完整数据集根目录；只授权
`complete.json`、`data/`、`meta/` 或 `videos/` 的任一子路径都会在复制前被拒绝。

在未启用浏览器 OSS 直读的环境，ZIP descriptor 使用短期、一次性、数据集和操作者绑定的
同源下载 URL。该 URL 不得记录、持久化或出现在诊断输出中；API/worker 仍使用内网 OSS
Endpoint，不因浏览器回退扩大 Bucket 权限。

使用 `make prod-init` 创建可选 secret file，然后在服务器终端写入 OSS Access Key。Cloud 模式在凭据不完整时拒绝启动。内网 Endpoint 用于 ECS/worker；浏览器公网 Endpoint 仅签发经过对象级授权的短期 URL。process/official/export 只允许精确对象 GET；raw 只允许导入会话服务端确定的精确 multipart PUT，不签发 raw GET。

raw Bucket 必须配置浏览器上传 CORS：Origin 只能包含 `CORS_ORIGINS` 中受信任的 HTTPS 站点，允许 `PUT`，允许 `Content-Type` 请求头，并把 `ETag` 加入暴露响应头；不得用 `*` 放宽 Origin。默认 CSP 只会把校验后的 raw Bucket 精确公网 Origin 加入 `connect-src`；若部署覆盖 `CONTENT_SECURITY_POLICY`，必须提供等价的最小范围，否则浏览器会在发出 UploadPart 前拦截请求。每个 UploadPart URL 都会绑定 `Content-Type: application/octet-stream`，浏览器必须原样发送该签名头，否则 OSS 会按签名不匹配拒绝请求。另需配置“中止未完成 multipart upload”的生命周期规则，其天数不能长于团队可接受的孤儿分片保留期，建议 1 天，与默认 24 小时上传会话相匹配。Bucket 版本、SSE-KMS、禁止覆盖和最小权限策略仍按 raw 数据要求执行。

部署脚本会先在当前 Compose 项目中显式启动并等待 `redis-state`，再把这一规则作为显式门禁。CORS 命令仍使用 `run --no-deps`，避免它隐式拉起 migration 或应用服务；容器入口的 Redis 连通性检查保持启用。UAT 的 `scripts/uat-up.sh` 会在迁移和服务切换前幂等补齐并复查规则，且仅该路径可通过 `--allow-http` 支持隔离的 HTTP UAT；生产 `make prod-up` 只读检查，发现缺失规则就停止，不会隐式修改 Bucket。生产需要运维明确执行一次 `make oss-browser-cors-apply`，再重试 `make prod-up`。若 raw Bucket 已有任意 Origin、方法或请求头为 `*` 的 CORS 规则，检查会失败并要求运维先人工收紧，避免“补齐”后仍保留宽松规则。开启浏览器直传时，页面 CSP 只允许由 `OSS_BUCKET_RAW` 与 `OSS_BROWSER_ENDPOINT` 推导出的精确 raw Bucket 公网 Origin，不允许泛 OSS 域名。

部署后使用容器内的凭据检查精确 Origin；默认命令只检查，不修改 Bucket：

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.prod.yml exec -T api \
  /app/deploy/run-with-secrets.sh python -m scripts.configure_oss_browser_cors \
  --origin https://data.example.com
```

确认 Origin 无误后可显式增加 `--apply`，工具只追加缺少的上传规则并保留已有规则。仅隔离 UAT 使用 HTTP 时还必须显式增加 `--allow-http`；生产环境不得使用该开关。更换域名或 Bucket 时应先配置并通过此检查，再开放大文件上传。

`worker-publish` 会把已校验的不可变 raw QRDF 缓存在
`storage_root/hot/publication-source-cache/`，避免同一源 Episode 的每个切片重复从 OSS 下载。
默认保留 24 小时且总量不超过 20 GiB；清理按最旧优先执行，并跳过仍被发布任务持锁使用的缓存项。
该目录只是一份可重建缓存，不包含凭据，也不能作为 raw 数据的唯一副本。调整上限时需要把并发发布 staging
和其他本地运行数据一并计入磁盘容量。

工作空间/任务集的外部 OSS 导入授权必须由管理员在设置中心维护，并保存到 PostgreSQL 的 `external_oss_import_scopes`。数据库记录是唯一授权来源；没有启用中的精确 Bucket/前缀授权时默认拒绝，不使用环境变量兜底。

以下 EGO 设置继续由部署层维护，因为它们描述底层数据通道和存储布局，不授予租户权限：

```dotenv
EGO_SOURCE_OSS_OFFLINE_INGRESS_PREFIXES=prod/sources/
EGO_SOURCE_OSS_MANUAL_STAGING_PREFIX=prod/manual/ego/
EGO_SOURCE_OSS_RAW_DESTINATION_PREFIX=raw/ego/
EGO_SOURCE_OSS_MULTIPART_COPY_ENABLED=true
```

历史 EGO OSS 导入使用同地域服务端复制进入规范 raw 存储。超过 1 GiB 的对象需要 multipart `UploadPartCopy`；仅在确认源 Bucket 读权限、目标 Bucket 写权限和版本策略后启用。规范对象 Key 见 [API.md](API.md)。

## 平台设置

管理员可以在设置中心维护行为 AI 运行策略及外部 OSS 导入范围。AI 的 API Key、App ID 等 secret 采用 envelope encryption 保存，并通过 API 只写不读；未创建数据库 AI 设置时，现有部署级 AI bootstrap 配置仍可提供初始运行参数。

平台 OSS 凭据、逻辑 Bucket、Endpoint、SDK 二进制和校验清单仍属于部署输入，不能由租户设置页覆盖。

## 启动

```bash
make prod-up
make prod-bootstrap-admin ADMIN_EMAIL=admin@example.com
```

`make prod-up` 先构建 migration 镜像，启动并等待 `redis-state`，通过 OSS CORS 只读门禁后运行 Alembic，再启动 PostgreSQL、Redis、API、Celery beat 和当前 JobRun worker：

部署镜像标签由当前 Git checkout 和实际 QRDF vendor 内容自动生成，格式为
`git-<12 位提交 SHA>-qrdf-<12 位内容指纹>`。可先运行
`make prod-image-tag` 核对将使用的标签；即使历史 `deploy/.env` 仍包含
`QUICDATA_IMAGE_TAG`，`make prod-*` 和 `scripts/uat-up.sh` 也会覆盖它，避免构建新代码却沿用旧标签。Dockerfile 会复制的构建输入存在已暂存、未暂存或未跟踪改动时，标签脚本会拒绝部署；先提交或还原这些输入后再执行。回滚时先切换到目标提交，再运行 `make prod-up`，使运行镜像标签与源代码保持一致。

- `worker-ingest`：解析导入产物并创建 source Episode；
- `worker-media`：以默认 4 个槽位执行质量检查、单 Episode 预览和派生预览批处理；
- `worker-publish`：串行发布已验证的 official QRDF 产物；
- `worker-export`：执行不可变 DatasetRevision 导出；
- `worker-analytics`：执行看板 ETL；
- `worker-control`：恢复过期 lease、执行运行态保留清理，并兼容排空升级前遗留的 `general` 消息。
- `realtime-dispatcher`：唯一消费 PostgreSQL outbox 并经 Socket.IO 广播；生产 API 不运行 dispatcher 循环。

所有 Worker 使用 `prefetch=1`，并同时配置 `max-tasks-per-child` 和
`max-memory-per-child`。`*_WORKER_CONCURRENCY` 同时注入对应的数据库 JobRun
槽位数；不得只调大 Celery 并发而不调整槽位。

旧版 Task/EGO worker 保持禁用，不能与当前 JobRun worker 混用。初始环境不创建任务集，管理员需先创建任务集，再创建采集批次。

可选 `worker-ai` 只消费 `ai` 队列，`make prod-up` 默认不启动。只有在挂载经过 checksum 验证的 Embodied VL SDK 后才能显式启用；生产启用需要独立审查。行为 AI 输入由服务端复制到 process Bucket 的内容寻址命名空间，并通过短期 HTTPS URL 提供给服务商。

## 浏览器上传与恢复

所有导入受 `IMPORT_MAX_UPLOAD_GB` 限制。默认不超过 256 MiB 的文件经 API 分片上传，仍会占用 `storage_root` 的暂存和组装空间；超过 `IMPORT_API_UPLOAD_MAX_MB` 的文件必须使用 OSS multipart 直传，文件内容不经过 API 或 ECS 磁盘。直传使用 64 MiB 分片、默认 4 路并发，服务端逐片签发短期 URL，并在完成前以 OSS `ListParts`、对象大小和 CRC64 对账，不能信任浏览器提交的 ETag 清单。

API 分片上传中断后会保留对应 `imports/{import_session_id}` 暂存目录以便续传；OSS 直传中断后从 provider 已有 part 继续。授权用户取消 ImportSession 时，只能中止该会话记录的精确 Upload ID，或清理该会话暂存目录和 durable artifact-operation manifest 列出的未发布对象。失败或取消的数据库记录保留用于审计；操作不得删除 Batch、Episode 或已发布 raw 前缀。OSS 生命周期规则是服务中止失败和初始化响应丢失时的最终兜底。

## 检查与监控

```bash
make prod-status
curl http://127.0.0.1:8000/health
```

公共 `/health` 会实际检查 PostgreSQL 与状态 Redis，并复用短期缓存的 Worker 探测结果。管理员可调用 `GET /api/v1/platform-settings/runtime-health` 查看各 JobRun 队列深度和最老排队时间、待发布 outbox 年龄、Worker 数量及磁盘余量；响应不包含主机名、连接串、凭据或存储绝对路径。

上线前至少确认：

- Alembic 已到当前 head，API schema current 检查通过；
- PostgreSQL 和 Redis 没有公网监听；
- HTTPS、CORS 和会话 Cookie 属性正确；
- raw Bucket 对当前浏览器 Origin 的 multipart CORS 检查通过；
- 管理员 bootstrap 不使用默认密码且只执行一次；
- OSS Bucket policy、SSE、浏览器直读范围和数据库导入授权符合预期；
- 所有 worker 消费正确队列，JobRun lease 和失败告警可观测；
- 日志、审计和错误响应不包含 Token、Cookie、OSS 凭据或内部绝对路径。

## 备份与恢复

PostgreSQL、`deploy/runtime/storage` 和对象存储清单必须在同一恢复点备份。备份凭据和生产数据按敏感资产管理，恢复演练应验证数据库记录、artifact manifest 与对象实际存在性一致。

从运维机通过 SSH 配置别名触发 PostgreSQL 逻辑备份：

```bash
bash scripts/backup-deployment.sh \
  --ssh-target ecs-uat \
  --deploy-dir /opt/quicdata/uat/quicdata \
  --compose-project quicdata-uat \
  --retention-days 14 \
  --dry-run
```

确认 dry-run 后移除 `--dry-run`。脚本在远端 `deploy/runtime/backups` 原子生成 custom-format dump 和 SHA-256 校验文件，并只清理符合自身命名规则且超过保留期的文件；SSH 凭据由本机 SSH 配置管理，脚本不接受或记录密码。数据库备份仍需与对象存储清单和必要的本地唯一资产协调到同一恢复点。

历史 migration `0010_batch_episode_reset` 清理旧业务数据且不可降级；从早期版本升级时必须先在隔离副本验证并完成备份。当前部署不应通过重复运行历史 reset migration 清理数据。

```bash
make prod-wipe CONFIRM_WIPE=DELETE_DEPLOY_RUNTIME
```

`prod-wipe` 是破坏性操作，只删除本机 `deploy/runtime` 范围，不触碰 OSS。云对象保留和清理必须通过 Batch/Episode 生命周期规则及精确 manifest 管理。
