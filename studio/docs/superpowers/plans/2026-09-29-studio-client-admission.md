# Studio 客户端预检入库（client_admission）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Duance 声明时附带本地校验报告与 preview 文件清单，preview 文件直传 process 桶；Studio 的 admission worker 在客户端预检模式下不下载 MCAP，只 HEAD 对象、按服务端规则重新判定 issue、下载并验证 preview 小文件，写入 `integrity_source="client"` 的 admission 事实；任何无法使用客户端结果的情况回退到完整服务端模式并记录原因。

**Architecture:** 先钉死 Duance↔Studio 接口契约（Task 1：Pydantic 契约模型 + `docs/COLLECTION_UPLOAD_API.md` 中带名字的 JSON 示例，测试直接解析文档示例校验模型，文档与代码不能漂移）。之后沿数据流逐段实现：声明分配 preview `file_id`（Task 2）→ preview 分片上传到 process 桶并对 `Content-MD5` 签名（Task 3）→ admission 事实新增 `integrity_source` 并对客户端可见（Task 4）→ 服务端重新判定与解析状态（Task 5）→ worker 客户端预检模式与回退（Task 6）→ HTTP 真实字节端到端（Task 7）。服务端规则集中在 `data/services/client_admission.py`，worker 模式集中在 `data/integrations/qrdf/client_admission.py`。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、SQLAlchemy、Alembic、PostgreSQL、pytest、qrdf SDK 0.2.1（vendored，`backend/vendor/qrdf`）。

**Spec:** `docs/superpowers/specs/2026-09-28-client-precheck-preview-design.md`。本计划是该 spec 的第 2 个实现计划，实现 §4.1、§4.2、§4.3、§4.4、`integrity_source` 字段、配置 `accept_client_admission`，以及 §8/§9/§10 中 Studio 的部分。计划 1（`docs/superpowers/plans/2026-09-28-studio-episode-object-manifest.md`，已完成）提供对象清单模块 `backend/data/services/episode_objects.py`；计划 3（Duance）只依据本计划 Task 1 写入 `docs/COLLECTION_UPLOAD_API.md` 的“客户端预检”一节编写。

## Global Constraints

- 1.0 开发阶段：不回填、不兼容历史数据；新 alembic 迁移 `0010_admission_integrity_source` 接在当前 head `0009_drop_admission_process_ref` 之后，旧行取服务端默认值 `server`。
- 不带 `client_admission` 的声明与现在完全一致：请求、响应（不出现 `preview_files` 键）、存储的声明、上传与 admission 行为都不变。
- 服务端从不采用客户端的 `report.ok`；只按 `non_blocking_issue_codes`（即 `TRAINING_READINESS_ISSUE_CODES`，同一个 frozenset 对象）重新判定：存在 `severity == "ERROR"` 且 `code` 不在该表中的 issue 即 `integrity_status="failed"`。
- 客户端预检模式从不下载 MCAP（raw 对象只 HEAD）；只下载 `media/preview/` 下声明的文件。
- 服务端判定 `integrity_status="failed"` 时不回退；其余无法使用客户端结果的情况都回退到完整服务端模式，原因写入报告 `client_admission_fallback`。
- `integrity_source` 取值只有 `client`、`server`；服务端模式（含回退）一律写 `server`。
- 配置：`accept_client_admission: bool = True`、`client_admission_qrdf_versions = ["0.2.1"]`、`client_admission_policy_versions = ["v1"]`（环境变量 `ACCEPT_CLIENT_ADMISSION` 等，列表为 JSON）。
- `qrdf_version` 指 qrdf SDK 包版本（`qrdf.__version__`），不是 metadata 中的数据格式版本。
- preview 文件只能通过 `oss_multipart` 会话上传；preview 对象 key 由服务端生成，从不返回给客户端。
- 测试环境（在 `backend/` 目录运行）：

  ```bash
  cd backend && source ../.superpowers/sdd/test-env.sh && ../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/<file> -q -p no:cacheprovider
  ```

  下文 “Run:” 均指在 `backend/` 下、已 `source ../.superpowers/sdd/test-env.sh` 后运行。
- 全量门禁：全量失败集合必须是基线 `.superpowers/sdd/baseline-full.txt`（计划开始时建立，现有 55 个历史失败）的子集。
- 仓库有 ruff-format pre-commit 钩子会格式化 markdown 中的 Python 代码块；本计划中的 Python 代码块都是完整语句。

## Review Focus

- Duance 在 preview 上传中途崩溃后重启：用同一个 `file_id` 重新 `oss/init` 应返回相同分片参数、不新建 multipart（Task 3 `test_preview_init_is_resumable_with_the_same_file_id`）。
- Duance 因网络超时重发同一声明：`source_id`、`file_id`、服务端生成的 preview 对象 key 都不变（Task 2 `test_replayed_declaration_keeps_file_ids_and_object_keys`）。
- 客户端报告 `ok=true` 却含阻断 code（例如新版 qrdf 新增的 ERROR code）：服务端判 failed、不回退、不下载任何对象（Task 5 判定单测 + Task 6 `test_client_ok_with_blocking_code_fails_without_fallback`）。
- 客户端 metadata 文本与生成 preview 时的字节不同（换行/缩进被改写）：preview 指纹失配应回退服务端模式而不是判 failed（Task 6 回退参数化用例 `restyle_metadata`）。
- 旧 Duance 的 MCAP 分片不带 `content_md5`：签名与上传行为不变；preview 分片缺 `content_md5` 必须被拒（Task 3 `test_mcap_parts_sign_without_md5_but_preview_parts_require_it`）。

## 文件总览

| 文件 | 责任 | 任务 |
| --- | --- | --- |
| `backend/data/schemas/client_admission.py`（新建） | 契约的 Pydantic 请求/响应模型 | 1 |
| `backend/data/services/client_admission.py`（新建） | 服务端规则：能力、声明规范化、`file_id`/对象 key、报告重新判定、解析状态 | 1、2、5 |
| `backend/data/config.py` | 三个新配置项 | 1 |
| `backend/data/integrations/qrdf/admission.py` | `TRAINING_READINESS_ISSUE_CODES` 改为引用服务模块；`_preview_artifacts_valid(expected_source=)`；`admit_qrdf_episode(report_extra=)`；worker 分派客户端模式 | 1、6 |
| `backend/data/routers/collection_upload_sessions.py` | capabilities 路由；声明字段；multipart `file_id`/`content_md5` | 1、2、3 |
| `backend/data/services/collection_upload_intake.py` | 声明存储与响应；preview multipart；会话完成门禁 | 2、3 |
| `backend/data/infra/oss_client.py`、`object_storage.py`、`aliyun_object_storage.py`、`s3_object_storage.py` | process 桶 preview key 白名单；`Content-MD5` 签名 | 3 |
| `backend/alembic/versions/0010_admission_integrity_source.py`（新建）、`backend/data/models/episode_admission.py`、`backend/data/services/episode_admission.py` | `integrity_source` 列与写入 | 4 |
| `backend/data/routers/collection_packages.py`、`backend/data/services/collection_upload_parse.py` | 结果对客户端可见；解析把 `client_admission` 带入来源状态 | 4、5 |
| `backend/data/integrations/qrdf/client_admission.py`（新建） | worker 客户端预检模式 | 6 |
| `docs/COLLECTION_UPLOAD_API.md` | 契约文档（计划 3 的唯一依据） | 1 |
| 测试：`test_client_admission_contract.py`、`test_client_admission_declaration.py`、`test_client_admission_uploads.py`、`test_client_admission_worker.py`（新建），及 `test_oss_browser_multipart.py`、`test_aliyun_part_signing.py`、`test_s3_object_storage.py`、`test_episode_admission.py`、`test_collection_admission_worker.py`、`test_qrdf_admission.py`、`test_collection_upload_bytes.py` | | 各任务 |

---

### Task 1: Duance↔Studio 接口契约（文档、契约模型、capabilities）

**Files:**
- Create: `backend/data/schemas/client_admission.py`
- Create: `backend/data/services/client_admission.py`
- Modify: `backend/data/config.py`（`import_direct_upload_concurrency` 之后加三个配置项）
- Modify: `backend/data/integrations/qrdf/admission.py:31-41`（`TRAINING_READINESS_ISSUE_CODES` 改为引用服务模块）
- Modify: `backend/data/routers/collection_upload_sessions.py`（新增 `GET /upload-sessions/capabilities`，必须定义在 `GET /{upload_session_id}` 之前）
- Modify: `docs/COLLECTION_UPLOAD_API.md`（新增“客户端预检（client_admission）”一节）
- Test: `backend/tests/test_client_admission_contract.py`

**Interfaces:**
- Produces（`data.schemas.client_admission`）：`SHA256_PATTERN`、`CONTENT_MD5_PATTERN`（`r"^[A-Za-z0-9+/]{22}==$"`）、`ClientAdmissionCapabilities`、`UploadCapabilitiesResponse`、`ClientAdmissionIssue`、`ClientAdmissionReport`、`ClientAdmissionFile`、`ClientAdmissionRequest`、`DeclaredPreviewFile`、`DeclaredSource`、`DeclarationResponse`、`SignedPartResponse`、`SourceAdmissionStatus`。
- Produces（`data.services.client_admission`）：`NON_BLOCKING_ISSUE_CODES: frozenset[str]`、`client_admission_capabilities() -> dict[str, object]`（键 `enabled`、`accepted_qrdf_versions`、`accepted_policy_versions`、`non_blocking_issue_codes`，后三者为 list，codes 升序）。
- Produces（config）：`settings.accept_client_admission: bool`、`settings.client_admission_qrdf_versions: list[str]`、`settings.client_admission_policy_versions: list[str]`。
- Produces（测试辅助）：`tests.test_client_admission_contract.doc_examples() -> dict[str, Any]`，按名字返回文档中 ` ```json <name>` 代码块解析后的 JSON。
- Produces（HTTP）：`GET /api/v1/upload-sessions/capabilities?workspace_id=` → `data = {"client_admission": {...}}`。

- [ ] **Step 1: 写失败测试** `backend/tests/test_client_admission_contract.py`：

```python
"""Duance <-> Studio client-admission contract: documented examples match the schemas."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.collection_api_fixtures import make_workspace

from data.config import settings
from data.integrations.qrdf.admission import TRAINING_READINESS_ISSUE_CODES
from data.schemas.client_admission import (
    ClientAdmissionRequest,
    DeclarationResponse,
    SignedPartResponse,
    SourceAdmissionStatus,
    UploadCapabilitiesResponse,
)
from data.services.client_admission import (
    NON_BLOCKING_ISSUE_CODES,
    client_admission_capabilities,
)

DOC = Path(__file__).resolve().parents[2] / "docs" / "COLLECTION_UPLOAD_API.md"
_EXAMPLE = re.compile(r"```json ([a-z0-9-]+)\n(.*?)\n```", re.S)


def doc_examples() -> dict[str, Any]:
    text = DOC.read_text(encoding="utf-8")
    return {name: json.loads(body) for name, body in _EXAMPLE.findall(text)}


def test_documented_examples_match_the_contract_schemas():
    examples = doc_examples()
    UploadCapabilitiesResponse.model_validate(examples["capabilities-response"])
    ClientAdmissionRequest.model_validate(examples["declaration-item"]["client_admission"])
    DeclarationResponse.model_validate(examples["declaration-response"])
    SignedPartResponse.model_validate(examples["sign-part-response"])
    for item in examples["package-source-admission"]:
        SourceAdmissionStatus.model_validate(item)


def test_non_blocking_codes_are_the_training_readiness_codes():
    assert TRAINING_READINESS_ISSUE_CODES is NON_BLOCKING_ISSUE_CODES
    assert NON_BLOCKING_ISSUE_CODES == {
        "NO_VALID_TRAINING_FRAMES",
        "NO_VALID_EGO_FRAMES",
        "NO_STATE_TOPIC",
        "NO_ACTION_TOPIC",
    }


def test_capabilities_follow_configuration(monkeypatch):
    monkeypatch.setattr(settings, "accept_client_admission", False)
    assert client_admission_capabilities() == {
        "enabled": False,
        "accepted_qrdf_versions": ["0.2.1"],
        "accepted_policy_versions": ["v1"],
        "non_blocking_issue_codes": sorted(NON_BLOCKING_ISSUE_CODES),
    }


def test_capabilities_endpoint_is_not_shadowed_by_session_lookup(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    response = client.get(
        "/api/v1/upload-sessions/capabilities",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = UploadCapabilitiesResponse.model_validate(response.json()["data"])
    assert body.client_admission.enabled is True
    assert body.client_admission.non_blocking_issue_codes == sorted(NON_BLOCKING_ISSUE_CODES)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.pop("report"),
        lambda c: c.pop("qrdf_version"),
        lambda c: c["report"].pop("media_validation"),
        lambda c: c["report"].pop("issues"),
        lambda c: c["files"][0].update(sha256="ABC"),
        lambda c: c["files"][0].update(size_bytes=0),
        lambda c: c["files"][0].update(extra=True),
        lambda c: c["report"]["issues"].append({"severity": "FATAL", "code": "X"}),
        lambda c: c["report"]["issues"].append({"severity": "ERROR"}),
        lambda c: c.update(extra=1),
    ],
)
def test_client_admission_request_rejects_malformed_envelopes(mutate):
    item = doc_examples()["declaration-item"]["client_admission"]
    mutate(item)
    with pytest.raises(ValidationError):
        ClientAdmissionRequest.model_validate(item)
```

- [ ] **Step 2: 运行，确认失败**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_contract.py -q -p no:cacheprovider`
Expected: 收集阶段 FAIL，`ModuleNotFoundError: No module named 'data.schemas.client_admission'`。

- [ ] **Step 3: 契约模型** `backend/data/schemas/client_admission.py`：

```python
"""Duance <-> Studio client-admission contract.

Request models are enforced by the upload-session router; response models
describe the JSON the router returns.  ``docs/COLLECTION_UPLOAD_API.md`` holds
one named example for each shape and the contract tests validate them here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SHA256_PATTERN = r"^[0-9a-f]{64}$"
CONTENT_MD5_PATTERN = r"^[A-Za-z0-9+/]{22}==$"


class ClientAdmissionCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    accepted_qrdf_versions: list[str]
    accepted_policy_versions: list[str]
    non_blocking_issue_codes: list[str]


class UploadCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_admission: ClientAdmissionCapabilities


class ClientAdmissionIssue(BaseModel):
    """One QRDF ValidationIssue as the client serialized it."""

    model_config = ConfigDict(extra="allow")

    severity: Literal["ERROR", "WARNING", "INFO"]
    code: str = Field(min_length=1, max_length=128)
    message: str | None = Field(default=None, max_length=4096)
    path: str | None = Field(default=None, max_length=1024)
    topic: str | None = Field(default=None, max_length=512)


class ClientAdmissionReport(BaseModel):
    """The client's validation report; ``ok`` is recorded but never trusted."""

    model_config = ConfigDict(extra="allow")

    ok: bool
    issues: list[ClientAdmissionIssue] = Field(max_length=1000)
    media_validation: dict[str, Any] | None


class ClientAdmissionFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=SHA256_PATTERN)


class ClientAdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qrdf_version: str = Field(min_length=1, max_length=32)
    policy_version: str = Field(min_length=1, max_length=32)
    report: ClientAdmissionReport
    files: list[ClientAdmissionFile] = Field(max_length=256)


class DeclaredPreviewFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(pattern=SHA256_PATTERN)
    path: str
    size_bytes: int = Field(gt=0)


class DeclaredSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=SHA256_PATTERN)
    package_uid: str
    episode_id: str
    preview_files: list[DeclaredPreviewFile] | None = None


class DeclarationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    declared_packages: int
    declared_sources: int
    sources: list[DeclaredSource]


class SignedPartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    part_number: int = Field(ge=1)
    content_length: int = Field(gt=0)
    method: Literal["PUT"]
    url: str
    expires_in: int
    headers: dict[str, str]


class SourceAdmissionStatus(BaseModel):
    """One entry of ``qrdf_facts.source_admission.sources`` on package detail."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=SHA256_PATTERN)
    episode_id: int | None = None
    status: Literal["queued", "running", "ready", "failed"]
    attempt: int | None = None
    error_code: str | None = None
    integrity_source: Literal["client", "server"] | None = None
    client_admission_fallback: str | None = None
```

- [ ] **Step 4: 服务模块（本任务部分）** `backend/data/services/client_admission.py`：

```python
"""Server rules for Duance client admission.

Capabilities, declaration normalization, preview file identities and the
server's own re-judgement of a client report live here so the router, the
upload intake, the parse job and the admission worker share one definition.
"""

from __future__ import annotations

from data.config import settings

# Issue codes that never block intake: frame alignment and robot channel
# readiness are deferred to batch-build QC.  This is the one table both the
# client (via capabilities) and the server use to decide integrity.
NON_BLOCKING_ISSUE_CODES = frozenset(
    {
        "NO_VALID_TRAINING_FRAMES",
        "NO_VALID_EGO_FRAMES",
        "NO_STATE_TOPIC",
        "NO_ACTION_TOPIC",
    }
)


def client_admission_capabilities() -> dict[str, object]:
    """The ``client_admission`` block of the upload capabilities response."""
    return {
        "enabled": bool(settings.accept_client_admission),
        "accepted_qrdf_versions": list(settings.client_admission_qrdf_versions),
        "accepted_policy_versions": list(settings.client_admission_policy_versions),
        "non_blocking_issue_codes": sorted(NON_BLOCKING_ISSUE_CODES),
    }
```

- [ ] **Step 5: admission 改为引用同一个集合**。`backend/data/integrations/qrdf/admission.py` 中把现有的

```python
TRAINING_READINESS_ISSUE_CODES = frozenset(
    {
        # Deferred to batch-build QC: frame alignment and robot channel readiness
        # must not block intake (integrity only proves the preview is generatable).
        "NO_VALID_TRAINING_FRAMES",
        "NO_VALID_EGO_FRAMES",
        "NO_STATE_TOPIC",
        "NO_ACTION_TOPIC",
    }
)
```

替换为（`from data.services.client_admission import NON_BLOCKING_ISSUE_CODES` 放到文件顶部 import 区 `from data.infra.object_storage import StorageObjectRef` 之后）：

```python
# Deferred to batch-build QC: frame alignment and robot channel readiness must
# not block intake.  The same set is advertised to clients as
# ``non_blocking_issue_codes``.
TRAINING_READINESS_ISSUE_CODES = NON_BLOCKING_ISSUE_CODES
```

`data.services.client_admission` 只依赖 `data.config`，不会形成循环导入。

- [ ] **Step 6: 配置**。`backend/data/config.py` 中 `import_direct_upload_concurrency: int = 4` 下一行加入：

```python
    # Duance client admission (local QRDF validation and preview generation).
    # When disabled every source is admitted by the full server path.
    accept_client_admission: bool = True
    client_admission_qrdf_versions: list[str] = Field(default_factory=lambda: ["0.2.1"])
    client_admission_policy_versions: list[str] = Field(default_factory=lambda: ["v1"])
```

- [ ] **Step 7: capabilities 路由**。`backend/data/routers/collection_upload_sessions.py` 顶部 import 区加 `from data.services.client_admission import client_admission_capabilities`；在 `list_upload_sessions` 函数之后、`@router.get("/{upload_session_id}")` 之前插入：

```python
@router.get("/capabilities")
def upload_capabilities(
    workspace_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Advertise what this deployment accepts from upload clients."""
    require_permission(user, "episode:read")
    _require_workspace(db, user=user, workspace_id=workspace_id)
    return success({"client_admission": client_admission_capabilities()})
