"""The dataset handoff card that travels with a delivery.

The card is generated from frozen business state and verified evidence; it is
not a second source of truth. ``card_digest`` excludes ``created_at`` so a
rerun with identical content stays verifiable, while a changed field does not.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import APP_VERSION

CARD_SCHEMA_VERSION = 1
SOURCE_FIELDS = ("kind", "repo_id", "revision", "license", "robot", "fps",
                 "cameras", "missing_cameras", "depth_source_cameras",
                 "missing_depth_source_cameras", "optional_fields", "tactile",
                 "state_field", "action_field", "timestamp_semantics")


def _read(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def card_digest(card: dict) -> str:
    body = {key: value for key, value in card.items() if key not in {"card_digest", "created_at"}}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _source_block(source: dict) -> dict:
    return {key: source.get(key) for key in SOURCE_FIELDS}


def _quality_block(conversion_run: Path, bundle: dict) -> dict:
    final = _read(conversion_run / "reports/final_quality.json", {}) or {}
    official = _read(conversion_run / "steps/official_verify.json", {}) or {}
    output_quality = _read(conversion_run / "steps/output_quality.json", {}) or {}
    official_details = official.get("details", {}) if official else {}
    output_details = output_quality.get("details", {}) if output_quality else {}
    return {
        "final_quality": {"present": bool(final), "outcome": final.get("outcome"),
                          "summary": final.get("summary"), "rule_version": final.get("rule_version")},
        "official_verify": {"present": bool(official), "passed": official.get("status") == "passed",
                            "output_loader_consistency_checked": official_details.get("output_loader_consistency_checked"),
                            "sample_count": official_details.get("sample_count"),
                            "row_count": official_details.get("row_count"),
                            "source_numeric_consistency_rechecked": official_details.get("source_numeric_consistency_rechecked")},
        "output_quality": {"present": bool(output_quality), "summary": output_details.get("summary") or output_details},
        "bundle_contract": bundle,
    }


def build_card(batch: dict, output: dict, spec: dict, conversion_run: Path, *,
               dataset_id: str | None = None, version: str | None = None) -> dict:
    dataset = Path(spec["artifact_path"])
    manifest = _read(dataset / "source_manifest.json", {}) or {}
    source = batch.get("source", {})
    episodes = [{"source_episode_index": int(item["source_episode_index"]),
                 "output_episode_index": int(item["output_episode_index"]),
                 "row_count": int(item["row_count"]),
                 "task": item.get("annotation", {}).get("task")}
                for item in spec.get("episodes", [])]
    frames = sum(item["row_count"] for item in episodes)
    tasks = sorted({item["task"] for item in episodes if item.get("task")})
    validation = output.get("validation", {}) or {}
    card = {
        "schema_version": CARD_SCHEMA_VERSION,
        "dataset_id": dataset_id or ("robodata-" + hashlib.sha256(
            f"{batch['batch_id']}:{spec['snapshot_id']}".encode("utf-8")).hexdigest()[:16]),
        "version": str(version or spec.get("content_version") or batch.get("content_version")),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_version": APP_VERSION,
        "label": batch.get("label"),
        "batch_id": batch.get("batch_id"),
        "output_id": output.get("output_id"),
        "content_version": spec.get("content_version") or batch.get("content_version"),
        "format": {"name": "LeRobot", "version": "v3.0"},
        "source": _source_block(source),
        "content": {"episodes": episodes, "episode_count": len(episodes), "frame_count": frames,
                    "tasks": tasks, "cameras": source.get("cameras") or [],
                    "fps": source.get("fps"), "content_version": spec.get("content_version")},
        "quality": _quality_block(Path(conversion_run), {
            "status": "passed",
            "output_loader_consistency_checked": validation.get("official_loader_passed"),
            "source_numeric": validation.get("source_numeric"),
            "source_numeric_scope": validation.get("source_numeric_scope")}),
        "conversion": {key: (manifest.get("conversion_strategy") or {}).get(key) for key in (
            "name", "schema_version", "source_numeric_consistency_rechecked",
            "preview_video_hashes_verified", "preview_data_hashes_verified",
            "video_files_reused", "fallback_reason", "preview_manifest_sha256")},
        "artifacts": {"dataset": "dataset", "source_manifest": "dataset/source_manifest.json",
                      "annotations": "dataset/annotations.json", "conversion": "dataset/CONVERSION.md"},
        "evidence": {"source_manifest_sha256": _sha256(dataset / "source_manifest.json"),
                     "annotations_sha256": _sha256(dataset / "annotations.json"),
                     "official_verify_sha256": _sha256(conversion_run / "steps/official_verify.json"),
                     "final_quality_sha256": _sha256(conversion_run / "reports/final_quality.json"),
                     "quality_report_sha256": _sha256(conversion_run / "reports/quality_report.json"),
                     "zip_manifest": "manifest.json"},
        "limitations": list(manifest.get("limitations") or []),
    }
    card["card_digest"] = card_digest(card)
    return card


def _sha256(path: Path) -> str | None:
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_card(card: dict) -> tuple[bool, str]:
    expected = card.get("card_digest")
    if not expected:
        return False, "缺少 card_digest"
    actual = card_digest(card)
    if actual != expected:
        return False, f"card_digest 不一致：期望 {expected}，实际 {actual}"
    return True, "ok"


def load_card(path: Path) -> dict:
    value = _read(path)
    if not isinstance(value, dict) or value.get("schema_version") != CARD_SCHEMA_VERSION:
        raise ValueError(f"不是 dataset_card v{CARD_SCHEMA_VERSION}：{path}")
    return value
