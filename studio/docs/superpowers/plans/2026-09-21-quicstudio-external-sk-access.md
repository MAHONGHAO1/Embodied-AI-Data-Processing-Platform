# QuicStudio 外部工具接入实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让外部工具用绑定到人的长期令牌，离线优先地完成数据包上传，并能批量取数与批量提交标注结果。

**架构：** 平台新增凭据层（`api_tokens` + Bearer 解析，与 JWT 共用 principal），上传沿用 `collection-upload-sessions` 并扩展离线清单与脱敏声明；新增取数清单与标注批量提交两个接口；客户端（duance）新增令牌凭据模式，用 SQLite 承载离线绑定与断点状态，并把同步目标从 Batch 切到数据包。

**技术栈：** FastAPI + SQLAlchemy + Alembic（单 baseline 迁移）+ pytest；前端无框架改动；duance 侧 PyQt6 + 标准库 `sqlite3`。

**规格：** [2026-09-21 外部工具接入规格](../specs/2026-09-21-quicstudio-external-sk-access-design.md)

---

## 执行状态（每轮更新）

- 规格：`docs/superpowers/specs/2026-09-21-quicstudio-external-sk-access-design.md`（已批准）
- 进度：任务 1–13 全部完成并提交；任务 10（旧链路清理）按顺序调整到 13 之后执行，同样已完成；RBAC 计划外插入项（退役 operator 角色）完成并提交
- 已完成提交：`3a32eb6` 规格 · `6d7bd00` 计划+执行状态 · `e1de2c3` 存储收敛 · `ffded8a` 退役 operator 角色 · `ddf187c`–`9ad624c` 任务 2–9 · `b41ba9a` 任务 10 旧链路退役
- 当前任务：**任务 14 端到端与验收**（真实 MinIO + 前端/客户端回归 + 规格勾选）
- 测试命令：`TEST_DATABASE_URL='postgresql+psycopg://quicdata:quicdata@127.0.0.1:5432/quicdata_test' TEST_REDIS_URL='redis://127.0.0.1:6379/15' backend/.venv/bin/python -m pytest <路径> -q`
- 客户端工作目录：`~/Documents/devlop/quicrobot/duance/.worktrees/quicstudio-sk-upload`（分支 `feat/quicstudio-sk-upload`，基于 origin/main）
- 客户端提交独立于本仓库：`9ca9bc0` 令牌凭据 · `3c2d810` SQLite 状态库 · `134f16a` 离线绑定与同步计划 · `0f8bcf9` 采集链路 API · `3b510e1` 上传编排 · `26ee51c` QRDF 声明映射 · `11e245e` 数据集绑定 · `6cae386` 同步窗口切到采集项目/采集任务/数据包（13b-2c）。
- 客户端 13b-2c 记录：同步窗口目标改为「工作区 → 采集项目 → 采集任务 → 数据包（多选）」，新增「下载平台清单 / 导入离线清单 / 绑定体检」与逐包进度、失败保持勾选的部分重试；`QuicdataPreferences` 改为 `collection_project_id/collection_task_id` 并容忍旧 `task_set_id/batch_id/task_label_id` 键；GUI 增加长期令牌粘贴/保存/清除（`credentials.db` 0600，令牌优先于 cookie）；README 改写为数据包上传流程。
- 任务 10 记录：删除 `routers/{batches,imports,duance_imports}.py` 与对应契约测试；前端删除「导入数据」批量上传对话框与 `import-upload.js`，改为「数据接入指引」弹窗（引导使用 duance 离线清单上传），删除批次详情/批次活动/遗留 LeRobot 扫描入口与 `api.js` 旧客户端；`services/batches.py` 仅保留 native LeRobot 注册所需的 `create_batch`；`test_batch_episode_api.py` 等改从 DB 直接建批次行或改走保留服务。
- 任务 10 验证：后端 `backend/tests` 981 passed / 10 failed（10 项与改动无关，已在干净 HEAD 基线复现）；前端 `node --test frontend/tests/*.test.mjs` 261 passed；`make demo-check` 22 个视图全部渲染通过。
- worktree 缺 `vendor/qrdf` 时可用同级仓库产物 `/Users/qingmuhy/Documents/devlop/quicrobot/qrdf/dist/qrdf-0.2.0-py3-none-any.whl`；当前测试走主 venv（`PYTHONPATH=src ../../.venv/bin/python -m pytest`），该 venv 已装 qrdf 0.2.0。worktree 缺 `vendor/qrdf`，所以 duance 测试用 `PYTHONPATH=src ../../.venv/bin/python -m pytest tests/... -q`（不要用 `uv run`）
- 已知预存在失败：`test_import_session_intake.py::test_chunked_import_endpoints_accept_bytes_without_exposing_storage_uri`（旧导入链路，留给任务 10）
- 环境限制：本地 MinIO + loopback 浏览器端点下 `browser_direct_enabled()` 为假（设计如此），取数清单只给对象元数据并 `available:false`；签名 URL 的验收放到具备公开端点的环境（任务 14 E2E）
- 测试对象存储：19000 是测试专用端点（`TEST_STORAGE_ENDPOINT`，默认 `http://127.0.0.1:19000`，凭据 `quicstudio-test/quicstudio-test-secret`，桶 `quicstudio-test-{raw,process,export}`）。本机用一次性容器提供：
  `docker run -d --name quicstudio-test-minio -p 127.0.0.1:19000:9000 -e MINIO_ROOT_USER=quicstudio-test -e MINIO_ROOT_PASSWORD=quicstudio-test-secret quay.io/minio/minio:RELEASE.2024-06-13T22-53-53Z server /data`
  再建三个测试桶；此前 `test_teleop_quality_camera_infer.py` 的 502 就是这个端点缺位所致，补上后 3 passed