```

- [ ] **Step 8: 契约文档**。`docs/COLLECTION_UPLOAD_API.md`：
  1. “## OSS 直传的范围”一节末尾追加一段：`preview 文件也走这三个接口，用 file_id 代替 source_id，见下文“客户端预检（client_admission）”。`
  2. 在“## 取消与审核”之前插入下面整节（JSON 代码块的语言标记后跟示例名，契约测试按名字解析，不要改名）：

````markdown
## 客户端预检（client_admission）

Duance 可以在本地完成 QRDF 结构与媒体校验、生成 preview，然后随声明提交 `client_admission`，
把 preview 文件直传 process 桶。Studio 在客户端预检模式下**不下载 MCAP**：只 HEAD 对象、
按服务端自己的规则重新判定 issue、下载并验证 preview 小文件。无法使用客户端结果时自动改走
完整服务端链路。**不带 `client_admission` 的声明与之前完全一致。**

### 1. 能力协商

`GET /upload-sessions/capabilities?workspace_id={workspace_id}`（`episode:read` 权限与工作空间成员关系）。
响应 `data`（外层仍是 `{"code": 200, "message": "Success", "data": ...}`）：

```json capabilities-response
{
  "client_admission": {
    "enabled": true,
    "accepted_qrdf_versions": ["0.2.1"],
    "accepted_policy_versions": ["v1"],
    "non_blocking_issue_codes": ["NO_ACTION_TOPIC", "NO_STATE_TOPIC", "NO_VALID_EGO_FRAMES", "NO_VALID_TRAINING_FRAMES"]
  }
}
```

- `enabled=false`（部署配置 `ACCEPT_CLIENT_ADMISSION=false`）：客户端不做本地预检，声明不带 `client_admission`。
- `accepted_qrdf_versions` 是 qrdf **SDK 包版本**（`qrdf.__version__`），不是 metadata 里的数据格式版本 `qrdf_version`（`0.2.0`）。
  客户端 SDK 版本不在列表中时走旧链路。
- `accepted_policy_versions`：客户端所用校验策略版本，当前只有 `v1`。
- `non_blocking_issue_codes` 是阻断判定的唯一依据：任一 issue `severity == "ERROR"` 且 `code` 不在表中，
  该 episode 有阻断错误，不应上传。服务端用同一张表重新判定，从不采用客户端的 `ok`。
- 旧版 Studio 没有此路由，`GET /upload-sessions/capabilities` 会被当作会话查询返回 404；客户端把 404 视为不支持。

### 2. 声明

`POST /upload-sessions/{id}/declarations` 的 `items[]` 每项可带可选字段 `client_admission`：

```json declaration-item
{
  "package_uid": "PKG-20260929-0001",
  "source": {
    "episode_id": "episode_000001",
    "start_ns": "1759111200000000000",
    "end_ns": "1759111800000000000",
    "metadata_sha256": "45447b7afbd5e544f7d0f1df0fccd26014d9850130abd3f020b89ff96b82079f",
    "data_mcap_sha256": "d3690131c737f212505f230d86ea322e788db598fd0162f5500f7e22ab7005bc"
  },
  "metadata_text": "{\"qrdf_version\": \"0.2.0\", \"episode_id\": \"episode_000001\", \"data_file\": \"data.mcap\"}",
  "data_file": {
    "path": "data.mcap",
    "size_bytes": 3188624643,
    "sha256": "d3690131c737f212505f230d86ea322e788db598fd0162f5500f7e22ab7005bc"
  },
  "client_admission": {
    "qrdf_version": "0.2.1",
    "policy_version": "v1",
    "report": {
      "ok": true,
      "issues": [
        {"severity": "ERROR", "code": "NO_ACTION_TOPIC", "message": "no action topic registered", "path": null, "topic": null}
      ],
      "media_validation": {"ok": true, "issues": []}
    },
    "files": [
      {"path": "media/preview/manifest.json", "size_bytes": 812, "sha256": "05b3abf2579a5eb66403cd78be557fd860633a1fe2103c7642030defe32c657f"},
      {"path": "media/preview/generations/3c1f0e9a2b7d4c55/head_rgb.mp4", "size_bytes": 154235611, "sha256": "862c4ec62defaadafbf7638961214d286015c38ed4ccc7b10d52bdf434e5bee1"},
      {"path": "media/preview/generations/3c1f0e9a2b7d4c55/head_rgb.timeline.json", "size_bytes": 902113, "sha256": "94d192b3a326be1f019b71ef13ea5a367ffe939c5e9a88f1b270e53753d9569a"}
    ]
  }
}
```

| 字段 | 规则 |
| --- | --- |
| `qrdf_version` | 客户端 qrdf SDK 版本，1–32 字符 |
| `policy_version` | 校验策略版本，1–32 字符 |
| `report.ok` | 必填布尔；只记录，不参与判定 |
| `report.issues[]` | 最多 1000 项；每项必填 `severity`（`ERROR`/`WARNING`/`INFO`）与 `code`（1–128 字符），可选 `message`、`path`、`topic`；允许其他字段 |
| `report.media_validation` | 必填，对象或 `null`（媒体校验报告原样） |
| `report` 整体 | JSON 序列化后不超过 1 MiB；允许其他字段，原样保存 |
| `files[]` | 最多 256 项；非 RGB episode 可为空数组；非空时必须包含 `media/preview/manifest.json` |
| `files[].path` | episode 目录内相对路径，必须以 `media/preview/` 开头；按 `/` 分段，每段匹配 `[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}`；同一来源内不能重复 |
| `files[].size_bytes` | 正整数，必须等于上传字节数 |
| `files[].sha256` | 64 位小写十六进制 |

`client_admission` 只允许在 `upload_mode=oss_multipart` 的会话中使用。

响应 `data`：

```json declaration-response
{
  "id": "0b7c6f5e-1a2b-4c3d-8e9f-001122334455",
  "status": "init",
  "declared_packages": 1,
  "declared_sources": 1,
  "sources": [
    {
      "source_id": "5d41402abc4b2a76b9719d911017c5925d41402abc4b2a76b9719d911017c592",
      "package_uid": "PKG-20260929-0001",
      "episode_id": "episode_000001",
      "preview_files": [
        {"file_id": "cb12586399787bab0f86ec8cdf8a091e1d44da02d99379c9c95746d9825f7886", "path": "media/preview/generations/3c1f0e9a2b7d4c55/head_rgb.mp4", "size_bytes": 154235611},
        {"file_id": "02ce403b8fd16d8937b6141809d361a2c7389ca3ef1e64737a11b2f56b925e58", "path": "media/preview/generations/3c1f0e9a2b7d4c55/head_rgb.timeline.json", "size_bytes": 902113},
        {"file_id": "fb5994d098e7489a7b11c8a5498139f66c4fa4dd3d174eaa8c40710194f99c1f", "path": "media/preview/manifest.json", "size_bytes": 812}
      ]
    }
  ]
}
```

- `preview_files` 只出现在声明了 `client_admission` 的来源上（`files` 为空时是空数组），按 `path` 升序；未声明时响应中没有该键。
- `file_id = sha256("{source_id}:{path}")` 的小写十六进制，由服务端生成。客户端必须使用响应中的值，不要自行计算。
- `source_id` 与之前相同，仍用于上传该来源的 MCAP；每个 preview 文件用自己的 `file_id` 上传。
- 完全相同的声明可以重放，返回相同的 `source_id` 与 `file_id`。内容（含 `client_admission`）有任何变化返回 409
  `package_declarations_conflict`，需要取消会话后新建。

### 3. 声明时拒绝与 worker 回退

声明时拒绝（整批声明不生效，可修正后重发）：

| 情况 | HTTP | `detail` |
| --- | --- | --- |
| 缺字段、类型错误、未知字段（`client_admission`、`files[]` 不允许多余字段）、`sha256` 格式错误、`size_bytes <= 0`、`severity` 非法、issue 缺 `code`、超出数量上限 | 422 | FastAPI 校验错误数组 |
| 会话不是 `oss_multipart` | 422 | `client_admission_requires_oss_multipart` |
| `files[].path` 含 `\`、以 `/` 开头、有空段、`.`、`..` 或不合规字符 | 422 | `client_admission_path_unsafe` |
| 路径合法但不在 `media/preview/` 之下 | 422 | `client_admission_path_outside_preview` |
| 同一来源 `path` 重复 | 422 | `client_admission_path_duplicate` |
| `files` 非空但缺 `media/preview/manifest.json` | 422 | `client_admission_manifest_missing` |
| `report` 超过 1 MiB | 422 | `client_admission_report_too_large` |

以下情况声明照常接受，admission worker 回退完整服务端模式（下载 MCAP、校验、重新生成 preview），
原因写入 `client_admission_fallback`：

| `client_admission_fallback` | 含义 |
| --- | --- |
| `client_admission_disabled` | 处理时服务端已关闭客户端预检 |
| `client_qrdf_version_not_accepted` | `qrdf_version` 不在处理时的白名单 |
| `client_policy_version_not_accepted` | `policy_version` 不在处理时的白名单 |
| `client_report_invalid` | 保存的报告无法解析（防御性检查） |
| `client_source_object_mismatch` | raw MCAP 对象 HEAD 与上传完成时的身份不一致 |
| `client_preview_missing` | metadata 声明了 RGB 流，但 `files` 为空 |
| `client_preview_object_missing` | preview 对象不存在或无法下载 |
| `client_preview_object_mismatch` | preview 对象大小或 etag 与声明不符 |
| `client_preview_hash_mismatch` | 下载的 preview 字节 SHA-256 与声明不符 |
| `client_preview_invalid` | preview 未通过服务端验证（指纹与声明的 `data_mcap_sha256`/`metadata_sha256` 不符、帧数、faststart、H.264/yuv420p 等）；细节在报告 `client_admission_fallback_detail.error_code`（如 `PREVIEW_MEDIA_STALE`） |
| `client_preview_files_mismatch` | 声明的文件集合与 `manifest.json` 引用的文件集合不相等 |

服务端重新判定为 `integrity_status=failed`（例如客户端 `ok=true` 但含 `MCAP_UNREADABLE`）时**不回退**，
直接写 failed 事实，`integrity_source="client"`，`error_code` 为第一个阻断 code。

### 4. preview 上传

- 仍用 `/oss/init`、`/oss/sign-part`、`/oss/complete`，请求体用 `file_id` 代替 `source_id`；两者同时出现返回 422。
  `file_id` 不属于本会话返回 422 `file_id does not belong to this upload session`。
- `oss/init` 的 `total_size_bytes` 必须等于声明的 `size_bytes`，否则 422。响应与 MCAP 相同
  （`total_size_bytes`、`part_size_bytes`、`total_parts`）。用同一 `file_id` 重复 init 返回相同参数，用于断点续传。
- `oss/sign-part` 新增 `content_md5`：该分片字节 MD5 的标准 Base64（24 个字符，以 `==` 结尾）。
  preview 分片必填，缺失返回 422 `content_md5_required`；MCAP 分片可选，Duance 应一律携带。
  服务端把它纳入签名，响应 `headers` 中包含 `Content-MD5`；客户端必须原样发送响应 `headers` 中的全部头。
  字节损坏时存储端拒收该分片（HTTP 400），客户端重新签名并重传该分片。

```json sign-part-request
{"workspace_id": 7, "file_id": "cb12586399787bab0f86ec8cdf8a091e1d44da02d99379c9c95746d9825f7886", "part_number": 1, "content_md5": "1B2M2Y8AsgTpgAmY7PhCfg=="}
```

```json sign-part-response
{
  "id": "0b7c6f5e-1a2b-4c3d-8e9f-001122334455",
  "status": "uploading",
  "part_number": 1,
  "content_length": 67108864,
  "method": "PUT",
  "url": "https://quicstudio-process.oss-cn-beijing.aliyuncs.com/...",
  "expires_in": 900,
  "headers": {"Content-Type": "application/octet-stream", "Content-MD5": "1B2M2Y8AsgTpgAmY7PhCfg=="}
}
```

- `oss/complete` 与 MCAP 相同，提交全部 `{part_number, etag}`。
- 会话只有在**全部 MCAP 与全部 preview 文件**都 complete 后才进入 `uploaded` 并触发解析；此前 complete 响应的 `status` 为 `uploading`。
- 对象位置由服务端决定、不返回给客户端：process 桶
  `process/v2/workspaces/{workspace_id}/collection-uploads/{upload_session_id}/{32 位随机十六进制}/{path 的文件名}`。

### 5. 观察结果

解析与 admission 异步执行。客户端轮询 `GET /data-packages/{data_package_id}?workspace_id=...`：

- `qrdf_facts.source_admission.sources[]`，按声明响应中的 `source_id` 对应：

```json package-source-admission
[
  {"source_id": "5d41402abc4b2a76b9719d911017c5925d41402abc4b2a76b9719d911017c592", "episode_id": 101, "status": "ready", "attempt": 1, "error_code": "", "integrity_source": "client"},
  {"source_id": "0b6f3e2c9a4d8e17f5c2b0a9d8e7f6a50b6f3e2c9a4d8e17f5c2b0a9d8e7f6a5", "episode_id": 102, "status": "ready", "attempt": 1, "error_code": "", "integrity_source": "server", "client_admission_fallback": "client_preview_invalid"},
  {"source_id": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "episode_id": 103, "status": "failed", "attempt": 1, "error_code": "MCAP_UNREADABLE", "integrity_source": "client"}
]
```

  - `status`：`queued` / `running` / `ready` / `failed`；`integrity_source` 在 worker 结束后出现（`client` 或 `server`）。
  - `client_admission_fallback` 只在回退时出现，取值见第 3 节。
- `episodes[]` 每项新增 `integrity_source`（当前 admission 事实的来源；尚无事实时为 `null`）。
- admission 报告（`admission-report.json`，即事实的 `report_ref`）在客户端模式下含 `client_admission`
  （客户端原始报告与服务端判定 `judgement`），回退时含 `client_admission_fallback` 及可选的 `client_admission_fallback_detail`。
````

- [ ] **Step 9: 运行测试**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_contract.py tests/test_qrdf_admission.py -q -p no:cacheprovider`
Expected: 全部 PASS（`test_qrdf_admission.py` 中的 `test_training_readiness_issue_codes_are_deferred_to_batch_qc` 仍通过）。

- [ ] **Step 10: Commit**

```bash
git add backend/data/schemas/client_admission.py backend/data/services/client_admission.py backend/data/config.py backend/data/integrations/qrdf/admission.py backend/data/routers/collection_upload_sessions.py backend/tests/test_client_admission_contract.py docs/COLLECTION_UPLOAD_API.md
git commit -m "feat(upload): 客户端预检契约、capabilities 与接口文档"
```

---
### Task 2: 声明接收 `client_admission` 并分配 preview `file_id`

**Files:**
- Modify: `backend/data/services/client_admission.py`（追加声明规范化、`preview_file_id`、`preview_object_key`）
- Modify: `backend/data/routers/collection_upload_sessions.py:91-97`（`PackageSourceDeclarationRequest.client_admission`）
- Modify: `backend/data/services/collection_upload_intake.py:112-208`（`declare_package_sources` 全函数替换，新增 `_keep_preview_object_keys`、`_declared_source_item`）
- Test: `backend/tests/test_client_admission_declaration.py`

**Interfaces:**
- Consumes: Task 1 `ClientAdmissionRequest`、`DeclarationResponse`、`doc_examples()`。
- Produces（`data.services.client_admission`）：
  - `PREVIEW_MANIFEST_PATH = "media/preview/manifest.json"`
  - `class ClientAdmissionDeclarationError(ValueError)`，`.code: str`，`str(exc) == code`
  - `normalize_client_admission(raw: Any) -> dict[str, Any]`，返回 `{"qrdf_version", "policy_version", "report", "files": [{"path", "size_bytes", "sha256"}]}`，`files` 按 `path` 升序
  - `preview_file_id(source_id: str, path: str) -> str`（`sha256(f"{source_id}:{path}")` 小写十六进制）
  - `preview_object_key(*, workspace_id: int, upload_session_id: str, path: str) -> str`
- Produces（存储）：`result_json["declarations"][package_uid][i]["client_admission"] = {"qrdf_version", "policy_version", "report", "files": [{"path", "size_bytes", "sha256", "file_id", "object_key"}]}`；不带时无此键。
- Produces（HTTP 响应）：`sources[i]["preview_files"] = [{"file_id", "path", "size_bytes"}]`，仅在声明了 `client_admission` 时出现。

- [ ] **Step 1: 写失败测试** `backend/tests/test_client_admission_declaration.py`：

