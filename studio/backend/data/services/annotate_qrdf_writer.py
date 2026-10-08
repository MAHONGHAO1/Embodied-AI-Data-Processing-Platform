"""Write PostgreSQL annotations into QRDF package (annotation.json, actions.jsonl, etc.) upon approval."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data.integrations.qrdf.modality import ensure_modality_json, resolve_qrdf_root
from data.services.annotate_schema import clip_descriptions_from_doc
from data.services.behavior_tags_service import BehaviorVocabularySnapshot


def _find_episode_dir(qrdf_root: Path, episode_id: str | None = None) -> Path | None:
    episodes_dir = qrdf_root / "episodes"
    if not episodes_dir.is_dir():
        return None
    if episode_id:
        candidate = episodes_dir / episode_id
        if candidate.is_dir():
            return candidate
    children = sorted(p for p in episodes_dir.iterdir() if p.is_dir())
    return children[0] if children else None


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _episode_summary_to_metadata_annotation(
    summary: dict, clip_count: int, task_language: str
) -> dict:
    success_val = int(summary.get("success", -1))
    quality_val = int(summary.get("quality", 1))
    return {
        "success": success_val == 1,
        "quality": "valid" if quality_val == 1 else "invalid",
        "language": summary.get("language") or task_language,
        "clip_schema": "quicdata.clip_descriptions.v1",
        "clip_count": clip_count,
        "annotation_file": "annotation.json",
    }


def write_annotation_to_qrdf(
    *,
    storage_path: str | None,
    task_id: int,
    annotation_doc: dict,
    vocabulary: BehaviorVocabularySnapshot,
    updated_by: str = "",
) -> dict[str, Any]:
    """Persist annotations to disk in the QRDF directory after approval, returning a write summary."""
    qrdf_root = resolve_qrdf_root(storage_path)
    if not qrdf_root:
        return {"written": False, "reason": "storage_path_not_found"}

    clip_descriptions = clip_descriptions_from_doc(
        annotation_doc,
        action_by_key=vocabulary.action_by_key,
        label_by_key=vocabulary.label_by_key,
        label_to_key=vocabulary.label_to_key,
    )
    segments = clip_descriptions.get("segments") or {}
    episode_id = annotation_doc.get("episode_id") or "episode_000001"
    episode_dir = _find_episode_dir(qrdf_root, episode_id)
    if not episode_dir:
        return {"written": False, "reason": "episode_dir_not_found"}

    fps = float(annotation_doc.get("fps") or 30.0)
    total_frames = int(annotation_doc.get("total_frames") or 0)
    task_language = str(annotation_doc.get("task") or "")
    episode_summary = dict(annotation_doc.get("episode_summary") or {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    annotation_payload = {
        "qrdf_version": "0.1.0",
        "episode_id": episode_dir.name,
        "schema": "quicdata.clip_descriptions.v1",
        "task_id": task_id,
        "updated_by": updated_by,
        "updated_at": now,
        "task": task_language,
        "timeline": {
            "total_frames": total_frames,
            "fps": fps,
            "duration_sec": round(total_frames / fps, 2) if fps > 0 and total_frames > 0 else 0.0,
        },
        "clip_descriptions": clip_descriptions,
        "episode_summary": episode_summary,
    }
    _write_json(episode_dir / "annotation.json", annotation_payload)

    meta_dir = qrdf_root / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    actions_path = meta_dir / "actions.jsonl"
    action_lines = [
        json.dumps(
            {
                "action": tag["action"],
                "label": tag["label"],
                "color": tag["color"],
            },
            ensure_ascii=False,
        )
        for tag in vocabulary.action_options()
    ]
    actions_path.write_text("\n".join(action_lines) + "\n", encoding="utf-8")

    dataset_path = qrdf_root / "dataset.json"
    dataset = _load_json(dataset_path)
    if task_language:
        tasks = list(dataset.get("tasks") or [])
        if task_language not in tasks:
            tasks.append(task_language)
        dataset["tasks"] = tasks
    dataset["annotation"] = {
        "schema": "quicdata.clip_descriptions.v1",
        "actions_file": "meta/actions.jsonl",
        "segment_key_format": "{start_frame}_{end_frame}",
    }
    _write_json(dataset_path, dataset)

    metadata_path = episode_dir / "metadata.json"
    metadata = _load_json(metadata_path)
    task_block = dict(metadata.get("task") or {})
    if task_language:
        task_block["language"] = task_language
        metadata["task"] = task_block
    metadata["annotation"] = _episode_summary_to_metadata_annotation(
        episode_summary, len(segments), task_language
    )
    _write_json(metadata_path, metadata)

    ensure_modality_json(storage_path, fps=fps)

    return {
        "written": True,
        "qrdf_root": str(qrdf_root),
        "annotation_file": str(episode_dir / "annotation.json"),
        "actions_file": str(actions_path),
        "segment_count": len(segments),
        "action_vocab_size": len(vocabulary.tags),
    }
