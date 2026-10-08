"""QRDF SDK-driven preprocessing pipeline (without modifying vendor/qrdf)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from qrdf.validator.checks.file import check_dataset_files, check_episode_files
from qrdf.validator.checks.metadata import check_episode_metadata
from qrdf.validator.checks.schema import check_episode_schemas

from data.integrations.qrdf.data_cleaning import clean_dataset
from data.integrations.qrdf.service import (
    ensure_processable_dataset_path,
    list_episodes,
    list_topic_stats,
    validate_dataset,
)
from data.integrations.qrdf.temporal_align import standardize_dataset_timing


def _emit_step(
    steps: list[dict[str, Any]],
    on_step: Callable[[dict[str, Any]], None] | None,
    *,
    name: str,
    progress: int,
    message: str,
    ok: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    step = {"name": name, "progress": progress, "message": message, "ok": ok}
    if extra:
        step.update(extra)
    steps.append(step)
    if on_step:
        on_step(step)


def run_preprocess_pipeline(
    storage_path: str | None,
    *,
    source_uri: str | None = None,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Execute 4-step preprocessing: cleaning -> temporal alignment -> normalization check -> Validator QA."""
    dataset_path = ensure_processable_dataset_path(storage_path, source_uri=source_uri)
    steps: list[dict[str, Any]] = []
    anomalies: list[str] = []

    if not dataset_path:
        msg = "未找到 QRDF 数据集，跳过 SDK 校验"
        for name, progress in (
            ("cleaning", 20),
            ("aligning", 50),
            ("normalizing", 75),
            ("validating", 90),
        ):
            _emit_step(steps, on_step, name=name, progress=progress, message=msg, ok=False)
        quality = validate_dataset(storage_path)
        return {
            "steps": steps,
            "quality": quality,
            "episodes": [],
            "topics": [],
            "anomalies": [msg],
            "dataset_path": None,
            "cleaning": {},
            "aligning_stats": {},
        }

    if cancelled and cancelled():
        return {"steps": steps, "cancelled": True}

    # 1. Automated cleaning (P1)
    cleaning_stats = clean_dataset(dataset_path)
    removed_total = (
        cleaning_stats.get("removed_empty", 0)
        + cleaning_stats.get("removed_invalid", 0)
        + cleaning_stats.get("removed_duplicate", 0)
        + cleaning_stats.get("removed_noise", 0)
        + cleaning_stats.get("removed_empty_files", 0)
    )
    clean_ok = (
        cleaning_stats.get("messages_after", 0) > 0
        or cleaning_stats.get("episodes_processed", 0) == 0
    )
    clean_msg = (
        f"清洗完成：剔除 {removed_total} 项 "
        f"(空包={cleaning_stats.get('removed_empty', 0)}, "
        f"无效帧={cleaning_stats.get('removed_invalid', 0)}, "
        f"重复={cleaning_stats.get('removed_duplicate', 0)}, "
        f"噪声={cleaning_stats.get('removed_noise', 0)})"
    )
    _emit_step(
        steps,
        on_step,
        name="cleaning",
        progress=20,
        message=clean_msg,
        ok=clean_ok,
        extra={"stats": cleaning_stats},
    )
    if not clean_ok:
        anomalies.append(clean_msg)

    if cancelled and cancelled():
        return {"steps": steps, "cancelled": True, "cleaning": cleaning_stats}

    # 2. Temporal standardization (fixes ordering only, preserves native sampling rate)
    aligning_stats = standardize_dataset_timing(dataset_path)
    align_ok = (
        aligning_stats.get("valid_episodes", 0) > 0
        or aligning_stats.get("episodes_processed", 0) == 0
    )
    align_msg = aligning_stats.get("message", "时序对齐完成")
    _emit_step(
        steps,
        on_step,
        name="aligning",
        progress=50,
        message=align_msg,
        ok=align_ok,
        extra={"stats": aligning_stats},
    )
    if not align_ok and aligning_stats.get("episodes_processed", 0) > 0:
        anomalies.append(align_msg)

    if cancelled and cancelled():
        return {
            "steps": steps,
            "cancelled": True,
            "cleaning": cleaning_stats,
            "aligning_stats": aligning_stats,
        }

    # 3. File integrity + metadata / schema check
    file_report = check_dataset_files(dataset_path)
    episodes_dir = dataset_path / "episodes"
    if episodes_dir.exists():
        for ep in sorted(episodes_dir.iterdir()):
            if ep.is_dir():
                file_report.merge(check_episode_files(ep))

    meta_errors = 0
    for ep_path in sorted(episodes_dir.glob("*")) if episodes_dir.exists() else []:
        if not ep_path.is_dir():
            continue
        meta_r = check_episode_metadata(ep_path)
        meta_errors += meta_r.error_count
        if (ep_path / "metadata.json").exists():
            schema_r = check_episode_schemas(ep_path)
            meta_errors += schema_r.error_count

    norm_ok = file_report.error_count == 0 and meta_errors == 0
    norm_msg = (
        f"归一化检查：file_errors={file_report.error_count}, metadata/schema_errors={meta_errors}"
    )
    _emit_step(steps, on_step, name="normalizing", progress=75, message=norm_msg, ok=norm_ok)
    if file_report.error_count:
        anomalies.extend(i.message for i in file_report.issues if i.severity == "ERROR")
    if meta_errors:
        anomalies.append(f"归一化检查发现 {meta_errors} 个错误")

    if cancelled and cancelled():
        return {
            "steps": steps,
            "cancelled": True,
            "cleaning": cleaning_stats,
            "aligning_stats": aligning_stats,
        }

    # 4. Complete Validator
    quality = validate_dataset(storage_path)
    _emit_step(
        steps,
        on_step,
        name="validating",
        progress=90,
        message=f"QRDF Validator: {quality.get('level')} (score={quality.get('score')})",
        ok=quality.get("ok", False),
    )
    if not quality.get("ok"):
        anomalies.extend(quality.get("errors") or [])

    episodes = list_episodes(storage_path)
    topics = list_topic_stats(storage_path)

    return {
        "steps": steps,
        "quality": quality,
        "episodes": episodes,
        "topics": topics,
        "anomalies": anomalies,
        "dataset_path": str(dataset_path),
        "cleaning": cleaning_stats,
        "aligning_stats": aligning_stats,
    }
