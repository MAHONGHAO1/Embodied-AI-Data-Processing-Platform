# Studio Episode 对象清单替代 process tar 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** admission 事实用一个 episode 对象清单（`objects_json`）描述 episode 的全部存储对象，不再打 `process.tar`；预览播放、数据资产快照、导出与外部拉取都按清单读取。

**Architecture:** 先扩后收。Task 3–4 新增 `objects_json` 并让 worker 同时写新旧两种格式；Task 5–7 把读取方逐个切到清单（每个任务后全量测试保持与基线一致）；Task 8 停止打 tar、删除 `process_ref_json` / `source_objects_json` 与 tar 代码。清单的构造与校验集中在新模块 `data/services/episode_objects.py`，其他代码只通过它读写。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy、Alembic、PostgreSQL、pytest、qrdf SDK（vendored）。

**Spec:** `docs/superpowers/specs/2026-09-28-client-precheck-preview-design.md`（§2 决策“删除 process tar”、§4.4、§4.5、§4.6、§6）。本计划是该 spec 的第 1 个实现计划；客户端预检模式（§4.1–§4.3，含 `integrity_source` 字段）是计划 2，Duance 是计划 3。

**范围说明：** spec §4.6 列出的 `derived_preview_batch`、`legacy_preview_migration`、`_record_verified_source_artifact` 读写的是 `EpisodeArtifact` 表而非 admission 事实的 `process_ref_json` / `source_objects_json`，本计划不改动它们。

## Global Constraints

- 对象清单每项固定为 `{"path", "role", "kind", "ref"}`，预览媒体另有 `"topic"`：
  - `path`：QRDF episode 目录内的 POSIX 相对路径，如 `data.mcap`、`metadata.json`、`admission-report.json`、`media/preview/manifest.json`、`media/preview/generations/<id>/head_rgb.mp4`。
  - `role`：`raw` 或 `process`，必须等于 `ref.bucket_role`。
  - `kind`：`data`、`metadata`、`admission_report`、`preview_manifest`、`preview_video`、`preview_timeline` 之一。
  - `ref`：完整存储身份 `{"bucket_role", "object_key", "version_id", "etag", "size_bytes", "sha256"}`；`etag` 非空，`sha256` 为 64 位小写十六进制。
- 已校验通过（`output_verification_status == "verified"`）的清单必须恰好含一个 `data`、一个 `metadata`、一个 `admission_report`；有 RGB 时每个 topic 各一个 `preview_video`、`preview_timeline`，并有一个 `preview_manifest`。
- 1.0 开发阶段不兼容历史数据：迁移不回填旧行，旧的 admission 事实与已冻结快照在本计划后需要重新入库，不做兼容读取。
- Studio 测试必须使用独立 PostgreSQL/Redis。本机测试库为容器 `quicstudio-gap-postgres`（库 `quicdata_test_gap`，用户 `gaptest`），在仓库根目录运行：

  ```bash
  PW=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' quicstudio-gap-postgres | sed -n 's/^POSTGRES_PASSWORD=//p')
  export TEST_DATABASE_URL="postgresql+psycopg://gaptest:${PW}@127.0.0.1:15432/quicdata_test_gap"
  export TEST_REDIS_URL=redis://127.0.0.1:16379/15 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
  .venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/<file>.py -q
  ```

  下文 “Run:” 只写 pytest 部分，均假定已 export 上述变量。
- 基线（开始前已失败，与本计划无关，不修）：`test_episode_admission.py` 中 5 个与 intake review/已建批规则相关的用例（`package_intake_finalized`、`already_batched` 等），`test_asset_source_snapshots.py` 中 `test_asset_snapshot_freezes_annotation_and_admission_refs`、`test_asset_uses_batch_admission_attempt_after_newer_current_fact`。每个任务结束时，受影响文件的失败集合必须是基线的子集。
- vendored qrdf 必须 ≥ 0.2.1（含 EGO 别名路由 `EGO_EPISODE_TYPE_ALIASES`）。

---

### Task 1: vendored qrdf 升级到 0.2.1，删除 EGO metadata 改写

**Files:**
- Modify: `scripts/fetch-qrdf.sh`（`_vendor_sdk_supported`）
- Modify: `backend/data/integrations/qrdf/admission.py:43-68`（删除 `_EGO_EPISODE_TYPE_ALIASES`、`_normalize_ego_metadata`）及其调用处（`admit_qrdf_episode` 开头的 `_normalize_ego_metadata(path)`）
- Test: `backend/tests/test_qrdf_admission.py`

**Interfaces:**
- Consumes: qrdf 0.2.1 的 `qrdf.registry.topic_layout.EGO_EPISODE_TYPE_ALIASES`、`detect_episode_type`
- Produces: `admit_qrdf_episode` 不再修改 episode 目录中的 `metadata.json`

- [ ] **Step 1: 同步 vendored qrdf 并确认版本**

```bash
QRDF_FORCE_FETCH=1 bash scripts/fetch-qrdf.sh
.venv/bin/python -m pip install -e "backend/vendor/qrdf[dev]" -q
.venv/bin/python -c "import qrdf; from qrdf.registry.topic_layout import EGO_EPISODE_TYPE_ALIASES; print(qrdf.__version__, sorted(EGO_EPISODE_TYPE_ALIASES))"
```

Expected: `0.2.1 ['human_demonstration', 'human_ego_demo']`

- [ ] **Step 2: 写失败测试**——别名 episode 通过 admission 且 metadata 原文不变。在 `backend/tests/test_qrdf_admission.py` 末尾追加（`_episode` 为该文件已有的造 EGO episode 的辅助函数）：

```python
def test_ego_alias_is_admitted_without_rewriting_metadata(tmp_path):
    root = _episode(tmp_path)
    metadata_path = root / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.setdefault("capture", {})["episode_type"] = "human_demonstration"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    before = metadata_path.read_bytes()

    result = admit_qrdf_episode(root, generate_preview=True)

    assert metadata_path.read_bytes() == before
    assert result.integrity_status == "passed"
    codes = {issue["code"] for issue in result.issues}
    assert "NO_ACTION_TOPIC" not in codes and "NO_STATE_TOPIC" not in codes
```

若文件顶部未导入 `json` 或 `admit_qrdf_episode`，一并补上 `import json` 与 `from data.integrations.qrdf.admission import admit_qrdf_episode`。

- [ ] **Step 3: 运行，确认失败**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_qrdf_admission.py::test_ego_alias_is_admitted_without_rewriting_metadata -q`
Expected: FAIL，`metadata_path.read_bytes() == before` 断言失败（当前 `_normalize_ego_metadata` 会改写文件）。

- [ ] **Step 4: 删除改写逻辑**

在 `admission.py` 删除 `_EGO_EPISODE_TYPE_ALIASES` 常量、`_normalize_ego_metadata` 函数，以及 `admit_qrdf_episode` 中的 `_normalize_ego_metadata(path)` 调用。同时删除它上方描述别名的注释。

- [ ] **Step 5: 让部署拉取时拒绝旧 SDK**

`scripts/fetch-qrdf.sh` 的 `_vendor_sdk_supported` 末尾追加一个条件：

```bash
  local topic_layout="$VENDOR_DIR/qrdf/registry/topic_layout.py"
  grep -q 'canonical_schema_name' "$reader" \
    && grep -q 'descriptor_format' "$reader" \
    && grep -q 'is_legacy_schema' "$reader" \
    && grep -q 'def get_canonical_schema_name' "$reader" \
    && grep -q 'def resolve_data_file' "$episode" \
    && grep -q 'EGO_EPISODE_TYPE_ALIASES' "$topic_layout"