```python
"""Declarations carry client admission and receive one file_id per preview file."""

from copy import deepcopy

import pytest
from tests.collection_api_fixtures import make_assigned_package, make_project, make_workspace
from tests.test_client_admission_contract import doc_examples
from tests.test_collection_upload_intake_api import _create_session, _minimal_duance_payload

from data.models.collection_upload import CollectionUploadSession
from data.schemas.client_admission import DeclarationResponse
from data.services.client_admission import (
    ClientAdmissionDeclarationError,
    normalize_client_admission,
    preview_file_id,
)


def client_admission_block() -> dict:
    return deepcopy(doc_examples()["declaration-item"]["client_admission"])


def _session(client, db_session, admin_headers, mode="oss_multipart"):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    session_id = _create_session(client, admin_headers, workspace, project, package, mode)
    return workspace, package, session_id


def _declare(client, admin_headers, workspace, session_id, item):
    return client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "items": [item]},
    )


def test_documented_declaration_example_binds_file_ids_to_paths():
    examples = doc_examples()
    request = examples["declaration-item"]["client_admission"]
    [source] = examples["declaration-response"]["sources"]
    returned = source["preview_files"]
    assert [item["path"] for item in returned] == sorted(item["path"] for item in request["files"])
    for item in returned:
        assert item["file_id"] == preview_file_id(source["source_id"], item["path"])


def test_declaration_returns_preview_file_ids_and_stores_server_keys(
    client, db_session, admin_headers
):
    workspace, package, session_id = _session(client, db_session, admin_headers)
    item = {
        **_minimal_duance_payload(package.package_uid),
        "client_admission": client_admission_block(),
    }

    response = _declare(client, admin_headers, workspace, session_id, item)

    assert response.status_code == 200, response.text
    body = DeclarationResponse.model_validate(response.json()["data"])
    [source] = body.sources
    paths = sorted(entry["path"] for entry in item["client_admission"]["files"])
    assert [entry.path for entry in source.preview_files] == paths
    for entry in source.preview_files:
        assert entry.file_id == preview_file_id(source.source_id, entry.path)
    db_session.expire_all()
    stored = db_session.get(CollectionUploadSession, session_id).result_json["declarations"]
    files = stored[package.package_uid][0]["client_admission"]["files"]
    prefix = f"process/v2/workspaces/{workspace.id}/collection-uploads/{session_id}/"
    for entry in files:
        assert entry["object_key"].startswith(prefix)
        assert entry["object_key"].endswith("/" + entry["path"].rsplit("/", 1)[-1])
    assert '"object_key"' not in response.text


def test_replayed_declaration_keeps_file_ids_and_object_keys(client, db_session, admin_headers):
    workspace, package, session_id = _session(client, db_session, admin_headers)
    item = {
        **_minimal_duance_payload(package.package_uid),
        "client_admission": client_admission_block(),
    }
    first = _declare(client, admin_headers, workspace, session_id, item)
    db_session.expire_all()
    keys = [
        entry["object_key"]
        for entry in db_session.get(CollectionUploadSession, session_id).result_json[
            "declarations"
        ][package.package_uid][0]["client_admission"]["files"]
    ]

    second = _declare(client, admin_headers, workspace, session_id, item)

    assert second.status_code == 200, second.text
    assert second.json()["data"]["sources"] == first.json()["data"]["sources"]
    db_session.expire_all()
    replayed = db_session.get(CollectionUploadSession, session_id).result_json["declarations"]
    assert [
        entry["object_key"]
        for entry in replayed[package.package_uid][0]["client_admission"]["files"]
    ] == keys


def test_declaration_without_client_admission_is_unchanged(client, db_session, admin_headers):
    workspace, package, session_id = _session(client, db_session, admin_headers)

    response = _declare(
        client, admin_headers, workspace, session_id, _minimal_duance_payload(package.package_uid)
    )

    assert response.status_code == 200, response.text
    [source] = response.json()["data"]["sources"]
    assert set(source) == {"source_id", "package_uid", "episode_id"}
    db_session.expire_all()
    stored = db_session.get(CollectionUploadSession, session_id).result_json["declarations"]
    assert "client_admission" not in stored[package.package_uid][0]


def test_client_admission_requires_oss_multipart(client, db_session, admin_headers):
    workspace, package, session_id = _session(client, db_session, admin_headers, mode="chunked")
    item = {
        **_minimal_duance_payload(package.package_uid),
        "client_admission": client_admission_block(),
    }

    response = _declare(client, admin_headers, workspace, session_id, item)

    assert response.status_code == 422
    assert response.json()["detail"] == "client_admission_requires_oss_multipart"


@pytest.mark.parametrize(
    "path, code",
    [
        ("data.mcap", "client_admission_path_outside_preview"),
        ("media/other/x.mp4", "client_admission_path_outside_preview"),
        ("media/preview", "client_admission_path_outside_preview"),
        ("/media/preview/x.mp4", "client_admission_path_unsafe"),
        ("media/preview/../data.mcap", "client_admission_path_unsafe"),
        ("media/preview//x.mp4", "client_admission_path_unsafe"),
        ("media/preview/./x.mp4", "client_admission_path_unsafe"),
        ("media\\preview\\x.mp4", "client_admission_path_unsafe"),
        ("media/preview/.hidden", "client_admission_path_unsafe"),
        ("media/preview/a b.mp4", "client_admission_path_unsafe"),
    ],
)
def test_preview_paths_outside_or_unsafe_are_rejected(path, code):
    block = client_admission_block()
    block["files"][1]["path"] = path
    with pytest.raises(ClientAdmissionDeclarationError) as error:
        normalize_client_admission(block)
    assert error.value.code == code


def test_duplicate_path_and_missing_manifest_are_rejected():
    duplicate = client_admission_block()
    duplicate["files"].append(dict(duplicate["files"][1]))
    with pytest.raises(ClientAdmissionDeclarationError, match="client_admission_path_duplicate"):
        normalize_client_admission(duplicate)
    no_manifest = client_admission_block()
    no_manifest["files"] = no_manifest["files"][1:]
    with pytest.raises(ClientAdmissionDeclarationError, match="client_admission_manifest_missing"):
        normalize_client_admission(no_manifest)
    oversized = client_admission_block()
    oversized["report"]["padding"] = "x" * (1024 * 1024)
    with pytest.raises(ClientAdmissionDeclarationError, match="client_admission_report_too_large"):
        normalize_client_admission(oversized)


def test_non_rgb_episode_may_declare_no_preview_files():
    block = client_admission_block()
    block["files"] = []
    assert normalize_client_admission(block)["files"] == []


def test_http_rejects_bad_paths_and_malformed_fields(client, db_session, admin_headers):
    workspace, package, session_id = _session(client, db_session, admin_headers)
    outside = client_admission_block()
    outside["files"][1]["path"] = "data.mcap"
    response = _declare(
        client,
        admin_headers,
        workspace,
        session_id,
        {**_minimal_duance_payload(package.package_uid), "client_admission": outside},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "client_admission_path_outside_preview"
    malformed = client_admission_block()
    malformed["files"][0]["sha256"] = "not-a-digest"
    response = _declare(
        client,
        admin_headers,
        workspace,
        session_id,
        {**_minimal_duance_payload(package.package_uid), "client_admission": malformed},
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
```

- [ ] **Step 2: 运行，确认失败**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_declaration.py -q -p no:cacheprovider`
Expected: 收集阶段 FAIL，`ImportError: cannot import name 'ClientAdmissionDeclarationError'`。

- [ ] **Step 3: 服务函数**。`backend/data/services/client_admission.py` 顶部 import 区改为：

```python
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from data.config import settings
```

文件末尾追加：

```python
PREVIEW_MANIFEST_PATH = "media/preview/manifest.json"
MAX_CLIENT_REPORT_BYTES = 1024 * 1024
_PATH_COMPONENT_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")


class ClientAdmissionDeclarationError(ValueError):
    """A declared client admission block is rejected before any upload starts."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _preview_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise ClientAdmissionDeclarationError("client_admission_path_unsafe")
    parts = value.split("/")
    # Empty, ".", ".." and hidden segments all fail the component pattern.
    if any(_PATH_COMPONENT_RE.fullmatch(part) is None for part in parts):
        raise ClientAdmissionDeclarationError("client_admission_path_unsafe")
    if len(parts) < 3 or parts[0] != "media" or parts[1] != "preview":
        raise ClientAdmissionDeclarationError("client_admission_path_outside_preview")
    return value


def normalize_client_admission(raw: Any) -> dict[str, Any]:
    """Validate a schema-checked client admission block for durable storage."""
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
        raise ClientAdmissionDeclarationError("client_admission_invalid")
    report = raw.get("report")
    encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CLIENT_REPORT_BYTES:
        raise ClientAdmissionDeclarationError("client_admission_report_too_large")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw["files"]:
        path = _preview_path(item.get("path"))
        if path in seen:
            raise ClientAdmissionDeclarationError("client_admission_path_duplicate")
        seen.add(path)
        files.append(
            {"path": path, "size_bytes": int(item["size_bytes"]), "sha256": str(item["sha256"])}
        )
    if files and PREVIEW_MANIFEST_PATH not in seen:
        raise ClientAdmissionDeclarationError("client_admission_manifest_missing")
    return {
        "qrdf_version": str(raw["qrdf_version"]),
        "policy_version": str(raw["policy_version"]),
        "report": report,
        "files": sorted(files, key=lambda item: item["path"]),
    }


def preview_file_id(source_id: str, path: str) -> str:
    """Opaque upload identity of one preview file, stable across declaration replays."""
    return hashlib.sha256(f"{source_id}:{path}".encode()).hexdigest()


def preview_object_key(*, workspace_id: int, upload_session_id: str, path: str) -> str:
    """Server-owned process-bucket key; the file name keeps the provider's type inference."""
    return (
        f"process/v2/workspaces/{workspace_id}/collection-uploads/"
        f"{upload_session_id}/{uuid4().hex}/{PurePosixPath(path).name}"
    )
```

- [ ] **Step 4: 路由字段**。`backend/data/routers/collection_upload_sessions.py` 加 `from data.schemas.client_admission import ClientAdmissionRequest`，把 `PackageSourceDeclarationRequest` 替换为：

```python
class PackageSourceDeclarationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_uid: str = Field(min_length=1, max_length=64)
    source: DuanceSourceRequest
    metadata_text: str = Field(min_length=1, max_length=1024 * 1024)
    data_file: DuanceDataFileRequest
    client_admission: ClientAdmissionRequest | None = None
```

路由已把 `ValueError` 映射为 422 且 `detail=str(exc)`，`ClientAdmissionDeclarationError` 无需额外处理。

- [ ] **Step 5: 声明存储与响应**。`backend/data/services/collection_upload_intake.py` 顶部加：

```python
from data.services.client_admission import (
    ClientAdmissionDeclarationError,
    normalize_client_admission,
    preview_file_id,
    preview_object_key,
)
```

把 `declare_package_sources` 整个函数替换为下面的版本，并在它之后新增两个辅助函数：

```python
def declare_package_sources(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    declarations: list[dict[str, object]],
) -> dict[str, object]:
    """Bind validated Duance episode manifests to session-owned packages."""
    if not declarations:
        raise ValueError("declaration items must not be empty")
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.status not in {"init", "uploading"}:
        raise PackageStateConflictError("upload_session_not_declarable")
    package_uids = _session_package_uids(db, upload_session.id)
    stored: dict[str, list[dict[str, object]]] = {}
    staged_paths: set[str] = set()
    total_bytes = 0
    for declaration in declarations:
        package_uid = declaration.get("package_uid")
        if not isinstance(package_uid, str) or package_uid not in package_uids:
            raise PackageStateConflictError("package_not_in_session")
        manifest = build_duance_import_manifest(
            source=declaration.get("source", {}),
            metadata_text=declaration.get("metadata_text", ""),
            data_file=declaration.get("data_file", {}),
        )
        stored_manifest = manifest.to_storage()
        source_id = hashlib.sha256(f"{package_uid}:{manifest.source_key}".encode()).hexdigest()
        stored_manifest["staging_layout_version"] = 2
        stored_manifest["staging_path"] = f"sources/{package_uid}/{source_id}/{manifest.data_path}"
        # The client digest is an integrity hint only.  Object identity is
        # generated by the service and is never derived from client supplied
        # hashes or source IDs.
        stored_manifest["raw_object_key"] = (
            f"raw/v2/workspaces/{workspace_id}/collection-uploads/"
            f"{upload_session.id}/{uuid4().hex}/upload.bin"
        )
        if stored_manifest["staging_path"] in staged_paths:
            raise ValueError("duplicate data file path in package declarations")
        staged_paths.add(stored_manifest["staging_path"])
        total_bytes += manifest.data_size_bytes
        client_admission = declaration.get("client_admission")
        if client_admission is not None:
            if upload_session.upload_mode != "oss_multipart":
                raise ClientAdmissionDeclarationError("client_admission_requires_oss_multipart")
            normalized = normalize_client_admission(client_admission)
            for item in normalized["files"]:
                item["file_id"] = preview_file_id(source_id, item["path"])
                item["object_key"] = preview_object_key(
                    workspace_id=workspace_id,
                    upload_session_id=upload_session.id,
                    path=item["path"],
                )
                total_bytes += item["size_bytes"]
            stored_manifest["client_admission"] = normalized
        if total_bytes > MAX_IMPORT_TOTAL_BYTES:
            raise ValueError("collection upload exceeds the total size limit")
        stored.setdefault(package_uid, []).append(stored_manifest)
    if set(stored) != package_uids:
        raise ValueError("every session package must have a declaration")
    if len(_declared_sources({"declarations": stored})) != sum(
        len(items) for items in stored.values()
    ):
        raise ValueError("duplicate source identity in package declarations")
    result = dict(upload_session.result_json or {})
    existing = result.get("declarations")
    if isinstance(existing, dict):
        # Replays keep the service generated object identities.  Compare the
        # declaration payload while ignoring those identities, which are
        # intentionally not client controlled.
        def _without_object_key(value: object) -> object:
            if isinstance(value, dict):
                return {
                    key: _without_object_key(item)
                    for key, item in value.items()
                    if key
                    not in {
                        "raw_object_key",
                        "staging_path",
                        "staging_layout_version",
                        "object_key",
                    }
                }
            if isinstance(value, list):
                return [_without_object_key(item) for item in value]
            return value

        if _without_object_key(existing) != _without_object_key(stored):
            raise PackageStateConflictError("package_declarations_conflict")
        for package_uid, items in existing.items():
            for item, old in zip(stored.get(package_uid, []), items, strict=False):
                # A replay of an older session must keep its existing byte layout.
                for key in ("raw_object_key", "staging_path", "staging_layout_version"):
                    if key in old:
                        item[key] = old[key]
                    else:
                        item.pop(key, None)
                _keep_preview_object_keys(item, old)
    result["declarations"] = stored
    upload_session.result_json = result
    upload_session.updated_at = datetime.utcnow()
    db.flush()
    return {
        "id": upload_session.id,
        "status": upload_session.status,
        "declared_packages": len(stored),
        "declared_sources": sum(len(items) for items in stored.values()),
        "sources": [
            _declared_source_item(source_id, source)
            for source_id, source in _declared_sources(result).items()
        ],
    }


def _keep_preview_object_keys(item: dict, old: dict) -> None:
    """A replayed declaration keeps the preview object keys generated first."""
    old_keys = {
        entry["path"]: entry["object_key"]
        for entry in (old.get("client_admission") or {}).get("files", [])
        if entry.get("object_key")
    }
    for entry in (item.get("client_admission") or {}).get("files", []):
        if entry["path"] in old_keys:
            entry["object_key"] = old_keys[entry["path"]]


def _declared_source_item(source_id: str, source: dict) -> dict[str, object]:
    item: dict[str, object] = {
        "source_id": source_id,
        "package_uid": source["package_uid"],
        "episode_id": source["source"]["episode_id"],
    }
    declared = source.get("client_admission")
    if isinstance(declared, dict):
        item["preview_files"] = [
            {"file_id": entry["file_id"], "path": entry["path"], "size_bytes": entry["size_bytes"]}
            for entry in declared["files"]
        ]
    return item
```

- [ ] **Step 6: 运行测试**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_declaration.py tests/test_client_admission_contract.py tests/test_collection_upload_intake_api.py tests/test_collection_upload_sessions_api.py tests/test_collection_upload_bytes.py -q -p no:cacheprovider`
Expected: 全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add backend/data/services/client_admission.py backend/data/routers/collection_upload_sessions.py backend/data/services/collection_upload_intake.py backend/tests/test_client_admission_declaration.py
git commit -m "feat(upload): 声明接收 client_admission 并为 preview 文件分配 file_id"
```

---

### Task 3: preview 分片上传到 process 桶，分片签名带 `Content-MD5`

**Files:**
- Modify: `backend/data/infra/oss_client.py`（`_PROCESS_COLLECTION_PREVIEW_OBJECT_RE`、`_validate_browser_multipart_object`、`_validate_content_md5`、`sign_browser_upload_part`）
- Modify: `backend/data/infra/object_storage.py:69`（Protocol `sign_part` 签名）
- Modify: `backend/data/infra/aliyun_object_storage.py:337-351`、`backend/data/infra/s3_object_storage.py:161-176`（`sign_part` 支持 `content_md5`）
- Modify: `backend/data/services/collection_upload_intake.py`（`sign_part` 增加 `content_md5`；新增 `start_preview_multipart`、`sign_preview_part`、`complete_preview_multipart`、`_declared_preview_files`、`_preview_target`、`_all_uploads_completed`；`complete_oss_multipart` 改用 `_all_uploads_completed`）
- Modify: `backend/data/routers/collection_upload_sessions.py`（multipart 请求模型与三个 OSS 端点）
- Test: `backend/tests/test_client_admission_uploads.py`（新建）、`backend/tests/test_oss_browser_multipart.py`、`backend/tests/test_aliyun_part_signing.py`、`backend/tests/test_s3_object_storage.py`、`backend/tests/test_client_admission_contract.py`

**Interfaces:**
- Consumes: Task 1 `CONTENT_MD5_PATTERN`、`SignedPartResponse`；Task 2 存储中的 `client_admission.files[].file_id/object_key/size_bytes`、`preview_file_id`。
- Produces:
  - `oss_client.sign_browser_upload_part(bucket, key, upload_id, part_number, content_md5: str | None = None) -> tuple[str, int, dict[str, str]]`；带 MD5 时返回的 headers 含 `Content-MD5`。
  - Provider 协议 `sign_part(ref, upload_id, part_number, *, content_md5: str | None = None) -> str`。
  - `collection_upload_intake.sign_part(..., content_md5: str | None = None)`。
  - `start_preview_multipart(db, *, workspace_id, upload_session_id, file_id, total_size_bytes, content_type) -> dict`、`sign_preview_part(db, *, workspace_id, upload_session_id, file_id, part_number, content_md5) -> dict`、`complete_preview_multipart(db, *, workspace_id, upload_session_id, file_id, parts) -> CollectionUploadSession`。
  - `result_json["oss_multipart_files"][file_id] = {"bucket", "file_id", "source_id", "package_uid", "path", "object_key", "upload_id", "total_size_bytes", "content_type", "part_size", "total_parts", "completion_state", "completion_parts", "provider_identity": {"bucket_role": "process", "object_key", "version_id", "etag", "size_bytes", "sha256": None}}`。Task 5 读取 `completion_state` 与 `provider_identity`。
  - 会话仅在全部来源与全部 preview 文件 `completion_state == "completed"` 后进入 `uploaded`。

- [ ] **Step 1: 写失败测试（存储层）**。`backend/tests/test_oss_browser_multipart.py` 末尾追加：

```python
PREVIEW_KEY = (
    "process/v2/workspaces/1/collection-uploads/"
    "11111111-1111-1111-1111-111111111111/0123456789abcdef0123456789abcdef/head_rgb.mp4"
)


def test_preview_parts_sign_content_md5_on_the_process_bucket(monkeypatch):
    _internal, public = _configure(monkeypatch)

    url, _ttl, headers = oss_client.sign_browser_upload_part(
        "process-bucket", PREVIEW_KEY, "provider-upload-1", 1, "1B2M2Y8AsgTpgAmY7PhCfg=="
    )

    assert url.startswith("https://")
    assert headers == {
        "Content-Type": "application/octet-stream",
        "Content-MD5": "1B2M2Y8AsgTpgAmY7PhCfg==",
    }
    assert public.calls[0][2] == PREVIEW_KEY
    assert public.calls[0][4] == headers


@pytest.mark.parametrize(
    "bucket,key,md5",
    [
        ("raw-bucket", PREVIEW_KEY, None),
        ("process-bucket", RAW_KEY, None),
        ("process-bucket", PREVIEW_KEY.replace("head_rgb.mp4", ".hidden"), None),
        ("process-bucket", PREVIEW_KEY, "not-base64"),
    ],
)
def test_preview_signing_rejects_wrong_bucket_key_or_digest(monkeypatch, bucket, key, md5):
    _configure(monkeypatch)
    with pytest.raises(ValueError):
        oss_client.sign_browser_upload_part(bucket, key, "provider-upload-1", 1, md5)
```

`backend/tests/test_aliyun_part_signing.py` 末尾追加：

```python
def test_multipart_signature_covers_content_md5(monkeypatch):
    monkeypatch.setattr("oss2.auth.time.time", lambda: 1_700_000_000)
    provider = AliyunObjectStorage(
        StorageBucketConfig.for_environment("test"),
        endpoint_url="https://oss-cn-beijing.aliyuncs.com",
        access_key_id="test-key",
        access_key_secret="test-secret",
    )
    ref = StorageObjectRef("process", "process/v2/test/head_rgb.mp4", None, "", 10)
    signed = provider.sign_part(ref, "test-upload-id", 1, content_md5="1B2M2Y8AsgTpgAmY7PhCfg==")
    expected = oss2.Bucket(
        oss2.Auth("test-key", "test-secret"),
        "https://oss-cn-beijing.aliyuncs.com",
        "quicstudio-test-process",
    ).sign_url(
        "PUT",
        ref.object_key,
        900,
        params={"partNumber": "1", "uploadId": "test-upload-id"},
        headers={
            "Content-Type": "application/octet-stream",
            "Content-MD5": "1B2M2Y8AsgTpgAmY7PhCfg==",
        },
    )
    assert signed == expected