- 收尾核对已完成（2026-09-22）：6 份旧 plan 全部逐条勾选，并在各自文件末尾写入「审计结果（2026-09-22 收尾核对）」段，记录实跑测试数、替代覆盖的预测文件名与命名冲突说明；`specs/2026-09-18-quicstudio-object-storage-logical-assets-design.md` 的「用户审查/批准」两行也已在用户确认后补记（2026-09-22）。

## 文件结构

平台（`quic_studio`）：

| 文件 | 职责 |
|---|---|
| `backend/data/models/api_token.py`（新建） | `ApiToken` 模型：绑定用户、名称、key_id、secret_hash、生命周期、审计字段 |
| `backend/alembic/versions/0001_baseline.py`（修改） | 追加 `api_tokens` 表与索引（项目以单 baseline 迁移承载） |
| `backend/data/services/api_tokens.py`（新建） | 签发 / 校验 / 吊销 / 轮换 / 限流计数 / 审计 |
| `backend/data/security/tokens.py`（新建） | `parse_bearer_token()`：从 `Authorization` 解析 `qs_<key_id>_<secret>` |
| `backend/data/routers/tokens.py`（新建） | `/api/v1/tokens` CRUD + rotate |
| `backend/data/utils/helpers.py`（修改） | `get_current_user` 增加令牌分支，复用同一 principal 形状 |
| `backend/data/services/collection_tasks.py` + `routers/collection_tasks.py`（修改） | 离线清单导出扩展为包级行 |
| `backend/data/services/collection_upload_sessions.py` + `routers/collection_upload_sessions.py`（修改） | 声明携带包级脱敏声明 |
| `backend/data/services/fetch_manifests.py` + `routers/fetch_manifests.py`（新建） | 批量取数清单（签名 URL + 校验和） |
| `backend/data/services/annotation_work_items.py` + `routers/annotation_work_items.py`（修改） | 批量提交、来源标记、`review_required` 权限校验 |
| `backend/data/services/storage_mode.py`、`config.py`、`import_intake.py`、`import_parser.py`、`derived_preview_batch.py`（修改/删除） | 存储收敛：只允许 `minio` / `aliyun_oss` |
| `backend/data/main.py`（修改） | 旧路由下线（清理任务） |

客户端（`duance`）：

| 文件 | 职责 |
|---|---|
| `src/duance/quicdata/client.py`（修改） | 令牌模式：直接使用长期令牌，保留密码登录 |
| `src/duance/credentials.py`（新建） | `credentials.db` 读写（name / secret / issued_at / expires_at / backend） |
| `src/duance/services/state_store.py`（新建） | `.duance/state.db` schema、迁移、按包/文件组查询 |
| `src/duance/services/quicdata_sync.py`（修改） | 目标切换为采集项目/采集任务/数据包；声明按包分组 |
| `src/duance/ui/sync_dialog.py`（修改） | 凭据选择、清单导入、绑定体检、逐包进度 |

测试：

| 文件 | 覆盖 |
|---|---|
| `backend/tests/test_api_tokens_service.py`（新建） | 签发/校验/吊销/轮换/过期/哈希存储 |
| `backend/tests/test_api_tokens_api.py`（新建） | `/tokens` CRUD、权限、审计、限流 |
| `backend/tests/test_token_auth_principal.py`（新建） | Bearer 与 JWT 共用 principal、跨工作空间拒绝 |
| `backend/tests/test_collection_task_manifest_export.py`（新建/扩展） | 包级清单列、离线可下载、revision |
| `backend/tests/test_collection_intake_desensitization.py`（新建） | 脱敏声明落库、v1 不门禁、开关预留 |
| `backend/tests/test_fetch_manifests_api.py`（新建） | 清单内容、签名 URL、越权 403、幂等 |
| `backend/tests/test_annotation_batch_submit_api.py`（新建） | 幂等、冲突 409、`review_required` 降级、来源标记 |
| `backend/tests/test_storage_provider_convergence.py`（新建） | 只接受两种 provider，非法值启动失败 |
| `tests/test_state_store.py`（duance 新建） | SQLite schema、`sync-state.json` 迁移、断点查询 |
| `tests/test_token_credentials.py`（duance 新建） | 令牌优先级、免登录同步、凭据不入日志 |

---

## 任务 1：存储收敛（第 0 步）

**文件：**
- 修改：`backend/data/config.py`（`storage_mode` 校验）
- 修改：`backend/data/services/storage_mode.py`（删除 local/cloud/hybrid 归一）
- 修改：`backend/data/services/import_intake.py`、`import_parser.py`、`derived_preview_batch.py`（移除对归一结果的依赖）
- 测试：`backend/tests/test_storage_provider_convergence.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_storage_mode_rejects_legacy_values(monkeypatch):
    from data.config import Settings

    for legacy in ("local", "cloud", "hybrid"):
        monkeypatch.setenv("STORAGE_MODE", legacy)
        with pytest.raises(ValidationError):
            Settings()


def test_storage_mode_accepts_supported_providers(monkeypatch):
    from data.config import Settings

    for value in ("minio", "aliyun_oss"):
        monkeypatch.setenv("STORAGE_MODE", value)
        assert Settings().storage_mode == value
```

- [x] **步骤 2：运行测试验证失败**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_storage_provider_convergence.py -v`
预期：FAIL（legacy 值当前被归一为 `hybrid`，不报错）

- [x] **步骤 3：编写最少实现代码**

`storage_mode.py` 收敛为白名单校验；`config.py` 用同一校验器：

```python
SUPPORTED_STORAGE_MODES = frozenset({"minio", "aliyun_oss"})