```

（替换原有的 `grep ... && grep -q 'def resolve_data_file' "$episode"` 链，`local topic_layout` 放在 `local episode=` 下一行。）

- [ ] **Step 6: 运行测试**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_qrdf_admission.py backend/tests/test_collection_admission_worker.py -q`
Expected: 全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add scripts/fetch-qrdf.sh backend/data/integrations/qrdf/admission.py backend/tests/test_qrdf_admission.py
git commit -m "refactor(admission): EGO 别名交由 qrdf 0.2.1 路由，不再改写 metadata"
```

---

### Task 2: episode 对象清单模块

**Files:**
- Create: `backend/data/services/episode_objects.py`
- Test: `backend/tests/test_episode_objects.py`

**Interfaces:**
- Produces（后续任务只通过这些名字读写清单）：
  - `OBJECT_KINDS: frozenset[str]`
  - `class EpisodeObjectsError(ValueError)`，`.code: str`
  - `@dataclass(frozen=True) class EpisodeObject: path: str; role: str; kind: str; ref: dict[str, Any]; topic: str | None`，方法 `storage_ref() -> StorageObjectRef`、`as_entry() -> dict[str, Any]`
  - `object_entry(*, path: str, kind: str, ref: dict[str, Any], topic: str | None = None) -> dict[str, Any]`
  - `parse_objects(entries: Any) -> list[EpisodeObject]`
  - `require_verified_objects(objects: list[EpisodeObject]) -> None`
  - `data_object(objects: list[EpisodeObject]) -> EpisodeObject`
  - `object_of_kind(objects: list[EpisodeObject], kind: str) -> EpisodeObject`
  - `preview_streams(objects: list[EpisodeObject]) -> list[dict[str, EpisodeObject | str]]`，每项 `{"topic": str, "video": EpisodeObject, "timeline": EpisodeObject}`，按 topic 排序
  - `fact_objects(fact) -> list[EpisodeObject]`（读 `fact.objects_json`）

- [ ] **Step 1: 写失败测试** `backend/tests/test_episode_objects.py`：

```python
"""Episode object manifest: the only way admission facts describe storage."""

import pytest

from data.services.episode_objects import (
    EpisodeObjectsError,
    data_object,
    object_entry,
    object_of_kind,
    parse_objects,
    preview_streams,
    require_verified_objects,
)

SHA = "a" * 64


def ref(role="process", key="process/v2/qrdf/x/file", size=10, sha=SHA, etag="e1"):
    return {
        "bucket_role": role,
        "object_key": key,
        "version_id": "v1",
        "etag": etag,
        "size_bytes": size,
        "sha256": sha,
    }


def verified_entries():
    return [
        object_entry(path="data.mcap", kind="data", ref=ref("raw", "raw/v2/a/source.mcap")),
        object_entry(path="metadata.json", kind="metadata", ref=ref(key="p/metadata.json")),
        object_entry(
            path="admission-report.json", kind="admission_report", ref=ref(key="p/report.json")
        ),
        object_entry(
            path="media/preview/manifest.json", kind="preview_manifest", ref=ref(key="p/m.json")
        ),
        object_entry(
            path="media/preview/generations/g1/head_rgb.mp4",
            kind="preview_video",
            topic="/camera/head/rgb",
            ref=ref(key="p/head.mp4"),
        ),
        object_entry(
            path="media/preview/generations/g1/head_rgb.timeline.json",
            kind="preview_timeline",
            topic="/camera/head/rgb",
            ref=ref(key="p/head.timeline.json"),
        ),
    ]


def test_round_trip_and_lookups():
    objects = parse_objects(verified_entries())
    require_verified_objects(objects)
    assert data_object(objects).path == "data.mcap"
    assert data_object(objects).storage_ref().bucket_role == "raw"
    assert object_of_kind(objects, "admission_report").path == "admission-report.json"
    [stream] = preview_streams(objects)
    assert stream["topic"] == "/camera/head/rgb"
    assert stream["video"].path.endswith(".mp4")
    assert stream["timeline"].path.endswith(".timeline.json")
    assert [o.as_entry() for o in objects] == verified_entries()


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda e: e[0].update(path="../data.mcap"), "episode_object_path_unsafe"),
        (lambda e: e[0].update(path="/abs/data.mcap"), "episode_object_path_unsafe"),
        (lambda e: e[0].update(kind="tarball"), "episode_object_kind_invalid"),
        (lambda e: e[0].update(role="process"), "episode_object_role_mismatch"),
        (lambda e: e[1]["ref"].update(sha256="short"), "episode_object_ref_invalid"),
        (lambda e: e[1]["ref"].update(etag=""), "episode_object_ref_invalid"),
        (lambda e: e.append(dict(e[1])), "episode_object_path_duplicate"),
        (lambda e: e[4].pop("topic"), "episode_object_topic_missing"),
    ],
)
def test_parse_rejects_invalid_entries(mutate, code):
    entries = verified_entries()
    mutate(entries)
    with pytest.raises(EpisodeObjectsError) as excinfo:
        parse_objects(entries)
    assert excinfo.value.code == code


def test_verified_manifest_requires_core_objects_and_complete_streams():
    entries = [e for e in verified_entries() if e["kind"] != "admission_report"]
    with pytest.raises(EpisodeObjectsError) as excinfo:
        require_verified_objects(parse_objects(entries))
    assert excinfo.value.code == "episode_object_missing_admission_report"

    entries = [e for e in verified_entries() if e["kind"] != "preview_timeline"]
    with pytest.raises(EpisodeObjectsError) as excinfo:
        require_verified_objects(parse_objects(entries))
    assert excinfo.value.code == "episode_object_preview_stream_incomplete"


def test_non_rgb_episode_needs_no_preview_objects():
    entries = [e for e in verified_entries() if not e["kind"].startswith("preview_")]
    objects = parse_objects(entries)
    require_verified_objects(objects)
    assert preview_streams(objects) == []