```

（process 桶名由 `data.services.storage_bucket_config.bucket_name("test", "process")` 决定，与现有用例的 `quicstudio-test-raw` 同一命名规则。）

`backend/tests/test_s3_object_storage.py` 末尾追加：

```python
def test_s3_part_signature_includes_content_md5():
    fake = FakeS3()
    provider = S3ObjectStorage(StorageBucketConfig.for_environment("dev"), client=fake)

    provider.sign_part(_ref(), "upload-1", 1, content_md5="1B2M2Y8AsgTpgAmY7PhCfg==")

    assert fake.calls[-1][1]["Params"]["ContentMD5"] == "1B2M2Y8AsgTpgAmY7PhCfg=="
```

- [ ] **Step 2: 写失败测试（HTTP）** `backend/tests/test_client_admission_uploads.py`：

```python
"""Preview files upload to the process bucket by file_id with signed Content-MD5."""

import re

import pytest
from tests.collection_api_fixtures import make_assigned_package, make_project, make_workspace
from tests.test_client_admission_declaration import client_admission_block
from tests.test_collection_upload_intake_api import _create_session, _minimal_duance_payload

from data.infra import oss_client
from data.models.collection_upload import CollectionUploadSession
from data.schemas.client_admission import SignedPartResponse

MD5 = "1B2M2Y8AsgTpgAmY7PhCfg=="
PREVIEW_KEY_RE = re.compile(
    r"process/v2/workspaces/[1-9][0-9]*/collection-uploads/[0-9a-f-]{36}/[0-9a-f]{32}/[^/]+"
)


class FakeMultipart:
    """Records provider calls; an object exists once its multipart upload completes."""

    def __init__(self, sizes_by_name: dict[str, int]):
        self.sizes_by_name = sizes_by_name
        self.inits: list[tuple[str, str]] = []
        self.signed: list[tuple] = []
        self.completed: set[str] = set()

    def size_of(self, key: str) -> int:
        return self.sizes_by_name[key.rsplit("/", 1)[-1]]

    def init(self, bucket, key):
        self.inits.append((bucket, key))
        return f"upload-{len(self.inits)}"

    def sign(self, *args):
        self.signed.append(args)
        headers = {"Content-Type": "application/octet-stream"}
        if len(args) == 5:
            headers["Content-MD5"] = args[4]
        return f"https://uploads.example.test/{args[2]}/{args[3]}", 300, headers

    def list_parts(self, _bucket, key, _upload_id):
        return [oss_client.OSSMultipartPart(number=1, etag="etag-1", size=self.size_of(key))]

    def complete(self, _bucket, key, _upload_id, _parts):
        self.completed.add(key)

    def object_info(self, _bucket, key):
        if key not in self.completed:
            return None
        return oss_client.OSSObjectInfo(
            size=self.size_of(key),
            etag=f"etag-{key[-8:]}",
            crc64="1234",
            version_id=None,
            metadata={},
        )


def _small_block():
    block = client_admission_block()
    for entry in block["files"]:
        entry["size_bytes"] = 16
    return block


@pytest.fixture
def declared(client, db_session, admin_headers, monkeypatch):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    session_id = _create_session(
        client, admin_headers, workspace, project, package, "oss_multipart"
    )
    block = _small_block()
    sizes = {"upload.bin": 4}
    sizes.update({entry["path"].rsplit("/", 1)[-1]: 16 for entry in block["files"]})
    fake = FakeMultipart(sizes)
    monkeypatch.setattr(oss_client, "init_browser_multipart_upload", fake.init)
    monkeypatch.setattr(oss_client, "sign_browser_upload_part", fake.sign)
    monkeypatch.setattr(oss_client, "list_browser_multipart_parts", fake.list_parts)
    monkeypatch.setattr(oss_client, "complete_browser_multipart_upload", fake.complete)
    monkeypatch.setattr(oss_client, "object_info", fake.object_info)
    response = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "items": [{**_minimal_duance_payload(package.package_uid), "client_admission": block}],
        },
    )
    assert response.status_code == 200, response.text
    [source] = response.json()["data"]["sources"]
    return workspace, session_id, source, fake


def _post(client, headers, session_id, action, body):
    return client.post(
        f"/api/v1/upload-sessions/{session_id}/oss/{action}", headers=headers, json=body
    )


def _upload(client, headers, workspace, session_id, target, size, *, md5=MD5):
    initialized = _post(
        client,
        headers,
        session_id,
        "init",
        {"workspace_id": workspace.id, "total_size_bytes": size, **target},
    )
    assert initialized.status_code == 200, initialized.text
    signed = _post(
        client,
        headers,
        session_id,
        "sign-part",
        {
            "workspace_id": workspace.id,
            "part_number": 1,
            **target,
            **({"content_md5": md5} if md5 else {}),
        },
    )
    assert signed.status_code == 200, signed.text
    completed = _post(
        client,
        headers,
        session_id,
        "complete",
        {"workspace_id": workspace.id, "parts": [{"part_number": 1, "etag": "etag-1"}], **target},
    )
    assert completed.status_code == 200, completed.text
    return signed.json()["data"], completed.json()["data"]["status"]


def test_session_is_uploaded_only_after_mcap_and_every_preview(
    client, db_session, admin_headers, declared
):
    workspace, session_id, source, fake = declared

    _signed, status = _upload(
        client, admin_headers, workspace, session_id, {"source_id": source["source_id"]}, 4
    )
    assert status == "uploading"
    statuses = []
    for entry in source["preview_files"]:
        signed, status = _upload(
            client,
            admin_headers,
            workspace,
            session_id,
            {"file_id": entry["file_id"]},
            entry["size_bytes"],
        )
        SignedPartResponse.model_validate(signed)
        assert signed["headers"]["Content-MD5"] == MD5
        statuses.append(status)

    assert statuses == ["uploading"] * (len(statuses) - 1) + ["uploaded"]
    process_bucket = oss_client.bucket_name("process")
    preview_inits = [(bucket, key) for bucket, key in fake.inits if bucket == process_bucket]
    assert len(preview_inits) == len(source["preview_files"])
    assert all(PREVIEW_KEY_RE.fullmatch(key) for _bucket, key in preview_inits)
    db_session.expire_all()
    stored = db_session.get(CollectionUploadSession, session_id).result_json["oss_multipart_files"]
    for entry in source["preview_files"]:
        identity = stored[entry["file_id"]]["provider_identity"]
        assert stored[entry["file_id"]]["completion_state"] == "completed"
        assert identity["bucket_role"] == "process" and identity["sha256"] is None


