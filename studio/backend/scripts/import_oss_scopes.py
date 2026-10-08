#!/usr/bin/env python3
"""Import or verify legacy OSS scope JSON before removing deployment fallback."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data.bootstrap  # noqa: F401
from data.database import ExternalOssImportScope, SessionLocal
from data.runtime import assert_schema_current
from data.services.platform_settings import (
    PlatformSettingsError,
    normalize_oss_scope,
    validate_scope_ownership,
)


class OssScopeImportError(RuntimeError):
    pass


def load_scope_payload(*, input_file: str = "") -> list[dict[str, Any]]:
    if input_file:
        raw = Path(input_file).read_text(encoding="utf-8")
    else:
        raw = os.environ.get("OSS_IMPORT_SCOPES_JSON", "")
    if not raw.strip():
        raise OssScopeImportError(
            "OSS scope JSON is required through --input or OSS_IMPORT_SCOPES_JSON"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OssScopeImportError("OSS scope JSON is invalid") from exc
    if not isinstance(payload, list) or not payload:
        raise OssScopeImportError("OSS scope JSON must be a non-empty array")
    if not all(isinstance(item, dict) for item in payload):
        raise OssScopeImportError("every OSS scope must be an object")
    return payload


def import_legacy_oss_scopes(
    db,
    payload: list[dict[str, Any]],
    *,
    apply: bool = False,
    check: bool = False,
) -> dict[str, int]:
    if apply and check:
        raise OssScopeImportError("--apply and --check are mutually exclusive")
    normalized_items: list[dict[str, object]] = []
    seen: set[tuple[int, int, str]] = set()
    try:
        for item in payload:
            normalized = normalize_oss_scope(
                workspace_id=int(item.get("workspace_id") or 0),
                task_set_id=int(item.get("task_set_id") or item.get("project_id") or 0),
                bucket=str(item.get("bucket") or ""),
                prefixes=item.get("prefixes") if isinstance(item.get("prefixes"), list) else [],
            )
            validate_scope_ownership(
                db,
                workspace_id=int(normalized["workspace_id"]),
                task_set_id=int(normalized["task_set_id"]),
            )
            identity = (
                int(normalized["workspace_id"]),
                int(normalized["task_set_id"]),
                str(normalized["bucket"]),
            )
            if identity in seen:
                raise OssScopeImportError("OSS scope JSON contains duplicate targets")
            seen.add(identity)
            normalized_items.append(normalized)
    except (TypeError, ValueError, PlatformSettingsError) as exc:
        raise OssScopeImportError(str(exc)) from exc

    result = {"created": 0, "updated": 0, "unchanged": 0}
    for normalized in normalized_items:
        row = (
            db.query(ExternalOssImportScope)
            .filter(
                ExternalOssImportScope.workspace_id == normalized["workspace_id"],
                ExternalOssImportScope.task_set_id == normalized["task_set_id"],
                ExternalOssImportScope.bucket == normalized["bucket"],
            )
            .with_for_update()
            .one_or_none()
        )
        matches = bool(
            row and row.is_enabled and list(row.prefixes_json or []) == list(normalized["prefixes"])
        )
        if matches:
            result["unchanged"] += 1
            continue
        if check:
            db.rollback()
            raise OssScopeImportError(
                "database OSS scopes are not synchronized with the legacy payload"
            )
        if row is None:
            result["created"] += 1
            if apply:
                db.add(
                    ExternalOssImportScope(
                        workspace_id=normalized["workspace_id"],
                        task_set_id=normalized["task_set_id"],
                        bucket=normalized["bucket"],
                        prefixes_json=normalized["prefixes"],
                        is_enabled=True,
                        revision=1,
                    )
                )
        else:
            result["updated"] += 1
            if apply:
                row.prefixes_json = normalized["prefixes"]
                row.is_enabled = True
                row.revision = int(row.revision) + 1

    if apply:
        db.commit()
    else:
        db.rollback()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import legacy OSS_IMPORT_SCOPES_JSON into PostgreSQL or verify the cutover"
    )
    parser.add_argument("--input", default="", help="path to a legacy JSON array")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="create or update database rows")
    mode.add_argument(
        "--check", action="store_true", help="fail unless database rows already match"
    )
    args = parser.parse_args()

    assert_schema_current()
    payload = load_scope_payload(input_file=args.input)
    db = SessionLocal()
    try:
        result = import_legacy_oss_scopes(db, payload, apply=args.apply, check=args.check)
    finally:
        db.close()
    mode_name = "applied" if args.apply else "verified" if args.check else "preview"
    print(json.dumps({"mode": mode_name, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
