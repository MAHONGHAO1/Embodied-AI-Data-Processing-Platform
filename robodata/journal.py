"""Append-only work-unit journal for resumable long stages.

The journal is an accelerator, never a substitute for final verification. A
unit is reused only when its input digest matches and every recorded artifact
still exists with the recorded hash.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

SCHEMA_VERSION = 1


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_digest(path: Path) -> tuple[str, int]:
    entries = []
    total = 0
    for item in sorted(path.rglob("*")):
        if item.is_file():
            size = item.stat().st_size
            total += size
            entries.append(f"{item.relative_to(path).as_posix()}\0{size}\0{file_sha256(item)}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest(), total


def artifact_record(path: Path, *, root: Path | None = None) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_dir():
        digest, size = _tree_digest(path)
    else:
        digest, size = file_sha256(path), path.stat().st_size
    relative = path.resolve().relative_to(Path(root).resolve()).as_posix() if root else str(path.resolve())
    return {"path": relative, "sha256": digest, "size": size, "kind": "directory" if path.is_dir() else "file"}


def artifact_ok(record: dict, *, root: Path | None = None) -> bool:
    try:
        path = Path(record["path"])
        if root is not None and not path.is_absolute():
            path = Path(root) / path
        if not path.exists():
            return False
        current = artifact_record(path, root=root)
    except (OSError, KeyError, ValueError):
        return False
    return (current.get("sha256") == record.get("sha256")
            and int(current.get("size", -1)) == int(record.get("size", -2)))


class UnitJournal:
    def __init__(self, path: Path, *, root: Path | None = None):
        self.path = Path(path)
        self.root = Path(root).resolve() if root else None
        self._records: dict[str, dict] | None = None

    def load(self) -> dict[str, dict]:
        if self._records is not None:
            return self._records
        records: dict[str, dict] = {}
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("unit_id"):
                    records[str(record["unit_id"])] = record
        self._records = records
        return records

    def get(self, unit_id: str) -> dict | None:
        return self.load().get(unit_id)

    def is_done(self, unit_id: str, input_digest: str) -> bool:
        record = self.get(unit_id)
        if not record or record.get("status") != "done" or record.get("input_digest") != input_digest:
            return False
        artifacts = record.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            return False
        return all(artifact_ok(item, root=self.root) for item in artifacts)

    def append(self, unit_id: str, status: str, *, input_digest: str,
               artifacts: list[dict] | None = None, meta: dict | None = None,
               error: str | None = None) -> dict:
        record = {"schema_version": SCHEMA_VERSION, "unit_id": unit_id, "status": status,
                  "input_digest": input_digest, "artifacts": artifacts or [],
                  "meta": meta or {}, "error": error}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)
        if self._records is not None:
            self._records[unit_id] = record
        return record

    def start(self, unit_id: str, input_digest: str) -> dict:
        return self.append(unit_id, "started", input_digest=input_digest)

    def complete(self, unit_id: str, input_digest: str, artifacts: list[dict],
                 meta: dict | None = None) -> dict:
        return self.append(unit_id, "done", input_digest=input_digest,
                           artifacts=artifacts, meta=meta)

    def fail(self, unit_id: str, input_digest: str, error: str) -> dict:
        return self.append(unit_id, "failed", input_digest=input_digest, error=error)

    def summary(self) -> dict:
        records = self.load().values()
        return {"units": len(records),
                "done": sum(record.get("status") == "done" for record in records),
                "failed": sum(record.get("status") == "failed" for record in records),
                "reused": sum(bool(record.get("reused")) for record in records)}