def test_mcap_parts_sign_without_md5_but_preview_parts_require_it(client, admin_headers, declared):
    workspace, session_id, source, fake = declared
    target = {"source_id": source["source_id"]}
    _post(
        client,
        admin_headers,
        session_id,
        "init",
        {"workspace_id": workspace.id, "total_size_bytes": 4, **target},
    )
    legacy = _post(
        client,
        admin_headers,
        session_id,
        "sign-part",
        {"workspace_id": workspace.id, "part_number": 1, **target},
    )
    assert legacy.status_code == 200
    assert "Content-MD5" not in legacy.json()["data"]["headers"]
    assert len(fake.signed[-1]) == 4

    preview = {"file_id": source["preview_files"][0]["file_id"]}
    _post(
        client,
        admin_headers,
        session_id,
        "init",
        {"workspace_id": workspace.id, "total_size_bytes": 16, **preview},
    )
    missing = _post(
        client,
        admin_headers,
        session_id,
        "sign-part",
        {"workspace_id": workspace.id, "part_number": 1, **preview},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"] == "content_md5_required"


def test_preview_init_is_resumable_with_the_same_file_id(client, admin_headers, declared):
    workspace, session_id, source, fake = declared
    body = {
        "workspace_id": workspace.id,
        "total_size_bytes": 16,
        "file_id": source["preview_files"][0]["file_id"],
    }

    first = _post(client, admin_headers, session_id, "init", body)
    second = _post(client, admin_headers, session_id, "init", body)

    assert first.status_code == second.status_code == 200
    assert first.json()["data"] == second.json()["data"]
    assert len(fake.inits) == 1


def test_preview_target_errors(client, admin_headers, declared):
    workspace, session_id, source, _fake = declared
    file_id = source["preview_files"][0]["file_id"]
    wrong_size = _post(
        client,
        admin_headers,
        session_id,
        "init",
        {"workspace_id": workspace.id, "total_size_bytes": 17, "file_id": file_id},
    )
    assert wrong_size.status_code == 422
    both = _post(
        client,
        admin_headers,
        session_id,
        "init",
        {
            "workspace_id": workspace.id,
            "total_size_bytes": 16,
            "file_id": file_id,
            "source_id": source["source_id"],
        },
    )
    assert both.status_code == 422
    unknown = _post(
        client,
        admin_headers,
        session_id,
        "init",
        {"workspace_id": workspace.id, "total_size_bytes": 16, "file_id": "f" * 64},
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "file_id does not belong to this upload session"
```

`backend/tests/test_client_admission_contract.py` 末尾追加：

```python
def test_documented_sign_part_request_matches_the_router_model():
    from data.routers.collection_upload_sessions import OssSignPartRequest

    request = OssSignPartRequest.model_validate(doc_examples()["sign-part-request"])
    assert request.file_id and request.content_md5 and request.source_id is None
```

- [ ] **Step 3: 运行，确认失败**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_uploads.py tests/test_oss_browser_multipart.py tests/test_aliyun_part_signing.py tests/test_s3_object_storage.py tests/test_client_admission_contract.py -q -p no:cacheprovider`
Expected: 新增用例 FAIL（`TypeError: sign_browser_upload_part() takes 4 positional arguments but 5 were given`、`sign_part() got an unexpected keyword argument 'content_md5'`、HTTP 422 `extra_forbidden` for `file_id` 等）。

- [ ] **Step 4: oss_client**。`backend/data/infra/oss_client.py` 在 `_RAW_COLLECTION_UPLOAD_OBJECT_RE` 之后加：

```python
_PROCESS_COLLECTION_PREVIEW_OBJECT_RE = re.compile(
    r"process/v2/workspaces/[1-9][0-9]*/collection-uploads/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"[0-9a-f]{32}/[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}"
)
_CONTENT_MD5_RE = re.compile(r"[A-Za-z0-9+/]{22}==")
```

把 `_validate_browser_multipart_object` 替换为：

```python
def _validate_browser_multipart_object(bucket: str, key: str) -> None:
    key_text = str(key or "")
    raw_allowed = bucket == bucket_name("raw") and bool(
        _RAW_IMPORT_OBJECT_RE.fullmatch(key_text)
        or _RAW_COLLECTION_UPLOAD_OBJECT_RE.fullmatch(key_text)
    )
    # Client-generated collection previews are the only browser uploads that
    # may target the process bucket.
    preview_allowed = bucket == bucket_name("process") and bool(
        _PROCESS_COLLECTION_PREVIEW_OBJECT_RE.fullmatch(key_text)
    )
    if not (raw_allowed or preview_allowed) or not _provider_path_is_safe(bucket, key):
        raise ValueError("browser multipart object is not allowed")


def _validate_content_md5(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _CONTENT_MD5_RE.fullmatch(value) is None:
        raise ValueError("multipart part Content-MD5 is invalid")
    return value
```

把 `sign_browser_upload_part` 替换为：

```python
def sign_browser_upload_part(
    bucket: str,
    key: str,
    upload_id: str,
    part_number: int,
    content_md5: str | None = None,
) -> tuple[str, int, dict[str, str]]:
    """Sign only one numbered UploadPart request for a persisted upload.

    A given ``content_md5`` is part of the signature, so the storage service
    rejects a part whose bytes do not match the digest the client declared.
    """
    _validate_browser_multipart_object(bucket, key)
    checked_upload_id = _validate_multipart_upload_id(upload_id)
    checked_part_number = _validate_multipart_part_number(part_number)
    checked_md5 = _validate_content_md5(content_md5)
    headers = {"Content-Type": "application/octet-stream"}
    if checked_md5:
        headers["Content-MD5"] = checked_md5
    provider_ref = _new_provider_ref(bucket, key)
    if provider_ref is not None:
        provider, ref = provider_ref
        if checked_md5:
            url = provider.sign_part(
                ref, checked_upload_id, checked_part_number, content_md5=checked_md5
            )
        else:
            url = provider.sign_part(ref, checked_upload_id, checked_part_number)
        if not _is_public_browser_url(url):
            raise ValueError("provider returned an invalid browser upload URL")
        return url, browser_direct_ttl_seconds(), headers
    ttl = browser_direct_ttl_seconds()
    client = _get_browser_bucket(bucket)
    if client is None or ttl <= 0:
        raise ValueError("browser multipart upload is unavailable")
    url = client.sign_url(
        "PUT",
        key,
        ttl,
        headers=headers,
        params={"uploadId": checked_upload_id, "partNumber": str(checked_part_number)},
        slash_safe=True,
    )
    if not _is_public_browser_url(url):
        raise ValueError("provider returned an invalid browser upload URL")
    return url, ttl, headers
```

- [ ] **Step 5: provider 协议与实现**。`backend/data/infra/object_storage.py` Protocol 中：

```python
    def sign_part(
        self,
        ref: StorageObjectRef,
        upload_id: str,
        part_number: int,
        *,
        content_md5: str | None = None,
    ) -> str: ...
```

`backend/data/infra/aliyun_object_storage.py` 的 `sign_part` 替换为：

```python
    def sign_part(
        self,
        ref: StorageObjectRef,
        upload_id: str,
        part_number: int,
        *,
        content_md5: str | None = None,
    ) -> str:
        if not upload_id or part_number < 1:
            raise ObjectStorageError("upload_id and positive part_number are required")
        headers = {"Content-Type": "application/octet-stream"}
        if content_md5:
            headers["Content-MD5"] = content_md5
        try:
            return str(
                self._bucket(ref, self.browser_endpoint_url).sign_url(
                    "PUT",
                    ref.object_key,
                    self.presign_expires,
                    params={"partNumber": str(part_number), "uploadId": upload_id},
                    headers=headers,
                )
            )
        except Exception as exc:
            raise ObjectStorageError(f"OSS multipart sign failed: {exc}") from exc
```

`backend/data/infra/s3_object_storage.py` 的 `sign_part` 替换为：

```python
    def sign_part(
        self,
        ref: StorageObjectRef,
        upload_id: str,
        part_number: int,
        *,
        content_md5: str | None = None,
    ) -> str:
        bucket = self._bucket(ref)
        if not str(upload_id).strip() or part_number < 1:
            raise ObjectStorageError("upload_id and positive part_number are required")
        params: dict[str, Any] = {
            "Bucket": bucket,
            "Key": ref.object_key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        }
        if content_md5:
            # Signed as a header: S3 rejects a part whose bytes do not match.
            params["ContentMD5"] = content_md5
        return str(
            self._call(
                "generate_presigned_url",
                self.browser_client.generate_presigned_url,
                ClientMethod="upload_part",
                Params=params,
                ExpiresIn=self.presign_expires,
            )
        )
```

（`Any` 已在 `s3_object_storage.py` 顶部导入；若没有，补 `from typing import Any`。）

- [ ] **Step 6: 上传服务**。`backend/data/services/collection_upload_intake.py`：

1. 现有 `sign_part` 签名加 `content_md5: str | None = None`，把其中的 `url, ttl, headers = oss_client.sign_browser_upload_part(...)` 调用替换为（只在带 MD5 时才传第 5 个参数，老调用与测试桩不变）：

```python
    sign_args: tuple[object, ...] = (
        str(declaration["bucket"]),
        str(declaration["object_key"]),
        str(declaration["upload_id"]),
        part_number,
    )
    if content_md5:
        sign_args = (*sign_args, content_md5)
    url, ttl, headers = oss_client.sign_browser_upload_part(*sign_args)
```

2. `complete_oss_multipart` 中 `if _all_multipart_sources_completed(result, sources):` 改为 `if _all_uploads_completed(result, sources):`。

3. 在 `_all_multipart_sources_completed` 之后新增：

```python
def _declared_preview_files(result: dict) -> dict[str, dict]:
    """Every declared preview file keyed by its opaque file_id."""
    files: dict[str, dict] = {}
    for source_id, source in _declared_sources(result).items():
        for entry in (source.get("client_admission") or {}).get("files") or []:
            files[entry["file_id"]] = {
                **entry,
                "source_id": source_id,
                "package_uid": source["package_uid"],
            }
    return files


def _preview_target(result: dict, file_id: str) -> dict:
    files = _declared_preview_files(result)
    if file_id not in files:
        raise ValueError("file_id does not belong to this upload session")
    return files[file_id]


def _all_uploads_completed(result: dict[str, object], sources: dict[str, dict]) -> bool:
    """A session is uploaded only when every data file and every preview file completed."""
    if not _all_multipart_sources_completed(result, sources):
        return False
    uploads = result.get("oss_multipart_files") or {}
    return all(
        isinstance(uploads.get(file_id), dict)
        and uploads[file_id].get("completion_state") == "completed"
        for file_id in _declared_preview_files(result)
    )


def start_preview_multipart(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    file_id: str,
    total_size_bytes: int,
    content_type: str | None,
) -> dict[str, object]:
    from data.infra import oss_client

    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.upload_mode != "oss_multipart":
        raise PackageStateConflictError("upload_mode_mismatch")
    if upload_session.status not in {"init", "uploading"}:
        raise PackageStateConflictError("upload_session_not_uploading")
    result = _require_declarations(db, upload_session)
    target = _preview_target(result, file_id)
    if isinstance(total_size_bytes, bool) or total_size_bytes != target["size_bytes"]:
        raise ValueError("collection upload size differs from the declared file size")
    part_size = settings.import_direct_upload_part_bytes
    total_parts = (total_size_bytes + part_size - 1) // part_size
    if not 1 <= total_parts <= 10_000:
        raise ValueError("collection upload exceeds the multipart part limit")
    uploads = dict(result.get("oss_multipart_files") or {})
    declaration = uploads.get(file_id)
    if isinstance(declaration, dict):
        if (
            declaration.get("total_size_bytes") != total_size_bytes
            or declaration.get("content_type") != content_type
        ):
            raise PackageStateConflictError("oss_declaration_conflict")
        declaration = dict(declaration)
    else:
        declaration = {
            "bucket": oss_client.bucket_name("process"),
            "file_id": file_id,
            "source_id": target["source_id"],
            "package_uid": target["package_uid"],
            "path": target["path"],
            "object_key": target["object_key"],
            "upload_id": "",
            "total_size_bytes": total_size_bytes,
            "content_type": content_type,
            "part_size": part_size,
            "total_parts": total_parts,
        }
        uploads[file_id] = declaration
        result["oss_multipart_files"] = uploads
        upload_session.result_json = result
        upload_session.status = "uploading"
        upload_session.updated_at = datetime.utcnow()
        db.flush()
        # Persist the declaration before the provider creates an upload ID.
        db.commit()
        upload_session = _locked_session(
            db, workspace_id=workspace_id, upload_session_id=upload_session_id
        )
        result = dict(upload_session.result_json or {})
        uploads = dict(result.get("oss_multipart_files") or {})
        declaration = dict(uploads[file_id])
    if not declaration.get("upload_id"):
        declaration["upload_id"] = oss_client.init_browser_multipart_upload(
            str(declaration["bucket"]), str(declaration["object_key"])
        )
        uploads[file_id] = declaration
        result["oss_multipart_files"] = uploads
        upload_session.result_json = result
        db.flush()
    return _oss_progress(upload_session, declaration)


def sign_preview_part(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    file_id: str,
    part_number: int,
    content_md5: str | None,
) -> dict[str, object]:
    from data.infra import oss_client

    if not content_md5:
        raise ValueError("content_md5_required")
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.upload_mode != "oss_multipart" or upload_session.status != "uploading":
        raise PackageStateConflictError("upload_session_not_uploading")
    result = dict(upload_session.result_json or {})
    _preview_target(result, file_id)
    declaration = (result.get("oss_multipart_files") or {}).get(file_id)
    if not isinstance(declaration, dict) or not declaration.get("upload_id"):
        raise ValueError("multipart upload is not initialized")
    total_parts = int(declaration.get("total_parts") or 0)
    if isinstance(part_number, bool) or not 1 <= part_number <= total_parts:
        raise ValueError("multipart part number is invalid")
    url, ttl, headers = oss_client.sign_browser_upload_part(
        str(declaration["bucket"]),
        str(declaration["object_key"]),
        str(declaration["upload_id"]),
        part_number,
        content_md5,
    )
    return {
        "id": upload_session.id,
        "status": upload_session.status,
        "part_number": part_number,
        "content_length": _expected_part_size(declaration, part_number),
        "method": "PUT",
        "url": url,
        "expires_in": ttl,
        "headers": headers,
    }


def complete_preview_multipart(
    db: Session,
    *,
    workspace_id: int,
    upload_session_id: str,
    file_id: str,
    parts: list[dict[str, object]],
) -> CollectionUploadSession:
    from data.infra import oss_client

    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    if upload_session.status == "uploaded":
        return upload_session
    if upload_session.upload_mode != "oss_multipart" or upload_session.status != "uploading":
        raise PackageStateConflictError("upload_session_not_uploading")
    result = dict(upload_session.result_json or {})
    _preview_target(result, file_id)
    uploads = dict(result.get("oss_multipart_files") or {})
    declaration = uploads.get(file_id)
    if not isinstance(declaration, dict) or not declaration.get("upload_id"):
        raise ValueError("multipart upload is not initialized")
    declaration = dict(declaration)
    total_parts = int(declaration["total_parts"])
    client_manifest = _normalize_client_parts(parts, total_parts=total_parts)
    bucket = str(declaration["bucket"])
    object_key = str(declaration["object_key"])
    upload_id = str(declaration["upload_id"])
    info = oss_client.object_info(bucket, object_key)
    if info is None:
        provider_parts = oss_client.list_browser_multipart_parts(bucket, object_key, upload_id)
        if len(provider_parts) != total_parts:
            raise ValueError("multipart upload is incomplete")
        provider_manifest = [
            {"part_number": part.number, "etag": str(part.etag).strip('"')}
            for part in provider_parts
        ]
        if client_manifest != provider_manifest:
            raise ValueError("multipart part manifest does not match the provider")
        if any(
            part.size != _expected_part_size(declaration, part.number) for part in provider_parts
        ):
            raise ValueError("multipart part size does not match its declaration")
        declaration["completion_parts"] = provider_manifest
        declaration["completion_state"] = "completing"
        uploads[file_id] = declaration
        result["oss_multipart_files"] = uploads
        upload_session.result_json = result
        db.flush()
        # Persist the provider-verified intent before the upload ID is consumed.
        db.commit()
        try:
            oss_client.complete_browser_multipart_upload(
                bucket, object_key, upload_id, provider_parts
            )
        except Exception:
            info = oss_client.object_info(bucket, object_key)
            if info is None:
                raise
        else:
            info = oss_client.object_info(bucket, object_key)
    elif not declaration.get("completion_parts") or client_manifest != _normalize_client_parts(
        list(declaration["completion_parts"]), total_parts=total_parts
    ):
        raise ValueError("multipart part manifest does not match the persisted completion intent")
    if info is None or int(info.size) != int(declaration["total_size_bytes"]):
        raise ValueError("completed multipart object size does not match its declaration")
    upload_session = _locked_session(
        db, workspace_id=workspace_id, upload_session_id=upload_session_id
    )
    result = dict(upload_session.result_json or {})
    uploads = dict(result.get("oss_multipart_files") or {})
    current = dict(uploads[file_id])
    current["completion_state"] = "completed"
    # Preview bytes are downloaded and SHA-256 verified by the admission
    # worker, so provider CRC64 is not required here.
    current["provider_identity"] = {
        "bucket_role": "process",
        "object_key": object_key,
        "version_id": info.version_id,
        "etag": str(info.etag).strip('"'),
        "size_bytes": int(info.size),
        "sha256": None,
    }
    uploads[file_id] = current
    result["oss_multipart_files"] = uploads
    upload_session.result_json = result
    if _all_uploads_completed(result, _declared_sources(result)):
        upload_session = mark_upload_session_uploaded(
            db,
            workspace_id=workspace_id,
            upload_session_id=upload_session.id,
        )
        ensure_collection_upload_parse_job(db, upload_session)
    return upload_session
```

- [ ] **Step 7: 路由**。`backend/data/routers/collection_upload_sessions.py`：`from pydantic import BaseModel, ConfigDict, Field, model_validator`；`from data.schemas.client_admission import CONTENT_MD5_PATTERN, ClientAdmissionRequest`；从 `collection_upload_intake` 追加导入 `complete_preview_multipart`、`sign_preview_part`、`start_preview_multipart`。把 `OssMultipartInitRequest`、`OssSignPartRequest`、`OssMultipartPartRequest`、`OssMultipartCompleteRequest` 四个类替换为：

```python
class OssTargetRequest(BaseModel):
    """One multipart target: an episode data file (source_id) or a preview file (file_id)."""

    model_config = ConfigDict(extra="forbid")

    source_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    file_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _single_target(self):
        if self.source_id is not None and self.file_id is not None:
            raise ValueError("multipart target must be either source_id or file_id")
        return self


class OssMultipartInitRequest(OssTargetRequest):
    workspace_id: int = Field(gt=0)
    total_size_bytes: int = Field(gt=0)
    content_type: str | None = Field(default=None, min_length=1, max_length=256)


class OssSignPartRequest(OssTargetRequest):
    workspace_id: int = Field(gt=0)
    part_number: int = Field(ge=1, le=10_000)
    content_md5: str | None = Field(default=None, pattern=CONTENT_MD5_PATTERN)


class OssMultipartPartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=256)


class OssMultipartCompleteRequest(OssTargetRequest):
    workspace_id: int = Field(gt=0)
    parts: list[OssMultipartPartRequest] = Field(min_length=1, max_length=10_000)
```

三个 OSS 端点整体替换为：

```python
@router.post("/{upload_session_id}/oss/init")
def initialize_oss_multipart_upload(
    upload_session_id: str,
    body: OssMultipartInitRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        if body.file_id is not None:
            result = start_preview_multipart(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                file_id=body.file_id,
                total_size_bytes=body.total_size_bytes,
                content_type=body.content_type,
            )
        else:
            result = start_oss_multipart(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                total_size_bytes=body.total_size_bytes,
                content_type=body.content_type,
                source_id=body.source_id,
            )
        db.commit()
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(result)


@router.post("/{upload_session_id}/oss/sign-part")
def sign_oss_multipart_part(
    upload_session_id: str,
    body: OssSignPartRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        if body.file_id is not None:
            result = sign_preview_part(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                file_id=body.file_id,
                part_number=body.part_number,
                content_md5=body.content_md5,
            )
        else:
            result = sign_part(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                part_number=body.part_number,
                source_id=body.source_id,
                content_md5=body.content_md5,
            )
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(result)


@router.post("/{upload_session_id}/oss/complete")
def finish_oss_multipart_upload(
    upload_session_id: str,
    body: OssMultipartCompleteRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    require_permission(user, "workspace:write")
    _require_workspace(db, user=user, workspace_id=body.workspace_id)
    try:
        parts = [part.model_dump() for part in body.parts]
        if body.file_id is not None:
            completed_session = complete_preview_multipart(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                file_id=body.file_id,
                parts=parts,
            )
        else:
            completed_session = complete_oss_multipart(
                db,
                workspace_id=body.workspace_id,
                upload_session_id=upload_session_id,
                parts=parts,
                source_id=body.source_id,
            )
        db.commit()
        if completed_session.status == "uploaded":
            _dispatch_parse_job(db, upload_session_id)
        upload_session = get_upload_session_record(
            db,
            workspace_id=body.workspace_id,
            upload_session_id=upload_session_id,
        )
    except PackageStateConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(_session_item(upload_session))
```

- [ ] **Step 8: 运行测试**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_uploads.py tests/test_oss_browser_multipart.py tests/test_aliyun_part_signing.py tests/test_s3_object_storage.py tests/test_client_admission_contract.py tests/test_collection_upload_intake_api.py tests/test_storage_provider_runtime.py tests/test_aliyun_upload_integrity.py -q -p no:cacheprovider`
Expected: 全部 PASS。

- [ ] **Step 9: Commit**

```bash
git add backend/data/infra backend/data/services/collection_upload_intake.py backend/data/routers/collection_upload_sessions.py backend/tests/test_client_admission_uploads.py backend/tests/test_oss_browser_multipart.py backend/tests/test_aliyun_part_signing.py backend/tests/test_s3_object_storage.py backend/tests/test_client_admission_contract.py
git commit -m "feat(upload): preview 文件按 file_id 直传 process 桶，分片签名带 Content-MD5"
```

---
### Task 4: admission 事实记录 `integrity_source`，并对客户端可见

**Files:**
- Create: `backend/alembic/versions/0010_admission_integrity_source.py`
- Modify: `backend/data/models/episode_admission.py`（`__table_args__` 加检查约束，新增列）
- Modify: `backend/data/services/episode_admission.py:25-145`（`record_episode_admission_fact` 新增 `integrity_source` 参数）
- Modify: `backend/data/routers/collection_packages.py:318-360`（`_episode_item` 输出 `integrity_source`）
- Modify: `backend/data/services/collection_upload_parse.py:108-149`（`refresh_upload_admission_counts` 透出 `integrity_source`、`client_admission_fallback`）
- Test: `backend/tests/test_episode_admission.py`、`backend/tests/test_client_admission_contract.py`

**Interfaces:**
- Consumes: Task 1 `SourceAdmissionStatus`。
- Produces:
  - `EpisodeAdmissionFact.integrity_source: str`（`client` / `server`，库默认 `server`）。
  - `record_episode_admission_fact(..., integrity_source: str = "server")`；非法值抛 `ValueError("integrity_source must be client or server")`；幂等比较包含该字段。
  - 包详情 `episodes[i]["integrity_source"]`（无事实为 `None`）；`qrdf_facts.source_admission.sources[i]` 在来源状态有这两个键时透出 `integrity_source`、`client_admission_fallback`。Task 6 的 worker 负责把它们写进来源状态。

- [ ] **Step 1: 写失败测试**。`backend/tests/test_episode_admission.py` 末尾追加：

```python
def test_admission_fact_records_integrity_source(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    _package, (episode,) = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    assert current_episode_admission_fact(db_session, episode_id=episode.id).integrity_source == (
        "server"
    )
    fact = record_episode_admission_fact(
        db_session,
        episode_id=episode.id,
        attempt=2,
        source_fingerprint=episode.source_fingerprint,
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        report_ref={"ok": True},
        objects=verified_entries(),
        integrity_source="client",
    )
    db_session.commit()
    assert fact.integrity_source == "client"
    with pytest.raises(ValueError, match="integrity_source must be client or server"):
        record_episode_admission_fact(
            db_session,
            episode_id=episode.id,
            attempt=3,
            source_fingerprint=episode.source_fingerprint,
            validation_policy_version="v1",
            integrity_status="passed",
            preview_status="ready",
            output_verification_status="verified",
            report_ref={"ok": True},
            objects=verified_entries(),
            integrity_source="duance",
        )


def test_integrity_source_column_is_constrained():
    from sqlalchemy import inspect

    from data.database import engine

    inspector = inspect(engine)
    columns = {
        column["name"]: column for column in inspector.get_columns("episode_admission_facts")
    }
    assert columns["integrity_source"]["nullable"] is False
    checks = {item["name"] for item in inspector.get_check_constraints("episode_admission_facts")}
    assert "ck_episode_admission_facts_integrity_source" in checks
```

`backend/tests/test_client_admission_contract.py` 顶部 import 区补充：

```python
from uuid import uuid4

from tests.collection_api_fixtures import make_project, seed_package_pending_intake_review
from tests.test_episode_objects import verified_entries

from data.models.collection_upload import CollectionUploadSession, CollectionUploadSessionPackage
from data.services.collection_upload_parse import refresh_upload_admission_counts
from data.services.episode_admission import record_episode_admission_fact
```

（`make_workspace` 已从 `tests.collection_api_fixtures` 导入，合并到同一条 import。）文件末尾追加：

```python
def _source_state(source_id, package_uid, episode_id, **extra):
    return {
        "source_id": source_id,
        "package_uid": package_uid,
        "episode_id": episode_id,
        "status": "ready",
        "attempt": 2,
        "error_code": "",
        "metadata_text": "{}",
        **extra,
    }


def test_package_detail_exposes_integrity_source_and_fallback(client, db_session, admin_headers):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package, (client_episode, server_episode) = seed_package_pending_intake_review(
        db_session, workspace, project
    )
    record_episode_admission_fact(
        db_session,
        episode_id=client_episode.id,
        attempt=2,
        source_fingerprint=client_episode.source_fingerprint,
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        report_ref={"ok": True},
        objects=verified_entries(),
        integrity_source="client",
    )
    client_source, fallback_source = "a" * 64, "b" * 64
    upload = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=workspace.id,
        collection_project_id=project.id,
        status="succeeded",
        upload_mode="oss_multipart",
        result_json={
            "admission_sources": {
                client_source: _source_state(
                    client_source, package.package_uid, client_episode.id, integrity_source="client"
                ),
                fallback_source: _source_state(
                    fallback_source,
                    package.package_uid,
                    server_episode.id,
                    integrity_source="server",
                    client_admission_fallback="client_preview_invalid",
                ),
            }
        },
    )
    db_session.add(upload)
    db_session.flush()
    db_session.add(
        CollectionUploadSessionPackage(
            upload_session_id=upload.id,
            data_package_id=package.id,
            package_uid=package.package_uid,
        )
    )
    refresh_upload_admission_counts(db_session, upload)
    db_session.commit()

    response = client.get(
        f"/api/v1/data-packages/{package.id}",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    sources = {
        item.source_id: item
        for item in (
            SourceAdmissionStatus.model_validate(raw)
            for raw in data["qrdf_facts"]["source_admission"]["sources"]
        )
    }
    assert sources[client_source].integrity_source == "client"
    assert sources[client_source].client_admission_fallback is None
    assert sources[fallback_source].integrity_source == "server"
    assert sources[fallback_source].client_admission_fallback == "client_preview_invalid"
    assert {item["id"]: item["integrity_source"] for item in data["episodes"]} == {
        client_episode.id: "client",
        server_episode.id: "server",
    }
```

- [ ] **Step 2: 运行，确认失败**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_episode_admission.py tests/test_client_admission_contract.py -k "integrity_source" -q -p no:cacheprovider`
Expected: FAIL，`AttributeError: 'EpisodeAdmissionFact' object has no attribute 'integrity_source'`。

- [ ] **Step 3: 迁移** `backend/alembic/versions/0010_admission_integrity_source.py`：

```python
"""Admission facts record who proved source integrity (client or server)."""

import sqlalchemy as sa
from alembic import op

revision = "0010_admission_integrity_source"
down_revision = "0009_drop_admission_process_ref"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "episode_admission_facts",
        sa.Column(
            "integrity_source",
            sa.String(length=16),
            nullable=False,
            server_default="server",
        ),
    )
    op.create_check_constraint(
        "ck_episode_admission_facts_integrity_source",
        "episode_admission_facts",
        "integrity_source IN ('client', 'server')",
    )


def downgrade():
    op.drop_constraint(
        "ck_episode_admission_facts_integrity_source",
        "episode_admission_facts",
        type_="check",
    )
    op.drop_column("episode_admission_facts", "integrity_source")
```

- [ ] **Step 4: 模型**。`backend/data/models/episode_admission.py` 的 `__table_args__` 替换为：

```python
    __table_args__ = (
        UniqueConstraint(
            "episode_id",
            "attempt",
            name="uq_episode_admission_facts_episode_attempt",
        ),
        Index(
            "uq_episode_admission_facts_current_episode",
            "episode_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_episode_admission_facts_episode_current",
            "episode_id",
            "is_current",
        ),
        CheckConstraint("attempt >= 1", name="ck_episode_admission_facts_attempt"),
        CheckConstraint(
            "integrity_status IN ('pending', 'running', 'passed', 'failed', 'not_applicable')",
            name="ck_episode_admission_facts_integrity_status",
        ),
        CheckConstraint(
            "preview_status IN ('pending', 'running', 'ready', 'failed', 'not_applicable')",
            name="ck_episode_admission_facts_preview_status",
        ),
        CheckConstraint(
            "output_verification_status IN ('pending', 'running', 'verified', 'failed', 'not_applicable')",
            name="ck_episode_admission_facts_output_status",
        ),
        CheckConstraint(
            "integrity_source IN ('client', 'server')",
            name="ck_episode_admission_facts_integrity_source",
        ),
    )
```

`objects_json` 列下一行加：

```python
    integrity_source = Column(String(16), nullable=False, default="server", server_default="server")
```

- [ ] **Step 5: 写入函数**。`backend/data/services/episode_admission.py` 的 `record_episode_admission_fact`：
  - 签名在 `objects: list[Any] | None = None,` 之后加 `integrity_source: str = "server",`。
  - 在 `if attempt < 1:` 检查之后加：

```python
    if integrity_source not in {"client", "server"}:
        raise ValueError("integrity_source must be client or server")
```

  - 幂等比较 `same_payload = (...)` 的最后一个条件 `and fact.error_message == (error_message or "")` 之后加一行 `and fact.integrity_source == integrity_source`。
  - 赋值区 `fact.objects_json = normalized_objects` 下一行加：

```python
    fact.integrity_source = integrity_source
```

- [ ] **Step 6: 包详情 episode 项**。`backend/data/routers/collection_packages.py` 的 `_episode_item` 中 `item: dict[str, object] = {...}` 替换为：

```python
item: dict[str, object] = {
    "id": episode.id,
    "episode_uid": episode.episode_uid,
    "external_episode_id": (
        metadata.get("collection_upload", {}).get("external_episode_id")
        if isinstance(metadata.get("collection_upload"), dict)
        else None
    ),
    "validity_status": episode.validity_status,
    "modality": episode.modality,
    "preview_available": bool(
        admission_fact is not None
        and admission_fact.is_current
        and admission_fact.preview_status == "ready"
    ),
    "admission_status": admission_status,
    "integrity_source": (admission_fact.integrity_source if admission_fact is not None else None),
}
```

- [ ] **Step 7: 来源状态透出**。`backend/data/services/collection_upload_parse.py` 的 `refresh_upload_admission_counts` 整体替换为：

```python
_SOURCE_ADMISSION_KEYS = (
    "source_id",
    "episode_id",
    "status",
    "attempt",
    "error_code",
    "integrity_source",
    "client_admission_fallback",
)


def refresh_upload_admission_counts(db: Session, upload_session: CollectionUploadSession) -> None:
    """Refresh source counts from durable per-source outcomes under session lock."""
    result = dict(upload_session.result_json or {})
    sources = list((result.get("admission_sources") or {}).values())
    failed = [s for s in sources if s.get("status") == "failed"]
    result.update(
        ready_count=sum(s.get("status") == "ready" for s in sources),
        failed_count=len(failed),
        failed=failed,
        pending_count=sum(s.get("status") in {"queued", "running"} for s in sources),
    )
    upload_session.result_json = result
    # These counts include sources without an Episode, so package detail can
    # explain malformed metadata without inventing an Episode identity.
    for link in db.scalars(
        select(CollectionUploadSessionPackage).where(
            CollectionUploadSessionPackage.upload_session_id == upload_session.id
        )
    ):
        package = db.get(DataPackage, link.data_package_id)
        owned = [s for s in sources if s.get("package_uid") == link.package_uid]
        package.qrdf_facts_json = {
            **(package.qrdf_facts_json or {}),
            "source_admission": {
                "ready_count": sum(s.get("status") == "ready" for s in owned),
                "failed_count": sum(s.get("status") == "failed" for s in owned),
                "sources": [{k: s[k] for k in _SOURCE_ADMISSION_KEYS if k in s} for s in owned],
            },
        }
```

- [ ] **Step 8: 运行测试**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_episode_admission.py tests/test_client_admission_contract.py tests/test_baseline_migration.py tests/test_collection_upload_parse.py tests/test_collection_admission_worker.py -q -p no:cacheprovider`
Expected: 新增用例 PASS；`test_episode_admission.py` 的失败集合是基线（5 个）的子集，其余 PASS。

- [ ] **Step 9: Commit**

```bash
git add backend/alembic/versions/0010_admission_integrity_source.py backend/data/models/episode_admission.py backend/data/services/episode_admission.py backend/data/routers/collection_packages.py backend/data/services/collection_upload_parse.py backend/tests/test_episode_admission.py backend/tests/test_client_admission_contract.py
git commit -m "feat(admission): admission 事实记录 integrity_source 并在包详情中可见"
```

---

### Task 5: 服务端重新判定客户端报告，解析把 `client_admission` 带入来源状态

**Files:**
- Modify: `backend/data/services/client_admission.py`（追加 `ClientReportInvalid`、`judge_client_report`、`client_admission_state`）
- Modify: `backend/data/services/collection_upload_parse.py:262-276`（`_schedule_persisted_sources` 写入来源状态）
- Test: `backend/tests/test_client_admission_worker.py`（新建；本任务提供共用造数，Task 6 追加 worker 用例）

**Interfaces:**
- Consumes: Task 1 `NON_BLOCKING_ISSUE_CODES`；Task 2 存储的 `client_admission`（含 `file_id`）；Task 3 `oss_multipart_files[file_id].completion_state/provider_identity`、`preview_file_id`。
- Produces:
  - `class ClientReportInvalid(ValueError)`。
  - `judge_client_report(report: Any) -> tuple[str, list[dict[str, Any]]]`：返回 `("passed" | "failed", issues)`，每个 issue 规范化为 `{"severity", "code", "message", "path", "topic"}`；报告不是对象、`issues` 不是列表、issue 缺合法 `severity`/`code` 时抛 `ClientReportInvalid`。
  - `client_admission_state(result: dict[str, Any], *, source: dict[str, Any]) -> dict[str, Any]`：来源未声明时返回 `{}`；否则返回 `{"client_admission": {"qrdf_version", "policy_version", "report", "files": [{"path", "size_bytes", "sha256", "object": dict | None}]}}`，`object` 为已完成上传的 provider identity，未完成为 `None`。
  - 来源状态 `result_json["admission_sources"][source_id]["client_admission"]`（Task 6 的 worker 读取）。
  - 测试造数（`tests/test_client_admission_worker.py`）：`ObjectStore`、`generate_previews(root) -> list[str]`、`setup_client_upload(db, tmp_path, monkeypatch, *, qrdf_version="0.2.1", issues=None, restyle_metadata=False, drop_preview=False) -> (upload, package, store)`。

- [ ] **Step 1: 写失败测试** `backend/tests/test_client_admission_worker.py`：

```python
"""Client-precheck admission: server re-judgement, parse state, worker mode and fallback."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from tests.collection_api_fixtures import make_assigned_package, make_project, make_workspace
from tests.test_qrdf_admission import _episode

from data.config import settings
from data.infra.object_storage import StorageObjectNotFound, StorageObjectRef
from data.models.collection_upload import (
    CollectionUploadSession,
    CollectionUploadSessionPackage,
)
from data.services.client_admission import (
    ClientReportInvalid,
    client_admission_state,
    judge_client_report,
    preview_file_id,
)
from data.services.collection_upload_parse import parse_collection_upload_session

NON_BLOCKING_ERROR = {"severity": "ERROR", "code": "NO_ACTION_TOPIC", "message": "no action"}


class ObjectStore:
    """In-memory storage provider keyed by object key; records every download."""

    def __init__(self, db):
        self.db = db
        self.objects: dict[str, bytes] = {}
        self.identity: dict[str, tuple[str | None, str]] = {}
        self.downloads: list[tuple[str, str]] = []

    def put(self, key, data, *, version_id="v1", etag="e1"):
        self.objects[key] = data
        self.identity[key] = (version_id, etag)

    def head(self, ref):
        if ref.object_key not in self.objects:
            raise StorageObjectNotFound(ref.object_key)
        version_id, etag = self.identity[ref.object_key]
        size = len(self.objects[ref.object_key])
        return StorageObjectRef(ref.bucket_role, ref.object_key, version_id, etag, size, None)

    def download_file(self, ref, destination):
        assert not self.db.in_transaction(), "download must not hold a DB transaction"
        self.downloads.append((ref.bucket_role, ref.object_key))
        head = self.head(ref)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(self.objects[ref.object_key])
        return head

    def put_worker_object(self, ref, source_path):
        assert not self.db.in_transaction(), "upload must not hold a DB transaction"
        data = Path(source_path).read_bytes()
        self.put(ref.object_key, data, version_id="pv1", etag="pe1")
        digest = hashlib.sha256(data).hexdigest()
        return StorageObjectRef("process", ref.object_key, "pv1", "pe1", len(data), digest)

    def delete_exact(self, ref):
        self.objects.pop(ref.object_key, None)

    def sign_get(self, ref, *, expires=900):
        assert ref.object_key in self.objects
        return f"https://storage.test/{ref.object_key}"


def generate_previews(root: Path) -> list[str]:
    """Run the SDK preview generation the client runs and list the published files."""
    from qrdf.reader.episode import Episode as QRDFEpisode

    episode = QRDFEpisode(root)
    episode.generate_rgb_previews(camera_topics=None, max_edge=1280, keyframe_interval_s=1.0)
    manifest = episode.load_rgb_preview_manifest()
    paths = ["media/preview/manifest.json"]
    for stream in manifest.streams:
        paths.append(f"media/preview/{stream.video_path}")
        paths.append(f"media/preview/{stream.timeline_path}")
    return sorted(paths)


def setup_client_upload(
    db,
    tmp_path,
    monkeypatch,
    *,
    qrdf_version="0.2.1",
    issues=None,
    restyle_metadata=False,
    drop_preview=False,
):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    root = _episode(tmp_path)
    preview_paths = generate_previews(root)
    data = (root / "data.mcap").read_bytes()
    metadata_text = (root / "metadata.json").read_text(encoding="utf-8")
    if restyle_metadata:
        # Same content, different bytes: the client preview fingerprint is stale.
        metadata_text = json.dumps(json.loads(metadata_text), indent=2)
    metadata = json.loads(metadata_text)
    workspace = make_workspace(db)
    project = make_project(db, workspace)
    package = make_assigned_package(db, workspace, project)
    store = ObjectStore(db)
    digest = hashlib.sha256(data).hexdigest()
    raw_key = f"raw/v2/test/{uuid4().hex}/source.mcap"
    store.put(raw_key, data)
    source_key = uuid4().hex
    source_id = hashlib.sha256(f"{package.package_uid}:{source_key}".encode()).hexdigest()
    files = []
    uploads = {}
    for index, relative in enumerate(preview_paths):
        payload = (root / relative).read_bytes()
        file_id = preview_file_id(source_id, relative)
        key = (
            f"process/v2/workspaces/{workspace.id}/collection-uploads/test/"
            f"{uuid4().hex}/{Path(relative).name}"
        )
        store.put(key, payload, version_id=None, etag=f"preview-etag-{index}")
        files.append(
            {
                "path": relative,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "file_id": file_id,
                "object_key": key,
            }
        )
        uploads[file_id] = {
            "completion_state": "completed",
            "file_id": file_id,
            "object_key": key,
            "provider_identity": {
                "bucket_role": "process",
                "object_key": key,
                "version_id": None,
                "etag": f"preview-etag-{index}",
                "size_bytes": len(payload),
                "sha256": None,
            },
        }
    if drop_preview:
        store.objects.pop(files[-1]["object_key"])
    source = {
        "source_key": source_key,
        "source": {
            "episode_id": metadata["episode_id"],
            "start_ns": str(metadata["timing"]["start_timestamp_ns"]),
            "end_ns": str(metadata["timing"]["end_timestamp_ns"]),
            "metadata_sha256": hashlib.sha256(metadata_text.encode()).hexdigest(),
            "data_mcap_sha256": digest,
        },
        "metadata_text": metadata_text,
        "data_file": {"path": "data.mcap", "size_bytes": len(data), "sha256": digest},
        "staging_path": f"sources/{package.package_uid}/data.mcap",
        "raw_object_key": raw_key,
        "client_admission": {
            "qrdf_version": qrdf_version,
            "policy_version": "v1",
            "report": {
                "ok": True,
                "issues": [NON_BLOCKING_ERROR] if issues is None else issues,
                "media_validation": {"ok": True, "issues": []},
            },
            "files": files,
        },
    }
    identity = asdict(StorageObjectRef("raw", raw_key, "v1", "e1", len(data), None))
    upload = CollectionUploadSession(
        id=str(uuid4()),
        workspace_id=workspace.id,
        collection_project_id=project.id,
        status="uploaded",
        upload_mode="oss_multipart",
        result_json={
            "declarations": {package.package_uid: [source]},
            "oss_multipart_sources": {
                source_id: {
                    "completion_state": "completed",
                    "source_id": source_id,
                    "package_uid": package.package_uid,
                    "data_path": "data.mcap",
                    "object_key": raw_key,
                    "provider_identity": identity,
                }
            },
            "oss_multipart_files": uploads,
        },
    )
    db.add(upload)
    db.flush()
    db.add(
        CollectionUploadSessionPackage(
            upload_session_id=upload.id,
            data_package_id=package.id,
            package_uid=package.package_uid,
        )
    )
    package.status = "uploading"
    db.commit()
    monkeypatch.setattr("data.infra.storage_provider.get_storage_provider", lambda: store)
    return upload, package, store


def test_server_rejudges_issue_codes_and_ignores_client_ok():
    status, issues = judge_client_report(
        {"ok": True, "issues": [{"severity": "ERROR", "code": "MCAP_UNREADABLE"}]}
    )
    assert status == "failed"
    assert issues == [
        {"severity": "ERROR", "code": "MCAP_UNREADABLE", "message": "", "path": None, "topic": None}
    ]
    tolerated = {"ok": False, "issues": [NON_BLOCKING_ERROR, {"severity": "WARNING", "code": "X"}]}
    assert judge_client_report(tolerated)[0] == "passed"
    unknown = {"ok": True, "issues": [{"severity": "ERROR", "code": "A_NEW_SDK_CODE"}]}
    assert judge_client_report(unknown)[0] == "failed"


@pytest.mark.parametrize(
    "report",
    [
        None,
        {"ok": True},
        {"issues": "none"},
        {"issues": [{"severity": "ERROR"}]},
        {"issues": [{"severity": "FATAL", "code": "X"}]},
        {"issues": [{"severity": "ERROR", "code": ""}]},
        {"issues": ["MCAP_UNREADABLE"]},
    ],
)
def test_malformed_client_report_is_invalid(report):
    with pytest.raises(ClientReportInvalid):
        judge_client_report(report)


def test_state_carries_declared_files_with_completed_identities():
    identity = {
        "bucket_role": "process",
        "object_key": "k1",
        "version_id": None,
        "etag": "e",
        "size_bytes": 3,
        "sha256": None,
    }
    source = {
        "client_admission": {
            "qrdf_version": "0.2.1",
            "policy_version": "v1",
            "report": {"ok": True, "issues": [], "media_validation": None},
            "files": [
                {
                    "path": "media/preview/manifest.json",
                    "size_bytes": 3,
                    "sha256": "a" * 64,
                    "file_id": "f1",
                    "object_key": "k1",
                },
                {
                    "path": "media/preview/g/x_rgb.mp4",
                    "size_bytes": 4,
                    "sha256": "b" * 64,
                    "file_id": "f2",
                    "object_key": "k2",
                },
            ],
        }
    }
    result = {
        "oss_multipart_files": {
            "f1": {"completion_state": "completed", "provider_identity": identity},
            "f2": {"completion_state": "completing"},
        }
    }

    state = client_admission_state(result, source=source)

    files = state["client_admission"]["files"]
    assert files[0] == {
        "path": "media/preview/manifest.json",
        "size_bytes": 3,
        "sha256": "a" * 64,
        "object": identity,
    }
    assert files[1]["object"] is None
    assert state["client_admission"]["qrdf_version"] == "0.2.1"
    assert client_admission_state(result, source={}) == {}


def test_parse_records_client_admission_on_the_source_state(db_session, tmp_path, monkeypatch):
    upload, _package, _store = setup_client_upload(db_session, tmp_path, monkeypatch)

    parse_collection_upload_session(db_session, upload.id)

    db_session.expire_all()
    stored = db_session.get(CollectionUploadSession, upload.id)
    [state] = stored.result_json["admission_sources"].values()
    declared = state["client_admission"]
    assert declared["qrdf_version"] == "0.2.1"
    assert declared["files"]
    assert all(item["object"]["bucket_role"] == "process" for item in declared["files"])
```

- [ ] **Step 2: 运行，确认失败**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_worker.py -q -p no:cacheprovider`
Expected: 收集阶段 FAIL，`ImportError: cannot import name 'ClientReportInvalid'`。

- [ ] **Step 3: 判定与状态函数**。`backend/data/services/client_admission.py` 末尾追加：

```python
_SEVERITIES = frozenset({"ERROR", "WARNING", "INFO"})


class ClientReportInvalid(ValueError):
    """A stored client report cannot be judged; the worker falls back to server mode."""


def judge_client_report(report: Any) -> tuple[str, list[dict[str, Any]]]:
    """Re-judge client issues with the server's own table; the client ``ok`` is ignored."""
    if not isinstance(report, dict) or not isinstance(report.get("issues"), list):
        raise ClientReportInvalid("client_report_invalid")
    issues: list[dict[str, Any]] = []
    for raw in report["issues"]:
        if (
            not isinstance(raw, dict)
            or raw.get("severity") not in _SEVERITIES
            or not isinstance(raw.get("code"), str)
            or not raw["code"]
        ):
            raise ClientReportInvalid("client_report_invalid")
        issues.append(
            {
                "severity": raw["severity"],
                "code": raw["code"],
                "message": str(raw.get("message") or ""),
                "path": raw.get("path") if isinstance(raw.get("path"), str) else None,
                "topic": raw.get("topic") if isinstance(raw.get("topic"), str) else None,
            }
        )
    blocking = any(
        issue["severity"] == "ERROR" and issue["code"] not in NON_BLOCKING_ISSUE_CODES
        for issue in issues
    )
    return ("failed" if blocking else "passed"), issues


def client_admission_state(result: dict[str, Any], *, source: dict[str, Any]) -> dict[str, Any]:
    """What the admission worker needs from one declared source's client admission."""
    declared = source.get("client_admission")
    if not isinstance(declared, dict):
        return {}
    uploads = result.get("oss_multipart_files") or {}
    files: list[dict[str, Any]] = []
    for item in declared.get("files") or []:
        upload = uploads.get(item.get("file_id"))
        identity = (
            upload.get("provider_identity")
            if isinstance(upload, dict) and upload.get("completion_state") == "completed"
            else None
        )
        files.append(
            {
                "path": item["path"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
                "object": dict(identity) if isinstance(identity, dict) else None,
            }
        )
    return {
        "client_admission": {
            "qrdf_version": declared.get("qrdf_version"),
            "policy_version": declared.get("policy_version"),
            "report": declared.get("report"),
            "files": files,
        }
    }
```

- [ ] **Step 4: 解析写入来源状态**。`backend/data/services/collection_upload_parse.py` 顶部加 `from data.services.client_admission import client_admission_state`；`_schedule_persisted_sources` 中 `states[source_id] = {...}`（排队状态那一处，`"status": "queued"`）替换为：

```python
                states[source_id] = {
                    "source_id": source_id,
                    "package_uid": package_uid,
                    "episode_id": episode_id,
                    "source_fingerprint": parsed["source_fingerprint"],
                    "source_object": identity,
                    "metadata_text": source["metadata_text"],
                    "data_path": source["data_file"]["path"],
                    "declared_sha256": source["data_file"]["sha256"],
                    "job_id": job.id,
                    "status": "queued",
                    "attempt": 0,
                    **client_admission_state(result, source=source),
                }
```

- [ ] **Step 5: 运行测试**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_worker.py tests/test_collection_upload_parse.py tests/test_collection_admission_worker.py -q -p no:cacheprovider`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add backend/data/services/client_admission.py backend/data/services/collection_upload_parse.py backend/tests/test_client_admission_worker.py
git commit -m "feat(admission): 服务端重新判定客户端报告，解析把 client_admission 带入来源状态"
```

---
### Task 6: admission worker 客户端预检模式与回退

**Files:**
- Create: `backend/data/integrations/qrdf/client_admission.py`
- Modify: `backend/data/integrations/qrdf/admission.py`（`_preview_artifacts_valid` 增加 `expected_source`；`admit_qrdf_episode` 增加 `report_extra`；`run_qrdf_admission_worker` 从 `provider = None` 到函数结尾整段替换）
- Test: `backend/tests/test_client_admission_worker.py`、`backend/tests/test_collection_admission_worker.py`、`backend/tests/test_qrdf_admission.py`

**Interfaces:**
- Consumes: Task 4 `record_episode_admission_fact(integrity_source=)`；Task 5 `judge_client_report`、`ClientReportInvalid`、来源状态 `client_admission`、`setup_client_upload`、`ObjectStore`；Task 1 `client_admission_capabilities`、`NON_BLOCKING_ISSUE_CODES`；Task 2 `PREVIEW_MANIFEST_PATH`；计划 1 `object_entry`。
- Produces:
  - `_preview_artifacts_valid(episode_path, episode, *, expected_source: dict[str, str] | None = None)`：给定时不读取本地 MCAP，用它比较 manifest 的 `source` 指纹。
  - `admit_qrdf_episode(..., report_extra: dict[str, Any] | None = None)`：合并进报告后再上传 `admission-report.json`。
  - `data.integrations.qrdf.client_admission.ClientAdmissionFallback(reason: str, detail: dict | None)`，`.reason`、`.detail`。
  - `admit_client_prechecked(scratch: Path, *, state, source_ref: StorageObjectRef, source_fingerprint: str, provider, process_provider, check_owned) -> tuple[QRDFAdmissionResult, dict[str, Any]]`：返回结果与 `verified_source`（`{**asdict(raw HEAD), "sha256": 声明值, "relative_path": data_path}`）；无法使用客户端结果时抛 `ClientAdmissionFallback`。
  - worker 结果：事实 `integrity_source`；报告键 `integrity_source`、`client_admission`（客户端模式）、`client_admission_fallback` 与可选 `client_admission_fallback_detail`（回退）；来源状态键 `integrity_source`、`client_admission_fallback`（仅回退时）。

- [ ] **Step 1: 写失败测试**。`backend/tests/test_client_admission_worker.py` 顶部 import 区补充：

```python
from tests.test_collection_admission_worker import claimed_job

from data.database import Episode
from data.integrations.qrdf.admission import run_qrdf_admission_worker
from data.services.episode_admission import current_episode_admission_fact
```

文件末尾追加：

```python
def _run(db, upload, package):
    parse_collection_upload_session(db, upload.id)
    run_qrdf_admission_worker(db, claimed_job(db, upload))
    db.expire_all()
    episode = db.query(Episode).filter_by(data_package_id=package.id).one()
    fact = current_episode_admission_fact(db, episode_id=episode.id)
    [state] = db.get(CollectionUploadSession, upload.id).result_json["admission_sources"].values()
    return fact, state


def _stored_report(fact, store):
    entry = next(item for item in fact.objects_json if item["kind"] == "admission_report")
    return json.loads(store.objects[entry["ref"]["object_key"]])


def test_client_mode_admits_without_downloading_the_mcap(db_session, tmp_path, monkeypatch):
    upload, package, store = setup_client_upload(db_session, tmp_path, monkeypatch)

    fact, state = _run(db_session, upload, package)

    assert fact.integrity_source == "client"
    assert (fact.integrity_status, fact.preview_status, fact.output_verification_status) == (
        "passed",
        "ready",
        "verified",
    )
    assert store.downloads, "preview files are downloaded and verified"
    assert all(role == "process" for role, _key in store.downloads)
    kinds = {entry["kind"] for entry in fact.objects_json}
    assert {
        "data",
        "metadata",
        "admission_report",
        "preview_manifest",
        "preview_video",
        "preview_timeline",
    } <= kinds
    declared = state["client_admission"]["files"]
    preview_keys = {
        entry["ref"]["object_key"]
        for entry in fact.objects_json
        if entry["kind"].startswith("preview_")
    }
    assert preview_keys == {item["object"]["object_key"] for item in declared}
    data_entry = next(entry for entry in fact.objects_json if entry["kind"] == "data")
    assert data_entry["ref"]["sha256"] == state["declared_sha256"]
    report = _stored_report(fact, store)
    assert report == fact.report_ref_json
    assert report["integrity_source"] == "client"
    assert report["client_admission"]["judgement"]["integrity_status"] == "passed"
    assert "client_admission_fallback" not in report
    assert state["integrity_source"] == "client"
    assert "client_admission_fallback" not in state


def test_client_ok_with_blocking_code_fails_without_fallback(db_session, tmp_path, monkeypatch):
    upload, package, store = setup_client_upload(
        db_session,
        tmp_path,
        monkeypatch,
        issues=[{"severity": "ERROR", "code": "MCAP_UNREADABLE", "message": "truncated"}],
    )

    fact, state = _run(db_session, upload, package)

    assert fact.integrity_source == "client"
    assert fact.integrity_status == "failed"
    assert fact.output_verification_status == "failed"
    assert fact.error_code == "MCAP_UNREADABLE"
    assert store.downloads == []
    assert "client_admission_fallback" not in fact.report_ref_json
    assert fact.report_ref_json["client_admission"]["report"]["ok"] is True
    assert state["status"] == "failed"
    assert state["integrity_source"] == "client"


@pytest.mark.parametrize(
    "options, disable, reason",
    [
        ({"qrdf_version": "0.1.9"}, False, "client_qrdf_version_not_accepted"),
        ({}, True, "client_admission_disabled"),
        ({"restyle_metadata": True}, False, "client_preview_invalid"),
        ({"drop_preview": True}, False, "client_preview_object_missing"),
    ],
)
def test_unusable_client_result_falls_back_to_server_mode(
    db_session, tmp_path, monkeypatch, options, disable, reason
):
    upload, package, store = setup_client_upload(db_session, tmp_path, monkeypatch, **options)
    if disable:
        monkeypatch.setattr(settings, "accept_client_admission", False)

    fact, state = _run(db_session, upload, package)

    assert fact.integrity_source == "server"
    assert fact.output_verification_status == "verified"
    assert any(role == "raw" for role, _key in store.downloads), "server mode downloads the MCAP"
    assert fact.report_ref_json["client_admission_fallback"] == reason
    assert _stored_report(fact, store)["client_admission_fallback"] == reason
    assert state["integrity_source"] == "server"
    assert state["client_admission_fallback"] == reason
    if reason == "client_preview_invalid":
        detail = fact.report_ref_json["client_admission_fallback_detail"]
        assert detail["error_code"] == "PREVIEW_MEDIA_STALE"
```

`backend/tests/test_collection_admission_worker.py` 末尾追加：

```python
def test_server_mode_marks_integrity_source_server(db_session, tmp_path, monkeypatch):
    upload, package, _provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)

    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))

    db_session.expire_all()
    episode = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    fact = current_episode_admission_fact(db_session, episode_id=episode.id)
    assert fact.integrity_source == "server"
    assert fact.report_ref_json["integrity_source"] == "server"
    assert "client_admission_fallback" not in fact.report_ref_json
    session = db_session.get(CollectionUploadSession, upload.id)
    [state] = session.result_json["admission_sources"].values()
    assert state["integrity_source"] == "server"
    assert "client_admission_fallback" not in state
```

`backend/tests/test_qrdf_admission.py` 末尾追加：

```python
def test_report_extra_is_written_into_the_published_report(tmp_path):
    episode = _episode(tmp_path)
    provider = _ProcessCapture(tmp_path)

    result = admit_qrdf_episode(
        episode,
        process_provider=provider,
        report_extra={
            "integrity_source": "server",
            "client_admission_fallback": "client_preview_invalid",
        },
    )

    report_entry = next(item for item in result.objects if item["kind"] == "admission_report")
    stored = json.loads(provider.uploads[report_entry["ref"]["object_key"]])
    assert stored["client_admission_fallback"] == "client_preview_invalid"
    assert result.report["integrity_source"] == "server"
```

- [ ] **Step 2: 运行，确认失败**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_worker.py tests/test_collection_admission_worker.py tests/test_qrdf_admission.py -q -p no:cacheprovider`
Expected: 新增用例 FAIL（客户端模式用例下载了 raw、`integrity_source == "server"`；`admit_qrdf_episode() got an unexpected keyword argument 'report_extra'`）。

- [ ] **Step 3: preview 验证接受声明的指纹**。`backend/data/integrations/qrdf/admission.py`：`_preview_artifacts_valid` 的参数表改为 `(episode_path: Path, episode: Any, *, expected_source: dict[str, str] | None = None)`，返回类型不变。函数体中

```python
        metadata_path = episode_path / "metadata.json"
        expected_source = {
            "data_mcap_sha256": _sha256_file(Path(episode.mcap_path)),
            "metadata_sha256": _sha256_file(metadata_path),
        }
```

替换为

```python
        # Server mode fingerprints the scratch bytes; client-precheck mode has
        # no local MCAP and passes the declared digests instead.
        if expected_source is None:
            metadata_path = episode_path / "metadata.json"
            expected_source = {
                "data_mcap_sha256": _sha256_file(Path(episode.mcap_path)),
                "metadata_sha256": _sha256_file(metadata_path),
            }
```

其余规则不变。

- [ ] **Step 4: 报告附加字段**。`admit_qrdf_episode` 签名在 `preview_keyframe_interval_s: float = 1.0,` 之后加 `report_extra: dict[str, Any] | None = None,`；在 `report["source_fingerprint"] = source_fingerprint` 下一行加：

```python
    report.update(report_extra or {})
```

- [ ] **Step 5: 客户端预检模式模块** `backend/data/integrations/qrdf/client_admission.py`：

```python
"""Client-precheck admission: trust the re-judged client report, verify previews only.

The raw MCAP is never downloaded here.  Its identity is proven by a provider
HEAD (size and etag) and its digest is the one the client declared, carried by
an upload whose every part was signed with Content-MD5.  Only the preview
files are downloaded and checked against the declared fingerprints.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from data.infra.object_storage import StorageObjectNotFound, StorageObjectRef
from data.integrations.qrdf.admission import (
    QRDFAdmissionResult,
    _is_rgb_episode,
    _preview_artifacts_valid,
    _sha256_file,
)
from data.services.client_admission import (
    NON_BLOCKING_ISSUE_CODES,
    PREVIEW_MANIFEST_PATH,
    ClientReportInvalid,
    client_admission_capabilities,
    judge_client_report,
)
from data.services.episode_objects import object_entry


class ClientAdmissionFallback(Exception):
    """The client result cannot be used; this attempt reruns in server mode."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = dict(detail or {})


class _DeclaredEpisode:
    """The part of a qrdf ``Episode`` preview validation reads, without an MCAP."""

    def __init__(self, path: Path) -> None:
        from qrdf.models.episode import EpisodeMetadata

        self._path = path
        self._metadata = EpisodeMetadata.load(path / "metadata.json")

    def list_streams(self) -> list[Any]:
        return [stream for device in self._metadata.devices for stream in device.streams]

    def list_topics(self) -> list[str]:
        return [str(topic.name) for topic in self._metadata.topics]

    def load_rgb_preview_manifest(self) -> Any:
        from qrdf.models import PreviewManifest

        manifest_path = self._path / "media" / "preview" / "manifest.json"
        if not manifest_path.is_file():
            return None
        return PreviewManifest.load(manifest_path)


def _gate(declared: dict[str, Any]) -> None:
    capabilities = client_admission_capabilities()
    if not capabilities["enabled"]:
        raise ClientAdmissionFallback("client_admission_disabled")
    if declared.get("qrdf_version") not in capabilities["accepted_qrdf_versions"]:
        raise ClientAdmissionFallback("client_qrdf_version_not_accepted")
    if declared.get("policy_version") not in capabilities["accepted_policy_versions"]:
        raise ClientAdmissionFallback("client_policy_version_not_accepted")


def _head(provider: Any, ref: StorageObjectRef) -> StorageObjectRef | None:
    try:
        return provider.head(ref)
    except StorageObjectNotFound:
        return None


def _preview_heads(provider: Any, files: list[dict[str, Any]]) -> dict[str, StorageObjectRef]:
    heads: dict[str, StorageObjectRef] = {}
    for item in files:
        stored = item.get("object")
        if not isinstance(stored, dict) or stored.get("bucket_role") != "process":
            raise ClientAdmissionFallback("client_preview_object_missing")
        ref = StorageObjectRef(
            "process",
            str(stored["object_key"]),
            stored.get("version_id"),
            str(stored.get("etag") or ""),
            int(stored["size_bytes"]),
            None,
        )
        head = _head(provider, ref)
        if head is None:
            raise ClientAdmissionFallback("client_preview_object_missing")
        if (
            head.size_bytes != int(item["size_bytes"])
            or not head.etag
            or (ref.etag and head.etag != ref.etag)
        ):
            raise ClientAdmissionFallback("client_preview_object_mismatch")
        heads[str(item["path"])] = head
    return heads


def _publish(
    process_provider: Any, local: Path, prefix: str, relative: str, kind: str
) -> dict[str, Any]:
    sha256 = _sha256_file(local)
    ref = StorageObjectRef(
        "process", f"{prefix}/{relative}", None, "", local.stat().st_size, sha256
    )
    persisted = process_provider.put_worker_object(ref, str(local))
    return object_entry(path=relative, kind=kind, ref={**asdict(persisted), "sha256": sha256})


def admit_client_prechecked(
    scratch: Path,
    *,
    state: dict[str, Any],
    source_ref: StorageObjectRef,
    source_fingerprint: str,
    provider: Any,
    process_provider: Any,
    check_owned: Callable[[], None],
) -> tuple[QRDFAdmissionResult, dict[str, Any]]:
    """Admit one source from its client admission, or raise ``ClientAdmissionFallback``."""
    declared = dict(state["client_admission"])
    _gate(declared)
    try:
        integrity_status, issues = judge_client_report(declared.get("report"))
    except ClientReportInvalid as exc:
        raise ClientAdmissionFallback("client_report_invalid") from exc
    client_report = dict(declared["report"])
    report: dict[str, Any] = {
        "ok": False,
        "error_count": sum(issue["severity"] == "ERROR" for issue in issues),
        "warning_count": sum(issue["severity"] == "WARNING" for issue in issues),
        "issues": issues,
        "media": {},
        "media_validation": client_report.get("media_validation"),
        "qrdf_runtime": {"sdk_version": str(declared["qrdf_version"]), "producer": "client"},
        "source_fingerprint": source_fingerprint,
        "integrity_source": "client",
        "client_admission": {
            "qrdf_version": declared["qrdf_version"],
            "policy_version": declared["policy_version"],
            "report": client_report,
            "judgement": {
                "integrity_status": integrity_status,
                "non_blocking_issue_codes": sorted(NON_BLOCKING_ISSUE_CODES),
            },
        },
    }
    if integrity_status == "failed":
        # The server's own verdict: a blocking error is final, never a fallback.
        failed = QRDFAdmissionResult(
            ok=False,
            integrity_status="failed",
            preview_status="not_applicable",
            output_verification_status="failed",
            report=report,
            issues=tuple(issues),
            qrdf_profile="qrdf",
        )
        return failed, dict(asdict(source_ref))

    check_owned()
    raw = _head(provider, source_ref)
    if (
        raw is None
        or raw.size_bytes != source_ref.size_bytes
        or not raw.etag
        or (source_ref.etag and raw.etag != source_ref.etag)
        or (source_ref.version_id and raw.version_id != source_ref.version_id)
    ):
        raise ClientAdmissionFallback("client_source_object_mismatch")
    files = [dict(item) for item in declared.get("files") or []]
    heads = _preview_heads(provider, files)
    declared_sha256 = {str(item["path"]): str(item["sha256"]) for item in files}
    metadata_text = str(state["metadata_text"])
    (scratch / "metadata.json").write_text(metadata_text, encoding="utf-8")
    episode = _DeclaredEpisode(scratch)
    kinds: dict[str, tuple[str, str | None]] = {}
    if not files:
        if _is_rgb_episode(episode):
            raise ClientAdmissionFallback("client_preview_missing")
        preview_status, media_facts = "not_applicable", {"status": "not_applicable"}
    else:
        check_owned()
        for item in files:
            destination = scratch / str(item["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                provider.download_file(heads[str(item["path"])], str(destination))
            except Exception as exc:  # noqa: BLE001 - any provider failure means no usable preview.
                raise ClientAdmissionFallback("client_preview_object_missing") from exc
            if (
                destination.is_symlink()
                or destination.stat().st_size != int(item["size_bytes"])
                or _sha256_file(destination) != item["sha256"]
            ):
                raise ClientAdmissionFallback("client_preview_hash_mismatch")
        expected_source = {
            "data_mcap_sha256": str(state["declared_sha256"]),
            "metadata_sha256": hashlib.sha256(metadata_text.encode("utf-8")).hexdigest(),
        }
        valid, media_facts = _preview_artifacts_valid(
            scratch, episode, expected_source=expected_source
        )
        if not valid:
            raise ClientAdmissionFallback("client_preview_invalid", media_facts)
        kinds[PREVIEW_MANIFEST_PATH] = ("preview_manifest", None)
        for stream in media_facts["streams"]:
            kinds[f"media/preview/{stream['video']}"] = ("preview_video", stream["topic"])
            kinds[f"media/preview/{stream['timeline']}"] = ("preview_timeline", stream["topic"])
        if set(kinds) != set(heads):
            raise ClientAdmissionFallback("client_preview_files_mismatch")
        preview_status = "ready"

    report["ok"] = True
    report["media"] = media_facts
    report_path = scratch / "admission-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    prefix = f"process/v2/qrdf/{uuid4().hex}"
    objects = [
        _publish(process_provider, scratch / "metadata.json", prefix, "metadata.json", "metadata"),
        _publish(
            process_provider, report_path, prefix, "admission-report.json", "admission_report"
        ),
    ]
    for path in sorted(kinds):
        kind, topic = kinds[path]
        objects.append(
            object_entry(
                path=path,
                kind=kind,
                topic=topic,
                ref={**asdict(heads[path]), "sha256": declared_sha256[path]},
            )
        )
    verified_source = {
        **asdict(raw),
        "sha256": str(state["declared_sha256"]),
        "relative_path": str(state["data_path"]),
    }
    admitted = QRDFAdmissionResult(
        ok=True,
        integrity_status="passed",
        preview_status=preview_status,
        output_verification_status="verified",
        report=report,
        issues=tuple(issues),
        qrdf_profile="qrdf",
        objects=tuple(objects),
    )
    return admitted, verified_source
```

- [ ] **Step 6: worker 分派**。`run_qrdf_admission_worker` 中从 `    provider = None` 开始、到函数最后一行 `    return {"episode_id": episode_id, "attempt": attempt, "admission": result.to_dict()}` 为止整段替换为：

```python
provider = None
result = None
uploaded_objects = []
verified_source = dict(source_object)
error_code = ""
integrity_source = "server"
report_extra: dict[str, Any] = {"integrity_source": "server"}
try:
    # No job-supplied filesystem paths or artifact keys are accepted.
    parent = Path(settings.storage_root).absolute() / "admission-scratch"
    for component in [parent, *parent.parents]:
        if component.is_symlink():
            raise AdmissionSourceError("SOURCE_SCRATCH_UNSAFE")
    parent.mkdir(parents=True, exist_ok=True)
    data_path = Path(state["data_path"])
    if (
        data_path.is_absolute()
        or ".." in data_path.parts
        or "\\" in str(data_path)
        or str(data_path) == "metadata.json"
    ):
        raise AdmissionSourceError("SOURCE_PATH_UNSAFE")
    metadata = json.loads(state["metadata_text"])
    if metadata.get("data_file") != data_path.as_posix():
        raise AdmissionSourceError("SOURCE_PATH_UNSAFE")
    ref = StorageObjectRef(**source_object)
    from data.services.import_intake import MAX_IMPORT_TOTAL_BYTES

    if ref.bucket_role != "raw" or not ref.object_key or not (ref.version_id or ref.etag):
        raise AdmissionSourceError("SOURCE_IDENTITY_INVALID")
    if not 0 < ref.size_bytes <= MAX_IMPORT_TOTAL_BYTES:
        raise AdmissionSourceError("SOURCE_SIZE_INVALID")
    provider = get_storage_provider()

    class FencedProvider:
        def put_worker_object(self, ref, path):
            check_owned()
            try:
                persisted = provider.put_worker_object(ref, path)
            except Exception as exc:
                raise AdmissionSourceError("PROCESS_UPLOAD_FAILED") from exc
            uploaded_objects.append(persisted)
            return persisted

    client_outcome = None
    if state.get("client_admission"):
        from data.integrations.qrdf.client_admission import (
            ClientAdmissionFallback,
            admit_client_prechecked,
        )

        with tempfile.TemporaryDirectory(prefix="client-", dir=parent) as client_temporary:
            try:
                client_outcome = admit_client_prechecked(
                    Path(client_temporary),
                    state=state,
                    source_ref=ref,
                    source_fingerprint=source_fingerprint,
                    provider=provider,
                    process_provider=FencedProvider(),
                    check_owned=check_owned,
                )
            except ClientAdmissionFallback as fallback:
                for persisted in uploaded_objects:
                    provider.delete_exact(persisted)
                uploaded_objects.clear()
                report_extra["client_admission_fallback"] = fallback.reason
                if fallback.detail:
                    report_extra["client_admission_fallback_detail"] = fallback.detail
    if client_outcome is not None:
        result, verified_source = client_outcome
        integrity_source = "client"
    else:
        with tempfile.TemporaryDirectory(prefix="source-", dir=parent) as temporary:
            root = Path(temporary)
            check_owned()
            destination = root / data_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                downloaded = provider.download_file(ref, str(destination))
            except Exception as exc:
                raise AdmissionSourceError("SOURCE_DOWNLOAD_FAILED") from exc
            if (
                downloaded.object_key != ref.object_key
                or downloaded.bucket_role != ref.bucket_role
                or (ref.version_id and downloaded.version_id != ref.version_id)
                or (ref.etag and downloaded.etag != ref.etag)
                or downloaded.size_bytes != ref.size_bytes
                or destination.stat().st_size != ref.size_bytes
                or destination.is_symlink()
            ):
                raise AdmissionSourceError("SOURCE_IDENTITY_CHANGED")
            verified_source = {
                **asdict(downloaded),
                "sha256": _sha256_file(destination),
                "relative_path": data_path.as_posix(),
            }
            if verified_source["sha256"] != state["declared_sha256"]:
                raise AdmissionSourceError("SOURCE_DECLARED_HASH_MISMATCH")
            (root / "metadata.json").write_text(state["metadata_text"], encoding="utf-8")
            check_owned()
            result = admit_qrdf_episode(
                root,
                source_fingerprint=source_fingerprint,
                process_provider=FencedProvider(),
                report_extra=report_extra,
            )
except LeaseOwnershipLost:
    for persisted in uploaded_objects:
        provider.delete_exact(persisted)
    raise
except Exception as exc:  # noqa: BLE001 - persist one failed source, not a failed session.
    for persisted in uploaded_objects:
        provider.delete_exact(persisted)
    uploaded_objects.clear()
    error_code = getattr(exc, "code", "SOURCE_PROCESSING_FAILED")
    integrity_source = "server"
    result = QRDFAdmissionResult(
        ok=False,
        integrity_status="failed",
        preview_status="failed",
        output_verification_status="failed",
        report={"error_code": error_code, "error_type": type(exc).__name__, **report_extra},
        issues=(),
        qrdf_profile="qrdf",
    )
try:
    session, latest, _ = fence(claimed=True)
    error_code = error_code or str(result.report.get("media", {}).get("error_code") or "")
    error_code = error_code or next(
        (
            str(i["code"])
            for i in result.issues
            if i["severity"] == "ERROR" and i["code"] not in TRAINING_READINESS_ISSUE_CODES
        ),
        "",
    )
    record_episode_admission_fact(
        db,
        episode_id=episode_id,
        attempt=attempt,
        source_fingerprint=source_fingerprint,
        validation_policy_version="v1",
        integrity_status=result.integrity_status,
        preview_status=result.preview_status,
        output_verification_status=result.output_verification_status,
        qrdf_profile=result.qrdf_profile,
        report_ref=result.report,
        objects=[
            object_entry(path=verified_source["relative_path"], kind="data", ref=verified_source),
            *result.objects,
        ]
        if result.output_verification_status == "verified"
        else [],
        error_code=error_code,
        integrity_source=integrity_source,
    )
    if result.output_verification_status == "verified":
        # External tools fetch the exact verified bytes through the fetch
        # manifest, which reads published Episode artifacts.
        _record_verified_source_artifact(
            db,
            episode_id=episode_id,
            source=verified_source,
            source_fingerprint=source_fingerprint,
        )
    latest.update(
        status="ready" if result.output_verification_status == "verified" else "failed",
        error_code=error_code,
        integrity_source=integrity_source,
    )
    if report_extra.get("client_admission_fallback"):
        latest["client_admission_fallback"] = report_extra["client_admission_fallback"]
    session.result_json = {
        **session.result_json,
        "admission_sources": {
            **session.result_json["admission_sources"],
            source_id: latest,
        },
    }
    refresh_upload_admission_counts(db, session)
    db.commit()
except Exception:
    db.rollback()
    for persisted in uploaded_objects:
        provider.delete_exact(persisted)
    raise
return {"episode_id": episode_id, "attempt": attempt, "admission": result.to_dict()}
```

说明：客户端模式在 `check_owned()` 之后才 HEAD/下载；服务端模式保留原有顺序（`check_owned()` → 下载 MCAP → 校验身份 → 写 metadata → `check_owned()` → `admit_qrdf_episode`）。回退时已上传的 worker 对象（metadata/report）会被删除；客户端上传的 preview 对象不删除（1.0 不做回收）。

- [ ] **Step 7: 运行测试**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_worker.py tests/test_collection_admission_worker.py tests/test_qrdf_admission.py tests/test_collection_upload_parse.py -q -p no:cacheprovider`
Expected: 全部 PASS。

- [ ] **Step 8: Commit**

```bash
git add backend/data/integrations/qrdf/admission.py backend/data/integrations/qrdf/client_admission.py backend/tests/test_client_admission_worker.py backend/tests/test_collection_admission_worker.py backend/tests/test_qrdf_admission.py
git commit -m "feat(admission): worker 客户端预检模式，不下载 MCAP，失败自动回退服务端模式"
```

---

### Task 7: HTTP 真实字节端到端回归与全量门禁

**Files:**
- Modify: `backend/tests/test_collection_upload_bytes.py`（追加客户端预检端到端用例）

**Interfaces:**
- Consumes: Task 1–6 全部 HTTP 接口；Task 5 `generate_previews`；`tests.test_collection_admission_worker.claimed_job`。
- Produces: 无新代码接口；这是 spec §9 “真实字节回归：`test_collection_upload_bytes.py` 扩展客户端预检路径”。

- [ ] **Step 1: 写测试**。`backend/tests/test_collection_upload_bytes.py` 顶部 import 区补充：

```python
import base64
from uuid import uuid4

from tests.test_client_admission_worker import generate_previews
from tests.test_collection_admission_worker import claimed_job
from tests.test_qrdf_admission import _episode

from data.infra import oss_client
from data.infra.object_storage import StorageObjectNotFound, StorageObjectRef
from data.integrations.qrdf.admission import run_qrdf_admission_worker
```

（`hashlib`、`json`、`Path`、`pytest`、`settings`、`CollectionUploadSession` 与 `tests.collection_api_fixtures` 的三个工厂函数该文件已导入。）文件末尾追加：

```python
class FakeCloud:
    """One in-memory object store behind both the browser multipart API and the provider."""

    def __init__(self, db):
        self.db = db
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict] = {}
        self.downloads: list[tuple[str, str]] = []

    @staticmethod
    def _etag(key):
        return "etag-" + hashlib.sha256(key.encode()).hexdigest()[:16]

    def init(self, bucket, key):
        upload_id = f"upload-{uuid4().hex}"
        self.uploads[upload_id] = {"bucket": bucket, "key": key, "parts": {}}
        return upload_id

    def sign(self, bucket, key, upload_id, part_number, content_md5=None):
        headers = {"Content-Type": "application/octet-stream"}
        if content_md5:
            headers["Content-MD5"] = content_md5
        return f"https://oss.test/{upload_id}/{part_number}", 300, headers

    def put_part(self, url, content, headers):
        """What the storage service does with one signed UploadPart request."""
        _prefix, upload_id, part = url.rsplit("/", 2)
        digest = base64.b64encode(hashlib.md5(content, usedforsecurity=False).digest()).decode()
        assert headers.get("Content-MD5") == digest, "storage rejects a corrupted part"
        self.uploads[upload_id]["parts"][int(part)] = content
        return f"part-etag-{part}"

    def list_parts(self, _bucket, _key, upload_id):
        parts = self.uploads[upload_id]["parts"]
        return [
            oss_client.OSSMultipartPart(
                number=number, etag=f"part-etag-{number}", size=len(parts[number])
            )
            for number in sorted(parts)
        ]

    def complete(self, _bucket, key, upload_id, parts):
        stored = self.uploads[upload_id]["parts"]
        self.objects[key] = b"".join(stored[part.number] for part in parts)

    def object_info(self, _bucket, key):
        if key not in self.objects:
            return None
        return oss_client.OSSObjectInfo(
            size=len(self.objects[key]),
            etag=self._etag(key),
            crc64="1234",
            version_id=None,
            metadata={},
        )

    def head(self, ref):
        if ref.object_key not in self.objects:
            raise StorageObjectNotFound(ref.object_key)
        size = len(self.objects[ref.object_key])
        return StorageObjectRef(
            ref.bucket_role, ref.object_key, None, self._etag(ref.object_key), size, None
        )

    def download_file(self, ref, destination):
        assert not self.db.in_transaction(), "download must not hold a DB transaction"
        self.downloads.append((ref.bucket_role, ref.object_key))
        head = self.head(ref)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(self.objects[ref.object_key])
        return head

    def put_worker_object(self, ref, source_path):
        data = Path(source_path).read_bytes()
        self.objects[ref.object_key] = data
        return StorageObjectRef(
            "process",
            ref.object_key,
            None,
            self._etag(ref.object_key),
            len(data),
            hashlib.sha256(data).hexdigest(),
        )

    def delete_exact(self, ref):
        self.objects.pop(ref.object_key, None)

    def sign_get(self, ref, *, expires=900):
        assert ref.object_key in self.objects
        return f"https://oss.test/get/{ref.object_key}"