def normalize_storage_mode(raw: str | None) -> str:
    value = (raw or "minio").strip().lower()
    if value not in SUPPORTED_STORAGE_MODES:
        raise ValueError(
            f"unsupported storage mode {value!r}; expected one of {sorted(SUPPORTED_STORAGE_MODES)}"
        )
    return value
```

同时删除 `local / cloud / hybrid` 分支与 `uses_cloud_uri_authority` 对 hybrid 的依赖；受影响的导入路径在任务 10 一并清理。

- [x] **步骤 4：运行测试验证通过**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_storage_provider_convergence.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add backend/data/config.py backend/data/services/storage_mode.py \
  backend/data/services/import_intake.py backend/data/services/import_parser.py \
  backend/data/services/derived_preview_batch.py backend/tests/test_storage_provider_convergence.py
git commit -m "refactor(storage): accept only minio or aliyun oss"
```

---

## 任务 2：`api_tokens` 模型与迁移

**文件：**
- 创建：`backend/data/models/api_token.py`
- 修改：`backend/alembic/versions/0001_baseline.py`
- 测试：`backend/tests/test_api_tokens_service.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_api_token_row_stores_hash_not_secret(db):
    from data.services.api_tokens import issue_token

    issued = issue_token(db, user_id=1, name="duance-运维机", expires_in_days=90)
    assert issued["secret"].startswith("qs_")
    row = db.query(ApiToken).filter_by(key_id=issued["key_id"]).one()
    assert issued["secret"].split("_")[-1] not in (row.secret_hash or "")
```