def test_parse_accepts_empty_and_rejects_non_list():
    assert parse_objects([]) == []
    with pytest.raises(EpisodeObjectsError):
        parse_objects({"object_key": "x"})
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_episode_objects.py -q`
Expected: FAIL，`ModuleNotFoundError: data.services.episode_objects`

- [ ] **Step 3: 实现** `backend/data/services/episode_objects.py`：

```python
"""Episode object manifest stored on admission facts.

One list describes every storage object that makes up an admitted episode:
the raw data file plus the process objects produced by admission.  Each entry's
``path`` is its location inside a standard QRDF episode directory, so any
consumer can rebuild the episode by downloading every entry to its path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from data.infra.object_storage import StorageObjectRef

OBJECT_KINDS = frozenset(
    {
        "data",
        "metadata",
        "admission_report",
        "preview_manifest",
        "preview_video",
        "preview_timeline",
    }
)
_PREVIEW_MEDIA_KINDS = frozenset({"preview_video", "preview_timeline"})
_SINGLETON_KINDS = ("data", "metadata", "admission_report")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class EpisodeObjectsError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EpisodeObject:
    path: str
    role: str
    kind: str
    ref: dict[str, Any]
    topic: str | None = None

    def storage_ref(self) -> StorageObjectRef:
        return StorageObjectRef(
            self.ref["bucket_role"],
            self.ref["object_key"],
            self.ref.get("version_id"),
            self.ref["etag"],
            self.ref["size_bytes"],
            self.ref["sha256"],
        )

    def as_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "path": self.path,
            "role": self.role,
            "kind": self.kind,
            "ref": dict(self.ref),
        }
        if self.topic is not None:
            entry["topic"] = self.topic
        return entry


def object_entry(
    *, path: str, kind: str, ref: dict[str, Any], topic: str | None = None
) -> dict[str, Any]:
    """Build one validated manifest entry; ``role`` always mirrors the bucket."""
    entry: dict[str, Any] = {
        "path": path,
        "role": str(ref.get("bucket_role") or ""),
        "kind": kind,
        "ref": {
            "bucket_role": ref.get("bucket_role"),
            "object_key": ref.get("object_key"),
            "version_id": ref.get("version_id"),
            "etag": ref.get("etag"),
            "size_bytes": ref.get("size_bytes"),
            "sha256": ref.get("sha256"),
        },
    }
    if topic is not None:
        entry["topic"] = topic
    return parse_objects([entry])[0].as_entry()


def _safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise EpisodeObjectsError("episode_object_path_unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise EpisodeObjectsError("episode_object_path_unsafe")
    return path.as_posix()


def _valid_ref(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EpisodeObjectsError("episode_object_ref_invalid")
    role = raw.get("bucket_role")
    key = raw.get("object_key")
    etag = raw.get("etag")
    version = raw.get("version_id")
    size = raw.get("size_bytes")
    sha = raw.get("sha256")
    if (
        role not in {"raw", "process"}
        or not isinstance(key, str)
        or not key
        or key.startswith("/")
        or not isinstance(etag, str)
        or not etag
        or (version is not None and (not isinstance(version, str) or not version))
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(sha, str)
        or _SHA256_RE.fullmatch(sha) is None
    ):
        raise EpisodeObjectsError("episode_object_ref_invalid")
    return {
        "bucket_role": role,
        "object_key": key,
        "version_id": version,
        "etag": etag,
        "size_bytes": size,
        "sha256": sha,
    }


def parse_objects(entries: Any) -> list[EpisodeObject]:
    """Validate a stored manifest; fail closed on anything unexpected."""
    if not isinstance(entries, list):
        raise EpisodeObjectsError("episode_objects_invalid")
    objects: list[EpisodeObject] = []
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise EpisodeObjectsError("episode_objects_invalid")
        path = _safe_path(raw.get("path"))
        if path in seen:
            raise EpisodeObjectsError("episode_object_path_duplicate")
        seen.add(path)
        kind = raw.get("kind")
        if kind not in OBJECT_KINDS:
            raise EpisodeObjectsError("episode_object_kind_invalid")
        ref = _valid_ref(raw.get("ref"))
        if raw.get("role") != ref["bucket_role"]:
            raise EpisodeObjectsError("episode_object_role_mismatch")
        topic = raw.get("topic")
        if kind in _PREVIEW_MEDIA_KINDS:
            if not isinstance(topic, str) or not topic:
                raise EpisodeObjectsError("episode_object_topic_missing")
        elif topic is not None:
            raise EpisodeObjectsError("episode_object_topic_unexpected")
        objects.append(EpisodeObject(path, ref["bucket_role"], kind, ref, topic))
    return objects


def object_of_kind(objects: list[EpisodeObject], kind: str) -> EpisodeObject:
    matches = [item for item in objects if item.kind == kind]
    if len(matches) != 1:
        raise EpisodeObjectsError(f"episode_object_missing_{kind}")
    return matches[0]


def data_object(objects: list[EpisodeObject]) -> EpisodeObject:
    return object_of_kind(objects, "data")


def preview_streams(objects: list[EpisodeObject]) -> list[dict[str, Any]]:
    by_topic: dict[str, dict[str, Any]] = {}
    for item in objects:
        if item.kind not in _PREVIEW_MEDIA_KINDS:
            continue
        slot = "video" if item.kind == "preview_video" else "timeline"
        stream = by_topic.setdefault(item.topic, {"topic": item.topic})
        if slot in stream:
            raise EpisodeObjectsError("episode_object_preview_stream_duplicate")
        stream[slot] = item
    streams = [by_topic[topic] for topic in sorted(by_topic)]
    if any("video" not in stream or "timeline" not in stream for stream in streams):
        raise EpisodeObjectsError("episode_object_preview_stream_incomplete")
    return streams


def require_verified_objects(objects: list[EpisodeObject]) -> None:
    """A verified manifest can rebuild a complete, playable QRDF episode."""
    for kind in _SINGLETON_KINDS:
        object_of_kind(objects, kind)
    if data_object(objects).role != "raw":
        raise EpisodeObjectsError("episode_object_role_mismatch")
    streams = preview_streams(objects)
    manifests = [item for item in objects if item.kind == "preview_manifest"]
    if streams and len(manifests) != 1:
        raise EpisodeObjectsError("episode_object_missing_preview_manifest")


def fact_objects(fact: Any) -> list[EpisodeObject]:
    return parse_objects(getattr(fact, "objects_json", None) or [])
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_episode_objects.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/data/services/episode_objects.py backend/tests/test_episode_objects.py
git commit -m "feat(admission): 新增 episode 对象清单模块"
```

---

### Task 3: admission 事实增加 `objects_json`（扩）

**Files:**
- Create: `backend/alembic/versions/0008_episode_admission_objects.py`
- Modify: `backend/data/models/episode_admission.py`（新增列）
- Modify: `backend/data/services/episode_admission.py:25-145`（`record_episode_admission_fact` 新增 `objects` 参数）
- Test: `backend/tests/test_episode_admission.py`

**Interfaces:**
- Consumes: Task 2 `parse_objects`、`require_verified_objects`、`EpisodeObjectsError`
- Produces: `record_episode_admission_fact(..., objects: list[dict] | None = None)`；`EpisodeAdmissionFact.objects_json: list[dict]`。`output_verification_status == "verified"` 时 `objects` 必须通过 `require_verified_objects`，否则抛 `ValueError("episode_objects_unverified:<code>")`。

- [ ] **Step 1: 写失败测试**，追加到 `backend/tests/test_episode_admission.py`。`make_workspace`、`make_project`、`seed_package_pending_intake_review`、`record_episode_admission_fact`、`pytest` 在该文件中已导入；种子数据已写入第 1 次 attempt，因此新用例从 attempt 2 开始：

```python
from tests.test_episode_objects import verified_entries


def _admission_episode(db_session):
    workspace = make_workspace(db_session)
    project = make_project(db_session, workspace)
    _package, (episode,) = seed_package_pending_intake_review(
        db_session, workspace, project, episode_hours=(1.0,)
    )
    return episode.id, episode.source_fingerprint


def test_verified_fact_stores_validated_objects(db_session):
    episode_id, fingerprint = _admission_episode(db_session)
    fact = record_episode_admission_fact(
        db_session,
        episode_id=episode_id,
        attempt=2,
        source_fingerprint=fingerprint,
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        report_ref={"ok": True},
        objects=verified_entries(),
    )
    db_session.commit()
    assert fact.objects_json == verified_entries()


def test_verified_fact_rejects_incomplete_objects(db_session):
    episode_id, fingerprint = _admission_episode(db_session)
    incomplete = [e for e in verified_entries() if e["kind"] != "metadata"]
    with pytest.raises(
        ValueError, match="episode_objects_unverified:episode_object_missing_metadata"
    ):
        record_episode_admission_fact(
            db_session,
            episode_id=episode_id,
            attempt=2,
            source_fingerprint=fingerprint,
            validation_policy_version="v1",
            integrity_status="passed",
            preview_status="ready",
            output_verification_status="verified",
            report_ref={"ok": True},
            objects=incomplete,
        )
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_episode_admission.py -k "stores_validated_objects or rejects_incomplete_objects" -q`
Expected: FAIL，`TypeError: record_episode_admission_fact() got an unexpected keyword argument 'objects'`

- [ ] **Step 3: 迁移** `backend/alembic/versions/0008_episode_admission_objects.py`：

```python
"""Episode admission facts describe storage with one object manifest."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_episode_admission_objects"
down_revision = "0007_dashboard_package_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "episode_admission_facts",
        sa.Column(
            "objects_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade():
    op.drop_column("episode_admission_facts", "objects_json")
```

核对 `data.database.JsonDocument` 在 PostgreSQL 上映射为 JSONB（打开 `backend/data/database.py` 查 `JsonDocument`）；若映射为 `JSON`，把上面的 `postgresql.JSONB(...)` 换成 `sa.JSON()`，默认值换成 `sa.text("'[]'")`。

- [ ] **Step 4: 模型**，在 `EpisodeAdmissionFact` 的 `source_objects_json` 下一行加：

```python
    objects_json = Column(JsonDocument, nullable=False, default=list)
```

- [ ] **Step 5: 写入函数**。在 `record_episode_admission_fact` 签名的 `source_objects` 参数后加 `objects: list[Any] | None = None,`；在 JSON 规范化之后、加锁之前加入校验：

```python
    from data.services.episode_objects import (
        EpisodeObjectsError,
        parse_objects,
        require_verified_objects,
    )

    try:
        parsed_objects = parse_objects(list(objects or []))
        if output_verification_status == "verified":
            require_verified_objects(parsed_objects)
    except EpisodeObjectsError as exc:
        raise ValueError(f"episode_objects_unverified:{exc.code}") from exc
    normalized_objects = [item.as_entry() for item in parsed_objects]
```

在幂等比较 `same_payload` 中加 `and fact.objects_json == normalized_objects`，在赋值区加 `fact.objects_json = normalized_objects`。

- [ ] **Step 6: 升级测试库并运行**

conftest 会按 alembic 重建测试库，直接运行：

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_episode_admission.py backend/tests/test_baseline_migration.py -q`
Expected: 新增 2 个用例 PASS；失败集合为基线子集。若 `test_baseline_migration.py` 断言了迁移头为 `0007_...`，把期望改为 `0008_episode_admission_objects`。

- [ ] **Step 7: Commit**

```bash
git add backend/alembic/versions/0008_episode_admission_objects.py backend/data/models/episode_admission.py backend/data/services/episode_admission.py backend/tests/test_episode_admission.py backend/tests/test_baseline_migration.py
git commit -m "feat(admission): admission 事实新增 objects_json 对象清单"
```

---

### Task 4: worker 产出对象清单（扩，仍保留 tar）

**Files:**
- Modify: `backend/data/integrations/qrdf/admission.py`（`QRDFAdmissionResult` 增加 `objects`；`admit_qrdf_episode` 上传 process 对象；`run_qrdf_admission_worker` 写 `objects`）
- Test: `backend/tests/test_collection_admission_worker.py`、`backend/tests/test_qrdf_admission.py`

**Interfaces:**
- Consumes: Task 2 `object_entry`；Task 3 `record_episode_admission_fact(objects=...)`
- Produces: `QRDFAdmissionResult.objects: list[dict]`（process 对象条目，不含 raw data）；worker 写入的 `objects_json` = raw `data` 条目 + `result.objects`

- [ ] **Step 1: 写失败测试**，追加到 `backend/tests/test_collection_admission_worker.py`（复用该文件的 `setup_upload`、`claimed_job`、`Provider`）：

```python
def test_worker_records_object_manifest_that_rebuilds_the_episode(
    db_session, tmp_path, monkeypatch
):
    from data.services.episode_objects import (
        data_object,
        fact_objects,
        object_of_kind,
        preview_streams,
        require_verified_objects,
    )

    upload, package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    ep = db_session.query(Episode).filter_by(data_package_id=package.id).one()
    fact = current_episode_admission_fact(db_session, episode_id=ep.id)

    objects = fact_objects(fact)
    require_verified_objects(objects)
    assert data_object(objects).role == "raw"
    assert data_object(objects).path == "data.mcap"
    metadata = object_of_kind(objects, "metadata")
    assert json.loads(provider.uploads[metadata.ref["object_key"]])["episode_id"]
    report = object_of_kind(objects, "admission_report")
    assert json.loads(provider.uploads[report.ref["object_key"]])["source_fingerprint"]
    [stream] = preview_streams(objects)
    assert b"ftyp" in provider.uploads[stream["video"].ref["object_key"]][:32]
    for item in objects:
        if item.role == "process":
            payload = provider.uploads[item.ref["object_key"]]
            assert item.ref["sha256"] == hashlib.sha256(payload).hexdigest()
            assert item.ref["size_bytes"] == len(payload)
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_collection_admission_worker.py::test_worker_records_object_manifest_that_rebuilds_the_episode -q`
Expected: FAIL，`EpisodeObjectsError: episode_object_missing_data`（`objects_json` 为空）。

- [ ] **Step 3: 结果类型**。`QRDFAdmissionResult` 增加字段 `objects: tuple[dict[str, Any], ...] = ()`（放在 `qrdf_profile` 之后），`to_dict` 增加 `"objects": list(self.objects)`。所有 `QRDFAdmissionResult(...)` 位置参数构造处（worker 失败分支）不需要改动，默认空元组。

- [ ] **Step 4: 上传 process 对象**。在 `admission.py` 中新增：

```python
def _publish_process_objects(
    path: Path,
    report_path: Path,
    media_facts: dict[str, Any],
    process_provider: Any,
) -> list[dict[str, Any]]:
    """Upload metadata, report and preview files as individual process objects."""
    from data.services.episode_objects import object_entry

    topics_by_file: dict[str, tuple[str, str]] = {}
    for stream in media_facts.get("streams", []):
        topics_by_file[stream["video"]] = (stream["topic"], "preview_video")
        topics_by_file[stream["timeline"]] = (stream["topic"], "preview_timeline")
    files: list[tuple[Path, str, str, str | None]] = [
        (path / "metadata.json", "metadata.json", "metadata", None),
        (report_path, "admission-report.json", "admission_report", None),
    ]
    preview_root = path / "media" / "preview"
    if media_facts.get("streams"):
        files.append(
            (
                preview_root / "manifest.json",
                "media/preview/manifest.json",
                "preview_manifest",
                None,
            )
        )
        for relative, (topic, kind) in sorted(topics_by_file.items()):
            files.append((preview_root / relative, f"media/preview/{relative}", kind, topic))

    prefix = f"process/v2/qrdf/{uuid4().hex}"
    entries: list[dict[str, Any]] = []
    for local, relative, kind, topic in files:
        sha256 = _sha256_file(local)
        ref = StorageObjectRef(
            "process", f"{prefix}/{relative}", None, "", local.stat().st_size, sha256
        )
        persisted = process_provider.put_worker_object(ref, str(local))
        entries.append(
            object_entry(
                path=relative,
                kind=kind,
                topic=topic,
                ref={**asdict(persisted), "sha256": sha256},
            )
        )
    return entries
```

在 `admit_qrdf_episode` 的 `if admission_ok and process_provider is not None:` 分支中，写完 `report_path` 之后、打 tar 之前调用：

```python
            objects = _publish_process_objects(path, report_path, media_facts, process_provider)
```

并在函数开头与 `process_ref: dict[str, Any] = {}` 同处初始化 `objects: list[dict[str, Any]] = []`；返回 `QRDFAdmissionResult(..., qrdf_profile="qrdf", objects=tuple(objects))`。本任务保留原 tar 与 `preview_refs` 逻辑不动。

- [ ] **Step 5: worker 写入清单**。在 `run_qrdf_admission_worker` 调用 `record_episode_admission_fact` 处追加参数：

```python
objects = (
    (
        [
            object_entry(
                path=verified_source["relative_path"],
                kind="data",
                ref=verified_source,
            ),
            *result.objects,
        ]
        if result.output_verification_status == "verified"
        else []
    ),
)
```

并在该函数的导入块加入 `from data.services.episode_objects import object_entry`。`verified_source` 已含 `bucket_role="raw"`、`object_key`、`version_id`、`etag`、`size_bytes`、`sha256`（下载后计算）与 `relative_path`。

- [ ] **Step 6: 运行测试**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_collection_admission_worker.py backend/tests/test_qrdf_admission.py backend/tests/test_episode_objects.py -q`
Expected: 全部 PASS（`test_partial_process_upload_failure_has_stable_code_and_cleans` 若断言上传次数，按新增的 metadata/report/manifest 上传更新期望数量）。

- [ ] **Step 7: Commit**

```bash
git add backend/data/integrations/qrdf/admission.py backend/tests/test_collection_admission_worker.py backend/tests/test_qrdf_admission.py
git commit -m "feat(admission): worker 逐个上传 process 对象并写入对象清单"
```

---

### Task 5: 预览播放与标注工作台改读对象清单

**Files:**
- Modify: `backend/data/routers/collection_packages.py`（`preview-urls` 端点，读取 `process_ref_json["previews"]` 处）
- Modify: `backend/data/routers/episodes.py:675-725`（`_admission_fact_preview_access`）
- Modify: `backend/data/services/package_annotation_workbench.py:136-150`（`source_binding`）、`:316-320`（`_preview_descriptor`）及其调用方对 `preview["video"]` / `preview["timeline"]` 的读取
- Test: `backend/tests/test_collection_admission_worker.py`、`backend/tests/test_package_annotation_workbench_api.py`

**Interfaces:**
- Consumes: Task 2 `fact_objects`、`preview_streams`、`data_object`、`EpisodeObjectsError`
- Produces: 端点响应不变（`streams[].topic/video_url/timeline_url`、`preview-url` 描述符）

- [ ] **Step 1: 改测试断言**。`test_signed_preview_reads_process_after_scratch_cleanup` 中把

```python
    refs = fact.process_ref_json["previews"]
    assert b"ftyp" in provider.uploads[refs[0]["video"]["object_key"]][:32]
    timeline = json.loads(provider.uploads[refs[0]["timeline"]["object_key"]])
```

改为

```python
    from data.services.episode_objects import fact_objects, preview_streams

    [stream] = preview_streams(fact_objects(fact))
    assert b"ftyp" in provider.uploads[stream["video"].ref["object_key"]][:32]
    timeline = json.loads(provider.uploads[stream["timeline"].ref["object_key"]])
```

并在该用例末尾增加“清单被清空后端点不再签名”的断言：

```python
    fact.process_ref_json = {}
    db_session.commit()
    still_served = client.get(
        f"/api/v1/data-packages/{package.id}/episodes/{ep.id}/preview-urls",
        params={"workspace_id": package.workspace_id},
        headers=admin_headers,
    )
    # 端点只读对象清单：清空旧字段不影响签名；仍读旧字段的实现在这里返回 409
    assert still_served.status_code == 200
```

注意此断言放在 `ep.source_fingerprint = "changed-after-admission"` 之前，并把指纹篡改前的 409 断言顺序保持不变。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_collection_admission_worker.py::test_signed_preview_reads_process_after_scratch_cleanup -q`
Expected: FAIL，`assert 409 == 200`（端点仍读 `process_ref_json`）。

- [ ] **Step 3: `collection_packages.py`** 把 `preview_refs = list((fact.process_ref_json or {}).get("previews") or [])` 及其后的循环替换为：

```python
    from data.services.episode_objects import EpisodeObjectsError, fact_objects, preview_streams

    try:
        stream_objects = preview_streams(fact_objects(fact))
    except EpisodeObjectsError as exc:
        raise HTTPException(status_code=409, detail="verified preview is unavailable") from exc
    if not stream_objects:
        raise HTTPException(status_code=409, detail="verified preview is unavailable")
    db.commit()
    provider = get_storage_provider()
    streams = []
    for stream in stream_objects:
        urls = {"topic": stream["topic"]}
        for kind in ("video", "timeline"):
            ref = stream[kind].storage_ref()
            if ref.bucket_role != "process" or not (ref.version_id or ref.etag):
                raise HTTPException(status_code=409, detail="verified preview is unavailable")
            urls[f"{kind}_url"] = provider.sign_get(ref, expires=300)
        streams.append(urls)
    return success({"streams": streams, "expires_in": 300})
```

（删除原来的 `db.commit()`、`provider = ...` 与循环，避免重复。）

- [ ] **Step 4: `episodes.py`** 在 `_admission_fact_preview_access` 中，把 `preview_refs = ...` 与 `for preview in preview_refs:` 循环替换为：

```python
    from data.services.episode_objects import EpisodeObjectsError, fact_objects, preview_streams

    try:
        streams = preview_streams(fact_objects(fact))
    except EpisodeObjectsError:
        return None
    try:
        provider = get_storage_provider()
    except Exception:
        return None
    for stream in streams:
        ref = stream["video"].storage_ref()
        if ref.bucket_role != "process" or not ref.object_key or not (ref.version_id or ref.etag):
            continue
        try:
            url = provider.sign_get(ref, expires=900)
        except Exception:
            continue
        if not url:
            continue
        return {
            "available": True,
            "direct": True,
            "url": url,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat(),
            "media_type": "video/mp4",
        }
    return None
```

- [ ] **Step 5: `package_annotation_workbench.py`**。`source_binding` 中把 `objects = fact.source_objects_json or []` 及其后的 `mcap = [...]` 判断替换为：

```python
    from data.services.episode_objects import EpisodeObjectsError, data_object, fact_objects

    try:
        source = data_object(fact_objects(fact))
    except EpisodeObjectsError as exc:
        raise AnnotationWorkError("annotation_source_hash_unavailable") from exc
    if not source.path.endswith(".mcap"):
        raise AnnotationWorkError("annotation_source_hash_unavailable")
    mcap = [{**source.ref, "relative_path": source.path}]
```

（后续代码继续使用 `mcap[0]`，不需改动。）`_preview_descriptor` 替换为：

```python
def _preview_descriptor(fact: EpisodeAdmissionFact) -> dict:
    from data.services.episode_objects import EpisodeObjectsError, fact_objects, preview_streams

    try:
        streams = preview_streams(fact_objects(fact))
    except EpisodeObjectsError as exc:
        raise AnnotationWorkError("annotation_preview_unavailable") from exc
    if not streams:
        raise AnnotationWorkError("annotation_preview_unavailable")
    first = streams[0]
    return {"topic": first["topic"], "video": first["video"].ref, "timeline": first["timeline"].ref}
```

返回形状与旧 `previews[0]` 相同，调用方无需修改。

- [ ] **Step 6: 更新工作台测试的造数**。`backend/tests/test_package_annotation_workbench_api.py` 中凡是以 `process_ref={"previews": [...]}`、`source_objects=[...]` 构造 fact 的地方，改为额外传 `objects=`，由 `tests.test_episode_objects.verified_entries()` 生成后按测试所需的 object_key 覆盖：

```python
from tests.test_episode_objects import verified_entries

objects = verified_entries()
# 按该用例原来 process_ref/source_objects 中的 object_key 与 sha256 覆盖对应条目，例如：
objects[0]["ref"].update(object_key=<原 source_objects[0]["object_key"]>, sha256=<原 sha256>)
```

保留原 `process_ref=`、`source_objects=` 参数（Task 8 统一删除）。

- [ ] **Step 7: 运行测试**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_collection_admission_worker.py backend/tests/test_package_annotation_workbench_api.py backend/tests/test_collection_packages_api.py backend/tests/test_episode_workbench.py -q`
Expected: 全部 PASS。

- [ ] **Step 8: Commit**

```bash
git add backend/data/routers/collection_packages.py backend/data/routers/episodes.py backend/data/services/package_annotation_workbench.py backend/tests/test_collection_admission_worker.py backend/tests/test_package_annotation_workbench_api.py
git commit -m "refactor(preview): 预览播放与标注工作台改读对象清单"
```

---

### Task 6: 数据资产快照与导出改用对象清单

**Files:**
- Modify: `backend/data/services/data_assets.py:373-485`（`_source_snapshot`）
- Modify: `backend/data/services/catalog_export_jobs.py:655-690`（`_materialize_snapshot` 中 QRDF 分支）
- Test: `backend/tests/test_catalog_export_jobs.py`、`backend/tests/test_data_assets_api.py`、`backend/tests/test_asset_source_snapshots.py`、`backend/tests/test_catalog_annotation_segment_export.py`

**Interfaces:**
- Consumes: Task 2 `fact_objects`、`require_verified_objects`、`object_of_kind`
- Produces: 快照中每个 episode 为 `{"files": [{"path", "kind", "ref"}...], "governance_report_refs": [{"object_ref": <admission-report ref>, "path": "admission-report.json", "admission_attempt": int}], ...}`，不再有 `process_members`、`process.tar`。`_materialize_snapshot` 把每个 file 下载到 `episode_dest/<path>`。

- [ ] **Step 1: 改写导出测试**。`test_catalog_export_jobs.py::test_frozen_process_archive_materializes_and_passes_real_qrdf_validator` 重命名为 `test_frozen_objects_materialize_and_pass_real_qrdf_validator`。把用例中从 `process = tmp_path / "process.tar"` 到 `provider = _Provider({"process/object": payload})` 的造数替换为“episode 目录下每个文件一个快照条目”（`_Provider` 是该文件中以 `{object_key: bytes}` 构造的 fake provider）：

```python
    episode_root = source / "episodes" / "episode_000001"
    payloads = {}
    files = []
    for local in sorted(p for p in episode_root.rglob("*") if p.is_file()):
        relative = local.relative_to(episode_root).as_posix()
        payload = local.read_bytes()
        key = f"process/v2/qrdf/test/{relative}"
        payloads[key] = payload
        files.append(
            {
                "path": relative,
                "kind": "data" if relative == "data.mcap" else "metadata",
                "ref": {
                    "bucket_role": "process",
                    "object_key": key,
                    "version_id": "v1",
                    "etag": "e1",
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            }
        )
    provider = _Provider(payloads)
```

快照中该 episode 的 `"files": [...]` 改为 `"files": files`，删除 `"process_members": members,`；`annotation_revision` 与其后的 `_materialize_snapshot`、`_validate_materialized` 及断言保持不变。若 `tarfile` 在该文件中已无其他使用，删除其导入。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_catalog_export_jobs.py -k frozen_objects -q`
Expected: FAIL，`CatalogExportError: frozen episode process archive is missing`。

- [ ] **Step 3: 导出按清单物化**。在 `_materialize_snapshot` 中，把从 `process = next(...)` 到 `archive_path.unlink(missing_ok=True)` 的整段替换为：

```python
files = [item for item in episode.get("files", []) if isinstance(item, Mapping)]
if not files:
    raise CatalogExportError("frozen episode objects are missing")
for item in files:
    _download_snapshot_ref(provider, item, episode_dest)
```

`_download_snapshot_ref` 已按 `item["path"]` 写到 `episode_dest/<path>` 并校验身份，后续读取 `episode_dest / "metadata.json"` 的逻辑不变。保留 `_extract_safe_tar`（仍用于导出产物校验）。

- [ ] **Step 4: 快照按清单冻结**。`_source_snapshot` 中把从 `objects = fact.source_objects_json ...` 到 `files.append({"path": "process.tar", "ref": process_ref})` 的整段替换为：

```python
from data.services.episode_objects import (
    EpisodeObjectsError,
    fact_objects,
    object_of_kind,
    require_verified_objects,
)

try:
    episode_objects = fact_objects(fact)
    require_verified_objects(episode_objects)
except EpisodeObjectsError as exc:
    raise DataAssetSnapshotError(exc.code) from exc
files = [{"path": item.path, "kind": item.kind, "ref": dict(item.ref)} for item in episode_objects]
report_object = object_of_kind(episode_objects, "admission_report")
```

在 `episodes.append({...})` 中删除 `"process_members": process_members,`，并把 `governance_report_refs` 改为：

```python
                "governance_report_refs": [
                    {
                        "object_ref": dict(report_object.ref),
                        "path": report_object.path,
                        "admission_attempt": member.admission_attempt,
                    }
                ],
```

删除不再使用的 `_storage_ref` 调用与 `process_members` 变量（若 `_storage_ref` 在文件中已无其他调用，一并删除该函数与 `_SHA256_RE`）。

- [ ] **Step 5: 更新快照相关测试的造数**。`test_data_assets_api.py`、`test_asset_source_snapshots.py`、`test_catalog_annotation_segment_export.py`、`test_governance_runs.py`、`test_governance_annotation_assets_smoke.py`、`test_catalog_datasets_api.py`、`test_plan4_rbac_api.py`、`test_data_backend_e2e.py`、`test_annotation_work_items_api.py` 中凡构造 verified fact 的地方，增加 `objects=verified_entries()`（`from tests.test_episode_objects import verified_entries`），对用例依赖的 object_key 按原 `source_objects`/`process_ref` 覆盖；凡断言快照中 `process.tar`、`process_members` 的地方，改为断言 `files` 含 `data.mcap`、`metadata.json`、`admission-report.json` 三个 path，以及 `governance_report_refs[0]["path"] == "admission-report.json"`。

- [ ] **Step 6: 运行测试**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_catalog_export_jobs.py backend/tests/test_data_assets_api.py backend/tests/test_asset_source_snapshots.py backend/tests/test_catalog_annotation_segment_export.py backend/tests/test_governance_runs.py backend/tests/test_governance_annotation_assets_smoke.py backend/tests/test_catalog_datasets_api.py backend/tests/test_plan4_rbac_api.py backend/tests/test_data_backend_e2e.py backend/tests/test_annotation_work_items_api.py -q`
Expected: 失败集合为基线子集（基线中的 2 个 `test_asset_source_snapshots` 用例仍按原因失败）。

- [ ] **Step 7: Commit**

```bash
git add backend/data/services/data_assets.py backend/data/services/catalog_export_jobs.py backend/tests
git commit -m "refactor(assets): 快照与导出按对象清单冻结和物化，不再使用 process tar"
```

---

### Task 7: 外部拉取清单改用对象清单并带相对路径

**Files:**
- Modify: `backend/data/services/fetch_manifests.py:125-190`
- Test: `backend/tests/test_fetch_manifests_api.py`

**Interfaces:**
- Consumes: Task 2 `fact_objects`
- Produces: 每个返回对象新增 `"path"`（episode 内相对路径）；不再返回 `application/x-tar` 对象。

- [ ] **Step 1: 写失败测试**，追加到 `test_fetch_manifests_api.py`（`_seed` 为该文件已有的造数函数，返回 `workspace, package, episode`）：

```python
def test_fetch_manifest_lists_every_episode_object_with_its_path(
    client, db_session, admin_headers, monkeypatch
):
    from data.services.episode_admission import record_episode_admission_fact
    from tests.test_episode_objects import verified_entries

    workspace, package, episode = _seed(db_session, monkeypatch)
    record_episode_admission_fact(
        db_session,
        episode_id=episode.id,
        attempt=1,
        source_fingerprint=episode.source_fingerprint or "",
        validation_policy_version="v1",
        integrity_status="passed",
        preview_status="ready",
        output_verification_status="verified",
        report_ref={"ok": True},
        objects=verified_entries(),
    )
    db_session.commit()

    response = client.post(
        "/api/v1/fetch-manifests",
        json={"workspace_id": workspace.id, "scope": "data_packages", "ids": [package.id]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    objects = response.json()["data"]["objects"]
    assert sorted(item["path"] for item in objects) == sorted(e["path"] for e in verified_entries())
    assert all(item["mime"] != "application/x-tar" for item in objects)
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_fetch_manifests_api.py -k every_episode_object -q`
Expected: FAIL，`KeyError: 'path'`。

- [ ] **Step 3: 实现**。把

```python
        if frozen and (attempt is None or fact is None or not fact.source_objects_json):
            raise ValueError("frozen_source_identity_missing")
        sources = [
            (_ref(raw), raw.get("mime", "application/octet-stream"))
            for raw in (fact.source_objects_json if fact else [])
        ]
        if fact and fact.process_ref_json and "object_key" in fact.process_ref_json:
            sources.append((_ref(fact.process_ref_json), "application/x-tar"))
```

替换为

```python
        from data.services.episode_objects import EpisodeObjectsError, fact_objects

        try:
            episode_objects = fact_objects(fact) if fact else []
        except EpisodeObjectsError as exc:
            raise ValueError("frozen_source_identity_missing") from exc
        if frozen and (attempt is None or not episode_objects):
            raise ValueError("frozen_source_identity_missing")
        sources = [
            (item.storage_ref(), _MIME_BY_KIND.get(item.kind, "application/octet-stream"), item.path)
            for item in episode_objects
        ]
```

模块级新增：

```python
_MIME_BY_KIND = {
    "data": "application/octet-stream",
    "metadata": "application/json",
    "admission_report": "application/json",
    "preview_manifest": "application/json",
    "preview_timeline": "application/json",
    "preview_video": "video/mp4",
}
```

历史 artifact 兜底分支中 `sources.append((StorageObjectRef(...), meta.get(...)))` 改为三元组，第三项为 `str(meta.get("relative_path") or PurePosixPath(key).name)`（若文件未导入 `PurePosixPath`，从 `pathlib` 导入）。最后的循环改为 `for ref, mime, path in sources:`，并在 `objects.append({...})` 中加 `"path": path,`。

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_fetch_manifests_api.py backend/tests/test_external_tool_e2e.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/data/services/fetch_manifests.py backend/tests/test_fetch_manifests_api.py
git commit -m "feat(fetch): 拉取清单按对象清单列出并带 episode 内相对路径"
```

---

### Task 8: 收口——停止打 tar，删除旧字段

**Files:**
- Create: `backend/alembic/versions/0009_drop_admission_process_ref.py`
- Modify: `backend/data/integrations/qrdf/admission.py`（删除 tar 打包、`preview_refs`、`process_ref`、`process_object_key` 参数）
- Modify: `backend/data/models/episode_admission.py`、`backend/data/services/episode_admission.py`（删除 `process_ref` / `source_objects`）
- Modify: `backend/data/services/collection_upload_parse.py:305-320, 437-456`
- Modify: 所有仍传 `process_ref=` / `source_objects=` 或读 `process_ref_json` / `source_objects_json` 的测试
- Modify: `docs/COLLECTION_UPLOAD_API.md`
- Test: 全量 `backend/tests`

**Interfaces:**
- Consumes: Task 2–7 的全部名字
- Produces: `record_episode_admission_fact(..., objects=...)` 为唯一存储描述；`EpisodeAdmissionFact` 无 `process_ref_json`、`source_objects_json`

- [ ] **Step 1: 写失败测试**，追加到 `backend/tests/test_collection_admission_worker.py`：

```python
def test_worker_does_not_upload_a_process_archive(db_session, tmp_path, monkeypatch):
    upload, _package, provider = setup_upload(db_session, tmp_path, monkeypatch)
    parse_collection_upload_session(db_session, upload.id)
    run_qrdf_admission_worker(db_session, claimed_job(db_session, upload))
    assert provider.uploads
    assert not any(key.endswith(".tar") for key in provider.uploads)
    assert not any(payload[257:262] == b"ustar" for payload in provider.uploads.values())
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests/test_collection_admission_worker.py::test_worker_does_not_upload_a_process_archive -q`
Expected: FAIL（仍上传 `report.tar`）。

- [ ] **Step 3: 删除 tar**。`admit_qrdf_episode` 的 `if admission_ok and process_provider is not None:` 分支只保留：写 `report_path`、调用 `_publish_process_objects`、`finally` 中删除 `report_path`。删除 `key = process_object_key or ...`、`tempfile.mkstemp`、`preview_refs`、`tarfile` 打包与 `files` 计算、`process_ref = {...}`。删除参数 `process_object_key`，删除变量 `process_ref`；`QRDFAdmissionResult` 删除 `process_ref` 字段，`to_dict` 删除对应键，并把 worker 失败分支中位置参数构造改为关键字参数，去掉 `{}` 那一项：

```python
        result = QRDFAdmissionResult(
            ok=False,
            integrity_status="failed",
            preview_status="failed",
            output_verification_status="failed",
            report={"error_code": error_code, "error_type": type(exc).__name__},
            issues=(),
            qrdf_profile="qrdf",
        )
```

若 `tarfile`、`tempfile` 仅剩 `_safe_extract_tar`（历史导入用）使用，保留 `tarfile` 导入，按 ruff 提示删除未使用的导入。

- [ ] **Step 4: worker 与写入函数**。`run_qrdf_admission_worker` 中删除 `process_ref=result.process_ref,`、`source_objects=[verified_source],`；`record_episode_admission_fact` 删除 `process_ref`、`source_objects` 参数及其规范化、比较、赋值。

- [ ] **Step 5: 上传解析的两处写入**。`collection_upload_parse.py` 失败路径删除 `process_ref`/`source_objects`（若有）；fixture 路径（`437-456`）改为：

```python
output_verification_status = ("verified" if preview_ready else "pending",)
qrdf_profile = (str(parsed["modality"]),)
report_ref = (
    {
        "source": "collection_upload_parse_fixture",
        "episode_uid": episode.episode_uid,
    },
)
objects = (_fixture_objects(episode) if preview_ready else [],)
```

并在文件中新增（fixture 模式仅测试环境可用）：

```python
def _fixture_objects(episode: Episode) -> list[dict[str, Any]]:
    """Deterministic manifest standing in for a completed worker in fixture mode."""
    from data.services.episode_objects import object_entry

    digest = hashlib.sha256(episode.source_fingerprint.encode()).hexdigest()
    base = f"fixture/{episode.episode_uid}"

    def ref(role: str, name: str) -> dict[str, Any]:
        return {
            "bucket_role": role,
            "object_key": f"{role}/{base}/{name}",
            "version_id": "fixture",
            "etag": "fixture",
            "size_bytes": 1,
            "sha256": digest,
        }

    return [
        object_entry(path="data.mcap", kind="data", ref=ref("raw", "data.mcap")),
        object_entry(path="metadata.json", kind="metadata", ref=ref("process", "metadata.json")),
        object_entry(
            path="admission-report.json",
            kind="admission_report",
            ref=ref("process", "admission-report.json"),
        ),
    ]
```

- [ ] **Step 6: 迁移** `backend/alembic/versions/0009_drop_admission_process_ref.py`：

```python
"""Admission facts keep only the episode object manifest."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_drop_admission_process_ref"
down_revision = "0008_episode_admission_objects"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("episode_admission_facts", "process_ref_json")
    op.drop_column("episode_admission_facts", "source_objects_json")


def downgrade():
    op.add_column(
        "episode_admission_facts",
        sa.Column(
            "source_objects_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "episode_admission_facts",
        sa.Column(
            "process_ref_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
```

（JSONB/JSON 的选择与 Task 3 一致。）模型中删除 `process_ref_json`、`source_objects_json` 两列；`test_baseline_migration.py` 的迁移头期望改为 `0009_drop_admission_process_ref`。

- [ ] **Step 7: 清理测试中的旧字段**

```bash
grep -rn -E "process_ref|source_objects_json|process_ref_json|process\.tar|process_members" backend/tests backend/data
```

对每个命中：测试中删除 `process_ref=`、`source_objects=` 参数（Task 5/6 已补 `objects=`，未补的在此补上 `objects=verified_entries()`）；断言旧字段的改为断言 `objects_json`。`test_historical_ego_import.py` 中的 `_capture_source_objects` 是同名测试辅助函数，与 fact 无关，不改。上述 grep 在 `backend/data` 中必须无结果。

- [ ] **Step 8: 文档**。`docs/COLLECTION_UPLOAD_API.md` “取消与审核”一节把

> 审核提交要求包事实中的 `integrity.status=passed` 且 `preview.available=true`。

改为

> 入库审核提交只要求每个 episode 的 admission 事实 `integrity_status=passed`（自 `31b0be9` 起）；preview 与输出校验在建批时检查。admission 事实以对象清单 `objects_json` 描述 episode 的全部存储对象（raw `data.mcap`，process 的 `metadata.json`、`admission-report.json` 与 `media/preview/` 下的 preview 文件），不再生成 process tar。

- [ ] **Step 9: 全量测试**

Run: `.venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests -q`
Expected: 失败集合为基线子集。

- [ ] **Step 10: ruff**

Run: `.venv/bin/python -m ruff check backend && .venv/bin/python -m ruff format --check backend`
Expected: 无错误。

- [ ] **Step 11: Commit**

```bash
git add backend docs/COLLECTION_UPLOAD_API.md
git commit -m "refactor(admission): 停止打 process tar，admission 事实只保留对象清单"
```