def _upload_file(client, headers, cloud, session_id, workspace_id, target, payload):
    base = f"/api/v1/upload-sessions/{session_id}/oss"
    initialized = client.post(
        f"{base}/init",
        headers=headers,
        json={"workspace_id": workspace_id, "total_size_bytes": len(payload), **target},
    )
    assert initialized.status_code == 200, initialized.text
    part_size = initialized.json()["data"]["part_size_bytes"]
    parts = []
    for index in range(initialized.json()["data"]["total_parts"]):
        chunk = payload[index * part_size : (index + 1) * part_size]
        md5 = base64.b64encode(hashlib.md5(chunk, usedforsecurity=False).digest()).decode()
        signed = client.post(
            f"{base}/sign-part",
            headers=headers,
            json={
                "workspace_id": workspace_id,
                "part_number": index + 1,
                "content_md5": md5,
                **target,
            },
        )
        assert signed.status_code == 200, signed.text
        data = signed.json()["data"]
        parts.append(
            {"part_number": index + 1, "etag": cloud.put_part(data["url"], chunk, data["headers"])}
        )
    completed = client.post(
        f"{base}/complete",
        headers=headers,
        json={"workspace_id": workspace_id, "parts": parts, **target},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()["data"]["status"]


def test_client_precheck_bytes_reach_review_without_downloading_the_mcap(
    client, db_session, admin_headers, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))
    cloud = FakeCloud(db_session)
    monkeypatch.setattr(oss_client, "init_browser_multipart_upload", cloud.init)
    monkeypatch.setattr(oss_client, "sign_browser_upload_part", cloud.sign)
    monkeypatch.setattr(oss_client, "list_browser_multipart_parts", cloud.list_parts)
    monkeypatch.setattr(oss_client, "complete_browser_multipart_upload", cloud.complete)
    monkeypatch.setattr(oss_client, "object_info", cloud.object_info)
    monkeypatch.setattr("data.infra.storage_provider.get_storage_provider", lambda: cloud)

    root = _episode(tmp_path)
    preview_paths = generate_previews(root)
    data = (root / "data.mcap").read_bytes()
    metadata_text = (root / "metadata.json").read_text(encoding="utf-8")
    metadata = json.loads(metadata_text)
    digest = hashlib.sha256(data).hexdigest()
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    package = make_assigned_package(db_session, workspace, project)
    capabilities = client.get(
        "/api/v1/upload-sessions/capabilities",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    ).json()["data"]["client_admission"]
    assert capabilities["enabled"] is True
    created = client.post(
        "/api/v1/upload-sessions",
        headers=admin_headers,
        json={
            "workspace_id": workspace.id,
            "collection_project_id": project.id,
            "package_uids": [package.package_uid],
            "upload_mode": "oss_multipart",
        },
    )
    session_id = created.json()["data"]["id"]
    item = {
        "package_uid": package.package_uid,
        "source": {
            "episode_id": metadata["episode_id"],
            "start_ns": str(metadata["timing"]["start_timestamp_ns"]),
            "end_ns": str(metadata["timing"]["end_timestamp_ns"]),
            "metadata_sha256": hashlib.sha256(metadata_text.encode()).hexdigest(),
            "data_mcap_sha256": digest,
        },
        "metadata_text": metadata_text,
        "data_file": {"path": "data.mcap", "size_bytes": len(data), "sha256": digest},
        "client_admission": {
            "qrdf_version": capabilities["accepted_qrdf_versions"][0],
            "policy_version": capabilities["accepted_policy_versions"][0],
            "report": {"ok": True, "issues": [], "media_validation": {"ok": True, "issues": []}},
            "files": [
                {
                    "path": path,
                    "size_bytes": (root / path).stat().st_size,
                    "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
                }
                for path in preview_paths
            ],
        },
    }
    declared = client.post(
        f"/api/v1/upload-sessions/{session_id}/declarations",
        headers=admin_headers,
        json={"workspace_id": workspace.id, "items": [item]},
    )
    assert declared.status_code == 200, declared.text
    [source] = declared.json()["data"]["sources"]

    statuses = [
        _upload_file(
            client,
            admin_headers,
            cloud,
            session_id,
            workspace.id,
            {"source_id": source["source_id"]},
            data,
        )
    ]
    for entry in source["preview_files"]:
        statuses.append(
            _upload_file(
                client,
                admin_headers,
                cloud,
                session_id,
                workspace.id,
                {"file_id": entry["file_id"]},
                (root / entry["path"]).read_bytes(),
            )
        )
    assert statuses[-1] == "uploaded" and set(statuses[:-1]) == {"uploading"}

    parse_collection_upload_session(db_session, session_id)
    upload = db_session.get(CollectionUploadSession, session_id)
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    db_session.expire_all()

    assert all(role == "process" for role, _key in cloud.downloads)
    detail = client.get(
        f"/api/v1/data-packages/{package.id}",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    ).json()["data"]
    assert detail["status"] == "pending_intake_review"
    [episode] = detail["episodes"]
    assert episode["integrity_source"] == "client"
    assert episode["preview_available"] is True
    [status] = detail["qrdf_facts"]["source_admission"]["sources"]
    assert status["source_id"] == source["source_id"]
    assert status["status"] == "ready" and status["integrity_source"] == "client"
    previews = client.get(
        f"/api/v1/data-packages/{package.id}/episodes/{episode['id']}/preview-urls",
        params={"workspace_id": workspace.id},
        headers=admin_headers,
    )
    assert previews.status_code == 200, previews.text
    assert previews.json()["data"]["streams"]