- [x] **步骤 2：运行测试验证失败**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_api_tokens_service.py -v`
预期：FAIL（模块与表不存在）

- [x] **步骤 3：编写最少实现代码**

```python
class ApiToken(Base):
    __tablename__ = "api_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_api_tokens_user_name"),
        Index("ix_api_tokens_user_revoked", "user_id", "revoked_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(64), nullable=False)
    key_id = Column(String(32), nullable=False, unique=True)
    secret_hash = Column(String(255), nullable=False)
    scopes = Column(JsonDocument, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
```

迁移追加到 `0001_baseline.py`（与现有 0006/0007/0008 段落同样的写法），并补 `upgrade()`/`downgrade()` 两个分支。

- [x] **步骤 4：运行测试验证通过**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_api_tokens_service.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add backend/data/models/api_token.py backend/alembic/versions/0001_baseline.py \
  backend/data/services/api_tokens.py backend/tests/test_api_tokens_service.py
git commit -m "feat(auth): add api_tokens model and issuance service"
```

---

## 任务 3：令牌签发 / 吊销 / 轮换路由

> 口径（2026-09-22 用户确认）：令牌是账户密码的替身，**自助签发**——任何已登录用户只能为自己签发/列出/轮换/吊销，
> 不接受 `user_id` 入参，管理他人令牌不在 v1 范围。


**文件：**
- 创建：`backend/data/routers/tokens.py`
- 修改：`backend/data/main.py`（挂载路由）
- 测试：`backend/tests/test_api_tokens_api.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_issue_list_and_revoke_token(client, admin_headers):
    created = client.post("/api/v1/tokens", json={"name": "算法批-A"}, headers=admin_headers)
    assert created.status_code == 200
    payload = created.json()["data"]
    assert payload["secret"].startswith("qs_")
    assert (
        "secret"
        not in client.get("/api/v1/tokens", headers=admin_headers).json()["data"]["items"][0]
    )

    revoked = client.delete(f"/api/v1/tokens/{payload['id']}", headers=admin_headers)
    assert revoked.status_code == 200
    assert client.get("/api/v1/tokens", headers=admin_headers).json()["data"]["items"][0][
        "revoked_at"
    ]


def test_rotate_keeps_previous_secret_for_one_hour(client, admin_headers):
    created = client.post(
        "/api/v1/tokens", json={"name": "duance-运维机"}, headers=admin_headers
    ).json()["data"]
    rotated = client.post(f"/api/v1/tokens/{created['id']}/rotate", headers=admin_headers).json()[
        "data"
    ]
    assert rotated["secret"] != created["secret"]
```

- [x] **步骤 2：运行测试验证失败**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_api_tokens_api.py -v`
预期：FAIL（404，路由不存在）

- [x] **步骤 3：编写最少实现代码**

```python
router = APIRouter(prefix="/tokens", tags=["外部凭据"])


class IssueTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    expires_in_days: int | None = Field(default=90, ge=1, le=3650)


@router.post("")
def issue_token_endpoint(
    body: IssueTokenRequest, db: Session = Depends(get_db), user: dict = Depends(get_current_user)
):
    require_permission(user, "workspace:write")
    issued = issue_token(
        db,
        user_id=int(user["sub"]),
        name=body.name,
        expires_in_days=body.expires_in_days,
        created_by=int(user["sub"]),
    )
    db.commit()
    return success(issued)
```

轮换实现：生成新 secret、旧记录写入 `rotated_from_id` 并在 1 小时内继续可用（校验时按 `rotated_at + 1h` 判断）。

- [x] **步骤 4：运行测试验证通过**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_api_tokens_api.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add backend/data/routers/tokens.py backend/data/main.py backend/tests/test_api_tokens_api.py
git commit -m "feat(auth): expose token issue, rotate and revoke endpoints"
```

---

## 任务 4：Bearer 解析接入 principal

**文件：**
- 创建：`backend/data/security/tokens.py`
- 修改：`backend/data/utils/helpers.py`（`get_current_user`）
- 测试：`backend/tests/test_token_auth_principal.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_token_can_list_batches_like_the_owning_user(client, admin_token_headers):
    assert (
        client.get(
            "/api/v1/batches", params={"workspace_id": 1}, headers=admin_token_headers
        ).status_code
        == 200
    )


def test_revoked_token_is_rejected(client, admin_token_headers, revoke_admin_token):
    revoke_admin_token()
    response = client.get(
        "/api/v1/batches", params={"workspace_id": 1}, headers=admin_token_headers
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "revoked"
```

- [x] **步骤 2：运行测试验证失败**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_token_auth_principal.py -v`
预期：FAIL（401，令牌未被识别）

- [x] **步骤 3：编写最少实现代码**

```python
def parse_bearer_token(db: Session, header_value: str | None) -> dict | None:
    """Return a JWT-shaped principal for ``qs_<key_id>_<secret>`` tokens."""
    if not header_value or not header_value.lower().startswith("bearer "):
        return None
    raw = header_value.split(" ", 1)[1].strip()
    if not raw.startswith("qs_"):
        return None
    _, key_id, secret = raw.split("_", 2)
    row = db.query(ApiToken).filter(ApiToken.key_id == key_id).one_or_none()
    if row is None:
        raise TokenError("invalid_token")
    if row.revoked_at is not None and not _within_rotation_window(row):
        raise TokenError("revoked")
    if row.expires_at is not None and row.expires_at < datetime.utcnow():
        raise TokenError("expired")
    if not verify_secret(secret, row.secret_hash):
        raise TokenError("invalid_token")
    touch_last_used(db, row)
    return build_principal_for_user(db, row.user_id)
```

`get_current_user` 先尝试 JWT，再尝试 `parse_bearer_token`，两者都失败时按现有逻辑返回 401。

- [x] **步骤 4：运行测试验证通过**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_token_auth_principal.py backend/tests/test_browser_sessions.py -v`
预期：PASS（JWT 路径不回归）

- [x] **步骤 5：Commit**

```bash
git add backend/data/security/tokens.py backend/data/utils/helpers.py backend/tests/test_token_auth_principal.py
git commit -m "feat(auth): accept long lived bearer tokens for api access"
```

---

## 任务 5：令牌限流与审计

**文件：**
- 修改：`backend/data/services/api_tokens.py`、`backend/data/security/rate_limit.py`
- 测试：`backend/tests/test_api_tokens_api.py`（追加）

- [x] **步骤 1：编写失败的测试**

```python
def test_token_rate_limit_returns_429(client, admin_token_headers, set_token_rate_limit):
    set_token_rate_limit(limit=3)
    codes = [
        client.get("/api/v1/tokens/me", headers=admin_token_headers).status_code for _ in range(5)
    ]
    assert 429 in codes


def test_token_usage_is_audited(client, admin_token_headers, audit_events):
    client.get("/api/v1/batches", params={"workspace_id": 1}, headers=admin_token_headers)
    assert any(event["action"] == "api_token.use" for event in audit_events())
```

- [x] **步骤 2：运行测试验证失败**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_api_tokens_api.py -v -k "rate or audit"`
预期：FAIL

- [x] **步骤 3：编写最少实现代码**

```python
def enforce_token_rate_limit(token_id: int, *, limit_per_minute: int) -> None:
    key = f"token-rate:{token_id}:{int(time.time() // 60)}"
    count = redis_client.incr(key)
    if count == 1:
        redis_client.expire(key, 60)
    if count > limit_per_minute:
        raise TokenRateLimited(limit_per_minute)
```

审计事件 `api_token.use` 记录 `token_name / user_id / ip / ua / route`；创建、吊销、轮换分别写 `api_token.issue / revoke / rotate`。

- [x] **步骤 4：运行测试验证通过**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_api_tokens_api.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add backend/data/services/api_tokens.py backend/data/security/rate_limit.py backend/tests/test_api_tokens_api.py
git commit -m "feat(auth): rate limit and audit api token usage"
```

---

## 任务 6：离线清单扩展为包级行

**文件：**
- 修改：`backend/data/services/collection_tasks.py`、`backend/data/routers/collection_tasks.py`
- 测试：`backend/tests/test_collection_task_manifest_export.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_offline_manifest_rows_are_package_level(client, admin_headers, seeded_task_with_packages):
    response = client.get(
        f"/api/v1/collection-tasks/{seeded_task_with_packages}/offline-manifest",
        params={"workspace_id": 1, "format": "json"},
        headers=admin_headers,
    )
    item = response.json()["data"]["items"][0]
    for key in (
        "package_uid",
        "collection_project",
        "collection_task",
        "expected_files",
        "required_metadata",
        "upload_status",
        "desensitization_status",
        "manifest_revision",
    ):
        assert key in item
```

- [x] **步骤 2：运行测试验证失败**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_collection_task_manifest_export.py -v`
预期：FAIL（缺列）

- [x] **步骤 3：编写最少实现代码**

```python
def offline_manifest_rows(
    db: Session, *, task_id: int, workspace_id: int
) -> list[dict[str, object]]:
    task = _require_task(db, task_id=task_id, workspace_id=workspace_id)
    project = db.get(CollectionProject, task.collection_project_id)
    return [
        {
            "package_uid": package.package_uid,
            "collection_project": project.name if project else "",
            "collection_task": task.name,
            "modality": task.modality or "ego",
            "target_duration_hours": _decimal(package.target_duration_hours),
            "expected_files": "*.mcap,metadata.json",
            "required_metadata": "collector,device_sn,captured_at",
            "upload_status": package.status,
            "desensitization_status": _desensitization_status(package),
            "manifest_revision": _manifest_revision(task, package),
        }
        for package in task.packages
    ]
```

CSV 导出复用同一行结构（表头与 JSON 字段一一对应）。

- [x] **步骤 4：运行测试验证通过**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_collection_task_manifest_export.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add backend/data/services/collection_tasks.py backend/data/routers/collection_tasks.py \
  backend/tests/test_collection_task_manifest_export.py
git commit -m "feat(collection): export package level offline manifest"
```

---

## 任务 7：包级脱敏声明（v1 只记录）

**文件：**
- 修改：`backend/data/models/data_package.py`、`backend/data/services/collection_upload_sessions.py`、`routers/collection_upload_sessions.py`
- 测试：`backend/tests/test_collection_intake_desensitization.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_declaration_records_desensitization_but_does_not_gate(client, admin_headers, package_id):
    response = client.post(
        f"/api/v1/data-packages/{package_id}/desensitization-declaration",
        json={
            "workspace_id": 1,
            "status": "declared",
            "by": "张明",
            "tool": "duance",
            "policy_version": "v1",
        },
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["data"]["desensitization"]["status"] == "declared"


def test_undeclared_package_reports_unknown(client, admin_headers, package_id):
    detail = client.get(
        f"/api/v1/data-packages/{package_id}", params={"workspace_id": 1}, headers=admin_headers
    )
    assert detail.json()["data"]["desensitization"]["status"] == "unknown"
```

- [x] **步骤 2：运行测试验证失败**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_collection_intake_desensitization.py -v`
预期：FAIL（404）

- [x] **步骤 3：编写最少实现代码**

```python
def record_desensitization_declaration(
    db: Session, *, package_id: int, workspace_id: int, payload: dict
) -> DataPackage:
    package = _require_package(db, package_id=package_id, workspace_id=workspace_id)
    facts = dict(package.qrdf_facts_json or {})
    facts["desensitization"] = {
        "status": payload["status"],
        "by": payload.get("by") or "",
        "tool": payload.get("tool") or "",
        "at": datetime.utcnow().isoformat(),
        "policy_version": payload.get("policy_version") or "",
    }
    package.qrdf_facts_json = facts
    return package
```

读取侧：缺字段时返回 `{"status": "unknown"}`；工作空间配置 `require_desensitization_declaration` 默认 `False`，置 `True` 时审核端点拒绝未声明包（本任务只落配置读取与分支判断）。

- [x] **步骤 4：运行测试验证通过**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_collection_intake_desensitization.py backend/tests/test_collection_upload_intake_api.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add backend/data/models/data_package.py backend/data/services/collection_upload_sessions.py \
  backend/data/routers/collection_upload_sessions.py backend/tests/test_collection_intake_desensitization.py
git commit -m "feat(collection): record package level desensitization declarations"
```

---

## 任务 8：取数清单接口

**文件：**
- 创建：`backend/data/services/fetch_manifests.py`、`backend/data/routers/fetch_manifests.py`
- 修改：`backend/data/main.py`（挂载）
- 测试：`backend/tests/test_fetch_manifests_api.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_fetch_manifest_returns_signed_urls_for_package_episodes(
    client, admin_headers, package_with_episodes
):
    response = client.post(
        "/api/v1/fetch-manifests",
        json={
            "workspace_id": 1,
            "scope": "data_packages",
            "ids": [package_with_episodes],
            "include": ["objects", "episodes"],
        },
        headers=admin_headers,
    )
    payload = response.json()["data"]
    entry = payload["objects"][0]
    assert entry["episode_uid"]
    assert entry["sha256"] and entry["size"] > 0
    assert entry["url"].startswith(("http://", "https://"))
    assert payload["expires_at"]


def test_fetch_manifest_rejects_other_workspace(
    client, other_workspace_headers, package_with_episodes
):
    response = client.post(
        "/api/v1/fetch-manifests",
        json={"workspace_id": 999, "scope": "data_packages", "ids": [package_with_episodes]},
        headers=other_workspace_headers,
    )
    assert response.status_code in (403, 404)
```

- [x] **步骤 2：运行测试验证失败**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_fetch_manifests_api.py -v`
预期：FAIL（404）

- [x] **步骤 3：编写最少实现代码**

```python
def build_fetch_manifest(
    db: Session,
    *,
    workspace_id: int,
    scope: str,
    ids: list[int],
    actor_id: int,
    url_ttl_seconds: int,
) -> dict:
    episodes = _select_episodes(
        db, workspace_id=workspace_id, scope=scope, ids=ids, actor_id=actor_id
    )
    objects = []
    for episode in episodes:
        artifact = _readable_artifact(episode)
        objects.append(
            {
                "episode_uid": episode.episode_uid,
                "key": artifact.storage_uri,
                "size": artifact.size_bytes,
                "sha256": artifact.checksum_sha256,
                "mime": artifact.mime_type,
                "url": sign_object_url(artifact.storage_uri, ttl_seconds=url_ttl_seconds),
                "episode": _episode_metadata(episode),
            }
        )
    return {
        "manifest_version": _manifest_version(episodes),
        "expires_at": _expiry(url_ttl_seconds),
        "objects": objects,
    }
```

`scope="my_annotation_work_items"` 时只选该用户已领取/被指派工作项对应的 Episode。

- [x] **步骤 4：运行测试验证通过**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_fetch_manifests_api.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add backend/data/services/fetch_manifests.py backend/data/routers/fetch_manifests.py \
  backend/data/main.py backend/tests/test_fetch_manifests_api.py
git commit -m "feat(collection): add batch fetch manifests with signed urls"
```

---

## 任务 9：标注批量提交

**文件：**
- 修改：`backend/data/services/annotation_work_items.py`、`backend/data/routers/annotation_work_items.py`
- 测试：`backend/tests/test_annotation_batch_submit_api.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_batch_submit_is_idempotent_and_marks_source(client, admin_headers, assigned_work_item):
    body = {
        "workspace_id": 1,
        "client_request_id": "run-001",
        "items": [
            {
                "work_item_id": assigned_work_item,
                "payload": {
                    "mode": "partitioned",
                    "segments": [],
                    "outcome": "success",
                    "rating": 4,
                },
                "source": {
                    "kind": "algorithm",
                    "name": "ego-vl",
                    "version": "1.3.0",
                    "run_id": "run-001",
                    "confidence": 0.82,
                },
                "review_required": True,
            }
        ],
    }
    first = client.post(
        "/api/v1/annotation-work-items/batch-submit", json=body, headers=admin_headers
    )
    second = client.post(
        "/api/v1/annotation-work-items/batch-submit", json=body, headers=admin_headers
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["items"][0]["source"]["kind"] == "algorithm"
    assert first.json()["data"]["items"][0]["status"] == "submitted"


def test_review_required_false_is_downgraded_without_permission(
    client, annotator_headers, assigned_work_item
):
    body = {
        "workspace_id": 1,
        "client_request_id": "run-002",
        "items": [
            {
                "work_item_id": assigned_work_item,
                "payload": {"mode": "partitioned", "segments": []},
                "review_required": False,
            }
        ],
    }
    response = client.post(
        "/api/v1/annotation-work-items/batch-submit", json=body, headers=annotator_headers
    )
    assert response.json()["data"]["items"][0]["review_required"] is True
```

- [x] **步骤 2：运行测试验证失败**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_annotation_batch_submit_api.py -v`
预期：FAIL（404）

- [x] **步骤 3：编写最少实现代码**

```python
def batch_submit_annotation_items(
    db: Session, *, workspace_id: int, actor: User, client_request_id: str, items: list[dict]
) -> dict:
    existing = _load_idempotent_result(
        db, workspace_id=workspace_id, actor_id=actor.id, client_request_id=client_request_id
    )
    if existing is not None:
        return existing
    results = []
    for item in items:
        work_item = _require_assigned_item(
            db, item_id=item["work_item_id"], actor=actor, workspace_id=workspace_id
        )
        if work_item.status in {"submitted", "accepted"}:
            raise AnnotationWorkConflict("work_item_already_submitted")
        review_required = bool(item.get("review_required", True))
        if not review_required and not _can_skip_review(actor):
            review_required = True
            emit_audit_event(
                "annotation.review_skip_downgraded",
                actor=actor.email,
                resource=f"work_item:{work_item.id}",
            )
        results.append(
            _submit_item(
                db,
                work_item=work_item,
                payload=item["payload"],
                source=item.get("source"),
                review_required=review_required,
            )
        )
    snapshot = _store_idempotent_result(
        db,
        workspace_id=workspace_id,
        actor_id=actor.id,
        client_request_id=client_request_id,
        items=results,
    )
    return snapshot
```

- [x] **步骤 4：运行测试验证通过**

运行：`backend/.venv/bin/python -m pytest backend/tests/test_annotation_batch_submit_api.py backend/tests/test_annotation_work_items_api.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add backend/data/services/annotation_work_items.py backend/data/routers/annotation_work_items.py \
  backend/tests/test_annotation_batch_submit_api.py
git commit -m "feat(annotation): add idempotent batch submission for algorithms"
```

---

## 任务 10：旧链路清理

> **顺序调整（2026-09-22）**：`duance` 目前仍调用 `/batches`、`/duance-imports/*`、`/imports/*`，
> 按规格「先将保留能力接到新链路，再删除不可达实现」，任务 10 必须在任务 11–13（客户端切到令牌 + 数据包 + 上传会话）
> 落地之后再执行，否则会打断正在使用的离线工具。执行顺序：11 → 12 → 13 → 10。


**文件：**
- 修改：`backend/data/main.py`（下线路由）
- 删除：`backend/data/routers/batches.py`、`imports.py`、`duance_imports.py` 及其服务与旧契约测试
- 修改：`frontend/js/app.js`、`frontend/js/api.js`（移除导入入口与批次加载残留）
- 测试：`backend/tests/test_migrated_startup_scripts.py` 等旧语义测试替换为新契约

- [x] **步骤 1：先列调用者清单**

运行：`rg -n "batches.router|imports.router|duance_imports.router|createImportSession|listBatchImportSessions|openImportDialog" backend frontend`
预期：列出全部调用点；确认任务 3–9 已让新链路覆盖上传能力

- [x] **步骤 2：逐项切换调用者并删除实现**

保留能力（上传会话、直传、解析）已在新链路；删除不可达实现与旧契约测试，替换为新契约断言。

- [x] **步骤 3：运行回归**

运行：`make demo-check && backend/.venv/bin/python -m pytest backend/tests -q -k "collection or upload or annotation or storage"`
预期：全部通过；前端 22 个视图仍正常

- [x] **步骤 4：Commit**

```bash
git add -A
git commit -m "refactor(collection): retire legacy batch and import entry points"
```

提交：`b41ba9a`（后端删三类路由 + 前端去导入对话框/批次详情/遗留 LeRobot 入口 + 测试改契约）。

---

## 任务 11：duance 令牌凭据模式

工作目录：`~/Documents/devlop/quicrobot/duance/.worktrees/quicstudio-sk-upload`

**文件：**
- 创建：`src/duance/credentials.py`
- 修改：`src/duance/quicdata/client.py`、`src/duance/config.py`、`src/duance/ui/sync_dialog.py`
- 测试：`tests/test_token_credentials.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_token_mode_skips_password_login(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "credentials.db")
    store.save_token(name="duance-运维机", secret="qs_abc_secret", expires_at=None)
    client = QuicdataClient(base_url="http://127.0.0.1:8000", credentials=store)
    assert client.restore_credential() is True
    assert client.has_credential() is True
    assert "qs_abc_secret" not in caplog.text
```

- [x] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_token_credentials.py -v`
预期：FAIL（模块不存在）

- [x] **步骤 3：编写最少实现代码**

```python
class CredentialStore:
    """SQLite backed credential store kept in the user config directory."""

    def save_token(self, *, name: str, secret: str, expires_at: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO credentials(kind, name, secret, issued_at, expires_at) VALUES('token', ?, ?, ?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET name=excluded.name, secret=excluded.secret, expires_at=excluded.expires_at",
                (name, secret, datetime.utcnow().isoformat(), expires_at),
            )
```

`QuicdataClient` 增加 `restore_credential()`：优先令牌，其次 cookie 会话；密码登录保留。

- [x] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_token_credentials.py tests/test_client.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add src/duance/credentials.py src/duance/quicdata/client.py src/duance/config.py \
  src/duance/ui/sync_dialog.py tests/test_token_credentials.py
git commit -m "feat(sync): support long lived api tokens as credentials"
```

---

## 任务 12：duance 本地状态改用 SQLite

**文件：**
- 创建：`src/duance/services/state_store.py`
- 修改：`src/duance/services/sync_state.py`（迁移入口）
- 测试：`tests/test_state_store.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_state_store_migrates_sync_state_json(tmp_path):
    (tmp_path / ".duance").mkdir()
    (tmp_path / ".duance" / "sync-state.json").write_text(
        json.dumps(
            {
                "packages": {
                    "pkg-001": {
                        "status": "uploading",
                        "files": {"a.mcap": {"sha256": "x", "done": True}},
                    }
                }
            }
        )
    )
    store = StateStore.for_dataset(tmp_path)
    store.migrate_legacy_json()
    assert store.package("pkg-001")["status"] == "uploading"
    assert store.pending_files("pkg-001") == []


def test_state_store_tracks_pending_files(tmp_path):
    store = StateStore.for_dataset(tmp_path)
    store.upsert_file(
        package_uid="pkg-002",
        episode_slug="ep-01",
        rel_path="pkg-002/ep-01/data.mcap",
        size=10,
        sha256="abc",
        upload_state="pending",
    )
    assert [row["rel_path"] for row in store.pending_files("pkg-002")] == [
        "pkg-002/ep-01/data.mcap"
    ]
```

- [x] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_state_store.py -v`
预期：FAIL（模块不存在）

- [x] **步骤 3：编写最少实现代码**

```python
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS packages(package_uid TEXT PRIMARY KEY, task_id INTEGER, project_id INTEGER,
  target_hours REAL, modality TEXT, status TEXT, manifest_revision TEXT);
CREATE TABLE IF NOT EXISTS episode_files(id INTEGER PRIMARY KEY, package_uid TEXT NOT NULL, episode_slug TEXT NOT NULL,
  rel_path TEXT NOT NULL UNIQUE, size INTEGER, sha256 TEXT, upload_state TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS upload_attempts(id INTEGER PRIMARY KEY, package_uid TEXT NOT NULL, session_id TEXT,
  part_state TEXT, updated_at TEXT NOT NULL);
"""
```

在可移动介质上按目录探测把 `journal_mode` 降级为 `TRUNCATE`；`meta.schema_version` 控制升级。

- [x] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_state_store.py tests/test_sync_state.py -v`
预期：PASS

- [x] **步骤 5：Commit**

```bash
git add src/duance/services/state_store.py src/duance/services/sync_state.py tests/test_state_store.py
git commit -m "refactor(sync): store offline binding state in sqlite"
```

---

## 任务 13：duance 同步目标切到数据包 + 离线绑定

> **拆分（2026-09-22）**：13a 离线绑定与同步计划逻辑（`package_sync.py`，已完成并提交 `134f16a`）
> · 13b-1 客户端 API 层（已完成并提交 `0f8bcf9`：采集项目/采集任务/数据包列表 + 上传会话 + 逐 Episode 声明 + OSS multipart + 脱敏声明）。
> · 13b-2a 上传编排（已完成并提交 `3b510e1`：`CollectionSyncCoordinator` 一会话一包、先声明后上传、逐文件 multipart、脱敏声明、状态落库）。
> · 13b-2b QRDF 声明映射（已完成并提交 `26ee51c`：复用 `direct_source_for_episode`，产出 source/metadata_text/data_file，并按绑定 slug 组装包级声明与文件清单）。
> · 13b-2b-2 数据集绑定（已完成并提交 `11e245e`：按 `<root>/<package_uid>/<episode>/` 约定扫描并登记文件，空包给出体检结论）。
> · 13b-2c 同步窗口改造（已完成并提交 `6cae386`）：`sync_dialog.py` 的 `batch_combo` → 采集任务、`task_set_combo` → 采集项目，新增数据包多选 + 绑定体检展示；`_load_batches/_present_batches` 换成采集项目/任务/数据包级联；上传 worker 由 `QuicdataSyncCoordinator.upload_episode`（逐 Episode、Batch、duance-imports）改为 `declarations_for_package` + `CollectionSyncCoordinator.upload_package`（逐包、上传会话）；`QuicdataPreferences.batch_id/task_label_id` → `collection_project_id/collection_task_id`（`AppConfig.from_dict` 容忍旧键）；`tests/test_sync_dialog.py` 的 Fake client 同步替换；README「批量同步到已有 Batch」段落改写。
> 13b 完成前不得执行任务 10 的后端路由删除。


**文件：**
- 修改：`src/duance/services/quicdata_sync.py`、`src/duance/ui/sync_dialog.py`
- 测试：`tests/test_quicdata_sync_packages.py`

- [x] **步骤 1：编写失败的测试**

```python
def test_sync_plan_groups_files_by_package(tmp_path, state_store):
    _write_episode(tmp_path, "pkg-001", "ep-01")
    _write_episode(tmp_path, "pkg-001", "ep-02")
    plan = build_sync_plan(dataset_root=tmp_path, state=state_store, selected_packages=["pkg-001"])
    assert plan["pkg-001"]["declarations"][0]["package_uid"] == "pkg-001"
    assert len(plan["pkg-001"]["declarations"]) == 2


def test_missing_required_metadata_blocks_package(tmp_path, state_store):
    _write_episode(tmp_path, "pkg-002", "ep-01", metadata={"collector": "", "device_sn": ""})
    report = binding_report(dataset_root=tmp_path, state=state_store, package_uid="pkg-002")
    assert "missing_metadata" in report["problems"]
```

- [x] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_quicdata_sync_packages.py -v`
预期：FAIL

- [x] **步骤 3：编写最少实现代码**

```python
def build_sync_plan(*, dataset_root: Path, state: StateStore, selected_packages: list[str]) -> dict:
    plan = {}
    for package_uid in selected_packages:
        groups = collect_episode_groups(dataset_root, package_uid)
        plan[package_uid] = {
            "declarations": [
                {"package_uid": package_uid, "episode_slug": g.slug, "files": g.files}
                for g in groups
            ],
            "desensitization": state.package(package_uid).get(
                "desensitization", {"status": "unknown"}
            ),
        }
    return plan
```

UI：同步窗口去掉任务集与 Batch，改为「工作区 → 采集项目 → 采集任务 → 数据包（多选）」；导入平台离线清单后展示绑定体检（未绑定文件 / 缺元数据 / 缺脱敏声明 / revision 不一致）。

- [x] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_quicdata_sync_packages.py tests/test_sync_dialog.py -v`
预期：PASS

- [x] **步骤 5：Commit**

提交：`134f16a` 离线绑定 · `0f8bcf9` 客户端 API · `3b510e1` 上传编排 · `26ee51c` 声明映射 · `11e245e` 数据集绑定 · `6cae386` 同步窗口。

---

## 任务 14：端到端与验收

**文件：**
- 创建：`backend/tests/test_external_tool_e2e.py`
- 修改：`docs/superpowers/specs/2026-09-21-quicstudio-external-sk-access-design.md`（勾选验收结果）

- [x] **步骤 1：编写失败测试**

```python
def test_offline_upload_then_fetch_then_batch_annotate(
    client, admin_token_headers, minio_available
):
    # 1) 导出离线清单  2) 建会话 + 声明（按包）  3) OSS multipart 直传
    # 4) 解析 → 待入库审核  5) 取数清单签名 URL 可下载且 sha256 一致
    # 6) 批量提交标注（幂等）  7) review_required=False 无权限时降级
    ...
```

- [x] **步骤 2：运行验证（需真实 MinIO）**

运行：`TEST_DATABASE_URL=... TEST_REDIS_URL=... backend/.venv/bin/python -m pytest backend/tests/test_external_tool_e2e.py -v`
预期：PASS；报告区分 mock / MinIO / 真实阿里云（未执行的阿里云 smoke 不计通过）

- [x] **步骤 3：前端与客户端回归**

运行：`make demo-check && node --test frontend/tests/*.test.mjs`（平台）与 `uv run pytest -q`（duance worktree）
预期：全部通过

- [x] **步骤 4：Commit**

证据：`test_external_tool_e2e.py` 覆盖令牌签发 → 离线清单（JSON+CSV）→ 上传会话/声明 → 真实 MinIO 分片 → 解析 → QRDF admission → 入库审核 → 取数清单（对象 sha256/size）→ 批量标注幂等 → `review_required` 降级审计；
同时修掉三处链路缺陷：duance 调用了不存在的 `/collection-upload-sessions`（应为 `/upload-sessions`）、duance 用本地摘要冒充平台 opaque source_id、批量提交把保留键写进 draft 契约导致包级提交必然 422；
并为已验证的上传对象补写 `raw_source` artifact，使取数清单对外部工具真正可用。


---

## 自检

1. **规格覆盖度**：规格 §4 凭据 → 任务 2/3/4/5；§5 上传 → 任务 6/7；§6 取数 → 任务 8；§7 标注 → 任务 9；§8 客户端 → 任务 11/12/13；§9 清理 → 任务 1/10；§11 验收 → 任务 14。无遗漏章节。
2. **占位符扫描**：仅任务 14 的端到端测试正文用注释列出步骤（需要真实 MinIO 与多服务协同，步骤在实现时展开）；其余任务均给出可执行代码与断言。
3. **类型一致性**：`ApiToken` / `parse_bearer_token` / `build_fetch_manifest` / `batch_submit_annotation_items` / `StateStore` / `CredentialStore` 在后续任务中均沿用同一命名与签名。