```

（`parse_collection_upload_session`、`Path` 已在该文件顶部导入；包详情的状态键是 `_package_item` 中的 `status`。）

- [ ] **Step 2: 运行**

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_collection_upload_bytes.py -q -p no:cacheprovider`
Expected: 全部 PASS。新用例若失败，按失败位置回到对应任务修正实现（不要放宽断言“不下载 raw”）。

- [ ] **Step 3: 文档复核**。确认 `docs/COLLECTION_UPLOAD_API.md` 的“客户端预检”一节与实现一致（HTTP 状态、`detail` 文本、`preview_files` 排序、`content_md5` 规则、回退原因表），并运行契约测试：

Run: `../.venv/bin/python -m pytest -p anyio.pytest_plugin tests/test_client_admission_contract.py tests/test_client_admission_declaration.py -q -p no:cacheprovider`
Expected: 全部 PASS。

- [ ] **Step 4: 全量门禁**

```bash
cd backend && source ../.superpowers/sdd/test-env.sh
../.venv/bin/python -m pytest -p anyio.pytest_plugin tests -q -p no:cacheprovider 2>&1 \
  | grep -E "^(FAILED|ERROR) " | sed 's/ - .*//' | sort -u > /tmp/plan2-full.txt
comm -13 <(sort -u ../.superpowers/sdd/baseline-full.txt) /tmp/plan2-full.txt
```

Expected: `comm` 无输出（失败集合是基线子集）。

- [ ] **Step 5: ruff**

Run: `../.venv/bin/python -m ruff check data tests && ../.venv/bin/python -m ruff format --check data tests`
Expected: 无错误（有格式差异时运行 `../.venv/bin/python -m ruff format data tests` 后重跑 Step 4 受影响的文件）。

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_collection_upload_bytes.py
git commit -m "test(upload): 客户端预检真实字节端到端回归"
```

---

## 自检记录

- **spec 覆盖**：§4.1 → Task 1（capabilities、配置、`non_blocking_issue_codes` 与 `TRAINING_READINESS_ISSUE_CODES` 同一对象）；§4.2 → Task 2（声明、路径规则、`file_id`）+ Task 3（process 桶 multipart、`Content-MD5`）；§4.3 → Task 5（重新判定、来源状态）+ Task 6（HEAD、不下载 MCAP、按声明指纹验证 preview、metadata/report 写 process 桶、`integrity_source="client"`）；§4.4 → Task 6（四种回退用例 + failed 不回退）；`integrity_source` → Task 4；§8 错误处理表中 Studio 行 → Task 3（分片 MD5 不符由存储拒收）、Task 6（回退/failed）；§9 Studio 测试 → Task 2/3/6/7；§10 兼容（旧 Duance 无 `client_admission`、新 Duance 连旧 Studio 得到 404）→ Task 1 文档 + Task 2 `test_declaration_without_client_admission_is_unchanged`。§4.5/§4.6/§6 已由计划 1 完成；§4.7 不在本期。
- **spec 歧义的处理**：见下一节“已解决的 spec 歧义”。
- **类型一致性**：`preview_file_id`、`client_admission_state`、`judge_client_report`、`ClientAdmissionFallback.reason/detail`、`oss_multipart_files[file_id].provider_identity`、`record_episode_admission_fact(integrity_source=)` 在生产与消费任务中名称一致。

## 已解决的 spec 歧义

1. **现有代码没有上传 capabilities 接口**：新增 `GET /upload-sessions/capabilities?workspace_id=`，路由必须注册在 `/{upload_session_id}` 之前；旧 Studio 返回 404，客户端据此走旧链路。
2. **`file_id` 与现有 `source_id`**：MCAP 继续用 `source_id`；preview 文件用新的 `file_id = sha256("{source_id}:{path}")`，在同样的 `/oss/init|sign-part|complete` 请求体中代替 `source_id`，两者互斥。声明响应在对应来源下返回 `preview_files[]`。
3. **`qrdf_version` 的含义**：spec 示例写 `0.2.0`（数据格式版本）；实际指 SDK 包版本，白名单默认 `["0.2.1"]`，因为 EGO 别名与硬件编码要求 SDK ≥ 0.2.1。
4. **“报告缺字段”是拒绝还是回退**：声明时由 Pydantic 严格拒绝（422），客户端能立即修正；worker 仍做防御性检查，失败回退 `client_report_invalid`。版本白名单与总开关只在 worker 处理时判断（配置可能在声明后变化），因此是回退而不是拒绝。
5. **`Content-MD5` 如何生效**：OSS/S3 的预签名 URL 必须把 `Content-MD5` 纳入签名，所以 `sign-part` 新增 `content_md5` 入参并在响应 `headers` 中返回；preview 必填，MCAP 可选（兼容旧 Duance）。
6. **preview 未上传完时的会话状态**：全部 MCAP 与 preview 文件都 complete 才进入 `uploaded` 并触发解析。
7. **非 RGB episode**：`files` 可为空；metadata 声明了 RGB 流却无 preview 时回退 `client_preview_missing`。
8. **客户端模式下 raw 的 `sha256`**：取声明值（由分片 MD5 与声明共同保证），`integrity_source="client"` 表明其来源；preview 的 CRC64 不强制，因为 worker 会下载并校验 SHA-256。
9. **回退后客户端 preview 对象**：不删除，1.0 不做回收。
