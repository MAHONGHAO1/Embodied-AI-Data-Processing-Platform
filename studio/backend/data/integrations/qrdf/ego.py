"""QRDF v0.2 EGO inspection and interval materialization helpers."""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from qrdf.analysis import EpisodeAnalysis, analyze_episode
from qrdf.mcap.reader import McapEpisodeReader, McapMessage
from qrdf.mcap.writer import McapEpisodeWriter
from qrdf.models.dataset import DatasetManifest
from qrdf.models.episode import AnnotationInfo, EpisodeMetadata, TaskInfo
from qrdf.registry.topic_layout import resolve_reference_topic
from qrdf.registry.topics import ANNOTATION_EVENT, ANNOTATION_QUALITY, EPISODE_EVENT
from qrdf.schema.qrdf.v0 import common_pb2
from qrdf.validator.validator import QRDFValidator

from data.integrations.qrdf.mcap_ops import IndexedMcapMessageReader, open_indexed_mcap_reader
from data.integrations.qrdf.paths import resolve_qrdf_episode_data_file
from data.utils.storage_paths import resolve_storage_path

LIFECYCLE_EVENT_TYPES = frozenset({"start", "pause", "resume", "stop"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EgoInspection:
    metadata: dict[str, object]
    source_episode_id: str
    capture_mode: str
    episode_type: str
    mount: str
    reference_topic: str
    reference_timestamps_ns: list[int]
    topic_schemas: dict[str, str]
    topic_counts: dict[str, int]
    event_tracks: dict[str, list[dict[str, object]]]
    warnings: list[str]


@dataclass(frozen=True)
class MaterializedEgoDataset:
    dataset_dir: Path
    output_episode_ids: dict[int, str]
    provenance: dict[str, object]


class EgoMaterializationError(ValueError):
    """A reviewed EGO selection could not become a standalone QRDF dataset."""


def inspect_ego_episode(
    episode_dir: Path | str,
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
    analysis: EpisodeAnalysis | None = None,
) -> EgoInspection:
    """Read EGO metadata, reference timestamps and optional event tracks without mutation."""
    if start_ns is not None and end_ns is not None and start_ns >= end_ns:
        raise ValueError("EGO inspection range must be half-open with start_ns < end_ns")
    episode_path = Path(episode_dir)
    metadata = EpisodeMetadata.load(episode_path / "metadata.json")
    reader = analysis or analyze_episode(episode_path, metadata=metadata)
    topics = reader.list_topics()
    reference_topic = resolve_reference_topic(metadata, topics)
    reference_timestamps = (
        [
            message.log_time
            for message in reader.iter_messages(reference_topic)
            if _in_scope(message.log_time, start_ns=start_ns, end_ns=end_ns)
        ]
        if reference_topic
        else []
    )
    warnings: list[str] = []
    event_tracks = _read_event_tracks(reader, start_ns=start_ns, end_ns=end_ns, warnings=warnings)
    return EgoInspection(
        metadata=metadata.model_dump(mode="json", exclude_none=True),
        source_episode_id=metadata.episode_id,
        capture_mode=metadata.capture.mode if metadata.capture else "",
        episode_type=metadata.capture.episode_type if metadata.capture else "",
        mount=_metadata_mount(metadata, reference_topic),
        reference_topic=reference_topic or "",
        reference_timestamps_ns=reference_timestamps,
        topic_schemas={topic: reader.get_schema_name(topic) or "" for topic in topics},
        topic_counts={
            topic: sum(
                1
                for message in reader.iter_messages(topic)
                if _in_scope(message.log_time, start_ns=start_ns, end_ns=end_ns)
            )
            for topic in topics
        },
        event_tracks=event_tracks,
        warnings=warnings,
    )


def stream_ego_event_tracks(episode_dir: Path | str) -> dict[str, list[dict[str, object]]]:
    """Read only EGO event topics in file order without retaining raw payloads."""

    episode_path = Path(episode_dir).resolve()
    metadata = EpisodeMetadata.load(episode_path / "metadata.json")
    data_file = resolve_qrdf_episode_data_file(metadata, episode_path).resolve()
    try:
        data_file.relative_to(episode_path)
    except ValueError as exc:
        raise ValueError("EGO data file must resolve inside the episode directory") from exc
    if not data_file.is_file():
        raise ValueError("EGO raw MCAP is unavailable")

    track_by_topic = {
        EPISODE_EVENT: "episode",
        ANNOTATION_EVENT: "annotation",
        ANNOTATION_QUALITY: "quality",
    }
    tracks: dict[str, list[dict[str, object]]] = {}
    warnings: list[str] = []
    reader = McapEpisodeReader(data_file)
    for item in _iter_selected_messages_streaming(reader, list(track_by_topic)):
        tracks.setdefault(track_by_topic[item.topic], []).append(
            _event_item(item.topic, item, warnings)
        )
    for events in tracks.values():
        events.sort(key=lambda item: int(item["timestamp_ns"]))
    return tracks


def _iter_selected_messages_streaming(
    reader: McapEpisodeReader,
    topics: list[str],
) -> Iterator[McapMessage]:
    """Use one no-cache pass across selected topics with deployed QRDF versions.

    Prefer QRDF's public multi-topic API. Retain compatibility with an older
    deployed SDK during rolling upgrades, without falling back to eager reads.
    """

    multi_topic = getattr(reader, "iter_messages_streaming_for_topics", None)
    if callable(multi_topic):
        yield from multi_topic(topics)
        return
    internal_stream = getattr(reader, "_iter_messages_streaming", None)
    if callable(internal_stream):
        yield from internal_stream(topics=topics)
        return
    for topic in topics:
        yield from reader.iter_messages_streaming(topic)


def _iter_all_messages_streaming(reader: McapEpisodeReader) -> Iterator[McapMessage]:
    """Prefer one public all-topic pass without populating QRDF reader caches."""
    all_topics = getattr(reader, "iter_all_messages_streaming", None)
    if callable(all_topics):
        yield from all_topics()
        return
    internal_stream = getattr(reader, "_iter_messages_streaming", None)
    if callable(internal_stream):
        yield from internal_stream(topics=None)
        return
    # Rolling-upgrade compatibility for SDKs without an all-topic stream.
    yield from _iter_selected_messages_streaming(reader, reader.list_topics())


def write_ego_interval_episode(
    source_episode_dir: Path | str,
    destination_dir: Path | str,
    *,
    output_episode_id: str,
    start_ns: int,
    end_ns: int,
    task_language: str | None,
) -> Path:
    """Materialize a self-contained EGO interval without writing to the raw source.

    Dynamic messages obey ``[start_ns, end_ns)``. Camera calibration is the
    intentional context exception: the newest message at or before ``start_ns``
    is retained, falling back to the first in-range message when necessary.
    """
    if start_ns >= end_ns:
        raise ValueError("EGO interval requires start_ns < end_ns")
    source_dir = Path(source_episode_dir)
    destination = Path(destination_dir)
    if destination.name != output_episode_id:
        raise ValueError("EGO interval destination directory must match output_episode_id")
    if destination.exists():
        raise ValueError("EGO interval destination already exists")

    metadata = EpisodeMetadata.load(source_dir / "metadata.json")
    reader = McapEpisodeReader(resolve_qrdf_episode_data_file(metadata, source_dir))
    calibration_topics = _calibration_topics(metadata)
    with open_indexed_mcap_reader(reader) as indexed_reader:
        if indexed_reader is None:
            logger.warning(
                "mcap_interval_index_fallback",
                extra={
                    "mcap_file": Path(reader.path).name if hasattr(reader, "path") else "unknown"
                },
            )
            selected, scoped_events = _select_interval_messages_streaming(
                reader,
                calibration_topics=calibration_topics,
                start_ns=start_ns,
                end_ns=end_ns,
            )
        else:
            selected, scoped_events = _select_interval_messages_indexed(
                indexed_reader,
                calibration_topics=calibration_topics,
                start_ns=start_ns,
                end_ns=end_ns,
            )

    _prune_optional_unaligned_topics(metadata, selected, calibration_topics)

    canonical_schema_for_topic: dict[str, str] = {}
    for topic, messages in selected.items():
        first = messages[0]
        schema_name = str(getattr(first, "canonical_schema_name", "") or first.schema_name or "")
        if not schema_name:
            raise ValueError(f"EGO interval source topic has no schema: {topic}")
        canonical_schema_for_topic[topic] = schema_name

    destination.mkdir(parents=True, exist_ok=False)
    output_metadata = _build_interval_metadata(
        metadata,
        output_episode_id=output_episode_id,
        start_ns=start_ns,
        end_ns=end_ns,
        task_language=task_language,
        selected_topics=set(selected),
        canonical_schema_for_topic=canonical_schema_for_topic,
    )
    output_metadata.save(destination / "metadata.json")

    with McapEpisodeWriter(destination / output_metadata.data_file) as writer:
        for topic in sorted(selected):
            writer.register_topic(topic, canonical_schema_for_topic[topic])
            for message in selected[topic]:
                writer.write(topic, message.message)

        writer.register_topic(EPISODE_EVENT, "qrdf.v0.EpisodeEvent")
        writer.write(
            EPISODE_EVENT,
            common_pb2.EpisodeEvent(
                header=common_pb2.Header(timestamp_ns=start_ns),
                event_type="start",
            ),
        )
        for event in scoped_events:
            writer.write(EPISODE_EVENT, event.message)
        writer.write(
            EPISODE_EVENT,
            common_pb2.EpisodeEvent(
                header=common_pb2.Header(timestamp_ns=end_ns),
                event_type="stop",
            ),
        )
    return destination


def _select_interval_messages_streaming(
    reader: McapEpisodeReader,
    *,
    calibration_topics: set[str],
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, list[McapMessage]], list[McapMessage]]:
    selected: dict[str, list[McapMessage]] = {}
    calibration_before: dict[str, McapMessage] = {}
    calibration_in_range: dict[str, McapMessage] = {}
    events: list[McapMessage] = []

    for message in _iter_all_messages_streaming(reader):
        topic = message.topic
        if topic == EPISODE_EVENT:
            event_type = getattr(message.message, "event_type", "")
            if event_type not in LIFECYCLE_EVENT_TYPES and _in_scope(
                message.log_time,
                start_ns=start_ns,
                end_ns=end_ns,
            ):
                events.append(message)
            continue
        if topic in calibration_topics:
            if message.log_time <= start_ns:
                previous = calibration_before.get(topic)
                if previous is None or message.log_time >= previous.log_time:
                    calibration_before[topic] = message
            elif message.log_time < end_ns:
                current = calibration_in_range.get(topic)
                if current is None or message.log_time < current.log_time:
                    calibration_in_range[topic] = message
            continue
        if _in_scope(message.log_time, start_ns=start_ns, end_ns=end_ns):
            selected.setdefault(topic, []).append(message)

    for topic in calibration_topics:
        context = calibration_before.get(topic) or calibration_in_range.get(topic)
        if context is None:
            continue
        selected[topic] = [context]
        if context.log_time < start_ns:
            # Preserve the source-timestamped context for provenance and add a
            # derived interval anchor for QRDF's alignment validation.
            selected[topic].append(_calibration_anchor(context, start_ns))

    for messages in selected.values():
        messages.sort(key=lambda item: item.log_time)
    events.sort(key=lambda item: item.log_time)
    return selected, events


def _select_interval_messages_indexed(
    reader: IndexedMcapMessageReader,
    *,
    calibration_topics: set[str],
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, list[McapMessage]], list[McapMessage]]:
    """Select one interval while decompressing only index-matching chunks."""

    calibration_context: dict[str, McapMessage] = {}
    if calibration_topics:
        for message in reader.iter_messages(
            topics=calibration_topics,
            start_ns=None,
            end_ns=start_ns + 1,
            reverse=True,
        ):
            calibration_context.setdefault(message.topic, message)
            if len(calibration_context) == len(calibration_topics):
                break

    selected: dict[str, list[McapMessage]] = {}
    calibration_in_range: dict[str, McapMessage] = {}
    events: list[McapMessage] = []
    for message in reader.iter_messages(
        topics=None,
        start_ns=start_ns,
        end_ns=end_ns,
    ):
        topic = message.topic
        if topic == EPISODE_EVENT:
            if getattr(message.message, "event_type", "") not in LIFECYCLE_EVENT_TYPES:
                events.append(message)
            continue
        if topic in calibration_topics:
            calibration_in_range.setdefault(topic, message)
            continue
        selected.setdefault(topic, []).append(message)

    for topic in calibration_topics:
        context = calibration_context.get(topic) or calibration_in_range.get(topic)
        if context is None:
            continue
        selected[topic] = [context]
        if context.log_time < start_ns:
            selected[topic].append(_calibration_anchor(context, start_ns))

    for messages in selected.values():
        messages.sort(key=lambda item: item.log_time)
    events.sort(key=lambda item: item.log_time)
    return selected, events


def materialize_ego_selection(
    stage_root: Path | str,
    selected: list[Any],
    *,
    annotations_by_episode: dict[int, object] | None = None,
) -> MaterializedEgoDataset:
    """Build an independent QRDF dataset from accepted EGO assets.

    Source assets retain byte-identical MCAP payloads. Derived assets delegate
    slicing to ``write_ego_interval_episode`` so quality profiling and official
    publication share exactly the same half-open interval semantics.
    """
    if not selected:
        raise EgoMaterializationError("at least one accepted EGO asset is required")
    root = Path(stage_root)
    if root.exists():
        raise EgoMaterializationError("EGO dataset staging path already exists")
    root.mkdir(parents=True, exist_ok=False)
    episodes_dir = root / "episodes"
    episodes_dir.mkdir()
    annotation_lookup = annotations_by_episode or {}
    output_episode_ids: dict[int, str] = {}
    for ordinal, ego_episode in enumerate(selected, start=1):
        _require_accepted_asset(ego_episode)
        output_id = f"episode_{ordinal:06d}"
        output_episode_ids[int(ego_episode.id)] = output_id
        write_materialized_episode(
            ego_episode,
            episodes_dir / output_id,
            output_id,
            annotation_record=annotation_lookup.get(int(ego_episode.id)),
        )
    write_dataset_manifest(root, selected, output_episode_ids)
    write_ego_provenance(root, selected, output_episode_ids, annotation_lookup)
    report = QRDFValidator().validate_dataset(root)
    if report.error_count:
        raise EgoMaterializationError("materialized EGO dataset failed QRDF validation")
    return MaterializedEgoDataset(root, output_episode_ids, read_ego_provenance(root))


def write_materialized_episode(
    ego_episode: Any,
    destination_dir: Path | str,
    output_episode_id: str,
    *,
    annotation_record: object | None = None,
) -> Path:
    """Write one accepted source or derived asset into a new output episode."""
    _require_accepted_asset(ego_episode)
    raw_dir = _materialization_source_dir(ego_episode)
    destination = Path(destination_dir)
    if destination.exists():
        raise EgoMaterializationError("EGO output episode already exists")
    task_language = reviewed_task_language(ego_episode)
    kind = str(getattr(ego_episode, "kind", ""))
    if kind == "source":
        metadata = EpisodeMetadata.load(raw_dir / "metadata.json")
        source_mcap = resolve_qrdf_episode_data_file(metadata, raw_dir)
        if not source_mcap.is_file():
            raise EgoMaterializationError("EGO raw MCAP is unavailable")
        destination.mkdir(parents=True, exist_ok=False)
        destination_mcap = resolve_qrdf_episode_data_file(metadata, destination)
        destination_mcap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_mcap, destination_mcap)
        _apply_publication_metadata(metadata, output_episode_id, task_language)
        metadata.save(destination / "metadata.json")
    elif kind == "derived":
        start_ns = getattr(ego_episode, "source_start_ns", None)
        end_ns = getattr(ego_episode, "source_end_ns", None)
        if start_ns is None or end_ns is None:
            raise EgoMaterializationError("derived EGO asset is missing its source interval")
        write_ego_interval_episode(
            raw_dir,
            destination,
            output_episode_id=output_episode_id,
            start_ns=int(start_ns),
            end_ns=int(end_ns),
            task_language=task_language or None,
        )
        metadata = EpisodeMetadata.load(destination / "metadata.json")
        _apply_publication_metadata(metadata, output_episode_id, task_language)
        metadata.save(destination / "metadata.json")
    else:
        raise EgoMaterializationError("unsupported EGO asset kind")

    _write_annotation_sidecar(destination, annotation_record, task_language)
    return destination


def _materialization_source_dir(ego_episode: Any) -> Path:
    """Resolve raw data normally, or the verified recovery sidecar when flagged.

    The raw URI stays immutable evidence and is verified by publication. A
    source marked as recovered must never silently fall back to that known-bad
    MCAP while writing the final QRDF package.
    """
    metadata = getattr(ego_episode, "metadata_json", {})
    integrity = metadata.get("source_integrity") if isinstance(metadata, dict) else None
    recovery_status = (
        str(integrity.get("recovery_status") or "") if isinstance(integrity, dict) else ""
    )
    if recovery_status == "recovered":
        uri = str(getattr(ego_episode, "process_uri", "") or "")
        if not uri:
            raise EgoMaterializationError("recovered EGO source sidecar is unavailable")
    else:
        uri = str(getattr(ego_episode, "raw_uri", "") or "")
    source_dir = resolve_storage_path(uri)
    if source_dir is None or not source_dir.is_dir():
        raise EgoMaterializationError("EGO publication source is unavailable")
    return source_dir


def write_dataset_manifest(
    dataset_dir: Path | str,
    selected: list[Any],
    output_episode_ids: dict[int, str],
) -> Path:
    root = Path(dataset_dir)
    manifest = DatasetManifest.create_default(root, dataset_name=root.name)
    manifest.domains = ["ego"]
    manifest.tasks = sorted(
        {language for episode in selected if (language := reviewed_task_language(episode))}
    )
    for episode in selected:
        manifest.add_episode(output_episode_ids[int(episode.id)], split="train")
    manifest_path = root / "dataset.json"
    manifest.save(manifest_path)
    return manifest_path


def write_ego_provenance(
    dataset_dir: Path | str,
    selected: list[Any],
    output_episode_ids: dict[int, str],
    annotations_by_episode: dict[int, object] | None = None,
) -> Path:
    root = Path(dataset_dir)
    meta_dir = root / "meta"
    meta_dir.mkdir(exist_ok=True)
    annotation_lookup = annotations_by_episode or {}
    outputs: list[dict[str, object]] = []
    for episode in selected:
        annotation = _annotation_document(
            annotation_lookup.get(int(episode.id)), reviewed_task_language(episode)
        )
        kind = str(getattr(episode, "kind", ""))
        outputs.append(
            {
                "ego_episode_id": int(episode.id),
                "asset_id": str(getattr(episode, "asset_id", "")),
                "output_episode_id": output_episode_ids[int(episode.id)],
                "kind": kind,
                "parent_id": _as_optional_int(getattr(episode, "parent_id", None)),
                "source_fingerprint": str(getattr(episode, "source_fingerprint", "")),
                "source_start_ns": _as_optional_ns(getattr(episode, "source_start_ns", None)),
                "source_end_ns": _as_optional_ns(getattr(episode, "source_end_ns", None)),
                "boundary_source": str(getattr(episode, "boundary_source", "")),
                "source_lineage": _published_source_lineage(episode),
                "annotation_version": annotation["version"],
                "interval_message_policy": (
                    {
                        "dynamic": "[start,end)",
                        "calibration": "latest_at_or_before_start_with_alignment_anchor",
                        "lifecycle": "synthesized",
                    }
                    if kind == "derived"
                    else {
                        "dynamic": "source_whole",
                        "calibration": "source_whole",
                        "lifecycle": "source_whole",
                    }
                ),
            }
        )
    payload = {
        "schema": "quicdata.ego_provenance.v1",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "outputs": outputs,
    }
    path = meta_dir / "quicdata_ego_provenance.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _published_source_lineage(ego_episode: Any) -> dict[str, object]:
    """Carry the child's frozen, storage-safe source lineage into QRDF output."""
    provenance = getattr(ego_episode, "provenance_snapshot_json", {})
    lineage = provenance.get("source_lineage") if isinstance(provenance, dict) else None
    if isinstance(lineage, dict):
        recovery = lineage.get("recovery")
        return {
            "source_ego_episode_id": _as_optional_int(lineage.get("source_ego_episode_id")),
            "ego_source_id": _bounded_lineage_text(lineage.get("ego_source_id")),
            "parent_ego_source_id": _bounded_lineage_text(lineage.get("parent_ego_source_id")),
            "recovery": {
                "status": (
                    str(recovery.get("status") or "unknown")
                    if isinstance(recovery, dict)
                    and str(recovery.get("status") or "") in {"not_needed", "recovered", "unknown"}
                    else "unknown"
                ),
                "data_loss_possible": bool(
                    recovery.get("data_loss_possible") if isinstance(recovery, dict) else False
                ),
            },
        }
    metadata = getattr(ego_episode, "metadata_json", {})
    integrity = metadata.get("source_integrity") if isinstance(metadata, dict) else None
    status = str(integrity.get("recovery_status") or "") if isinstance(integrity, dict) else ""
    source_id = getattr(ego_episode, "parent_ego_source_id", None) or getattr(
        ego_episode, "ego_source_id", None
    )
    return {
        "source_ego_episode_id": _as_optional_int(getattr(ego_episode, "parent_id", None)),
        "ego_source_id": _bounded_lineage_text(source_id),
        "parent_ego_source_id": _bounded_lineage_text(source_id),
        "recovery": {
            "status": status if status in {"not_needed", "recovered"} else "unknown",
            "data_loss_possible": bool(
                integrity.get("data_loss_possible") if isinstance(integrity, dict) else False
            ),
        },
    }


def _bounded_lineage_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.replace("\x00", " ").split())[:128]
    return normalized or None


def read_ego_provenance(dataset_dir: Path | str) -> dict[str, object]:
    path = Path(dataset_dir) / "meta" / "quicdata_ego_provenance.json"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise EgoMaterializationError("EGO provenance sidecar is unavailable") from exc
    if not isinstance(parsed, dict):
        raise EgoMaterializationError("EGO provenance sidecar is invalid")
    return parsed


def reviewed_task_language(ego_episode: Any) -> str:
    """Return only a reviewed, non-generic platform task language."""
    language = str(getattr(ego_episode, "task_language", "") or "").strip()
    source = str(getattr(ego_episode, "task_language_source", "") or "")
    if source not in {"human_annotation", "source_metadata"}:
        return ""
    if language.casefold() in {"iphone ego capture", "synthetic ego capture"}:
        return ""
    return language


def _require_accepted_asset(ego_episode: Any) -> None:
    if str(getattr(ego_episode, "review_status", "")) != "accepted":
        raise EgoMaterializationError("only accepted EGO assets can be materialized")
    if not getattr(ego_episode, "id", None):
        raise EgoMaterializationError("EGO asset id is required for materialization")


def _apply_publication_metadata(
    metadata: EpisodeMetadata,
    output_episode_id: str,
    task_language: str,
) -> None:
    metadata.episode_id = output_episode_id
    if metadata.task is None and task_language:
        metadata.task = TaskInfo(name="ego_episode", language=task_language)
    elif metadata.task is not None:
        metadata.task.language = task_language or None
    metadata.annotation = AnnotationInfo(language=task_language or None)


def _write_annotation_sidecar(
    episode_dir: Path,
    annotation_record: object | None,
    task_language: str,
) -> None:
    payload = _annotation_document(annotation_record, task_language)
    (episode_dir / "annotation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _annotation_document(annotation_record: object | None, task_language: str) -> dict[str, object]:
    if annotation_record is None:
        data: dict[str, Any] = {}
        version = 0
    elif isinstance(annotation_record, dict):
        data = dict(annotation_record.get("data_json") or annotation_record)
        version = int(annotation_record.get("version") or 0)
    else:
        data = dict(getattr(annotation_record, "data_json", {}) or {})
        version = int(getattr(annotation_record, "version", 0) or 0)
    return {
        "schema": "quicdata.ego_annotation.v1",
        "version": version,
        "task_language": str(data.get("task_language") or task_language or ""),
        "segments": _project_v1_annotation_segments(data.get("segments") or []),
        **{
            key: _serialize_absolute_ns(value)
            for key, value in data.items()
            if key in {"source_annotation_version", "source_episode_id", "source_ego_episode_id"}
        },
    }


def _project_v1_annotation_segments(segments: object) -> list[object]:
    serialized = _serialize_absolute_ns(segments)
    if not isinstance(serialized, list):
        raise EgoMaterializationError("EGO annotation segments must be a list")
    for segment in serialized:
        if not isinstance(segment, dict):
            continue
        action = segment.get("action")
        if not isinstance(action, dict):
            continue
        kind = action.get("kind")
        if kind == "standard" and isinstance(action.get("key"), str):
            segment["action"] = action["key"]
        elif kind == "custom" and isinstance(action.get("text"), str):
            segment["action"] = action["text"]
        else:
            raise EgoMaterializationError("EGO annotation action cannot be projected to v1")
    return serialized


def _serialize_absolute_ns(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(name): _serialize_absolute_ns(item, key=str(name)) for name, item in value.items()
        }
    if isinstance(value, list):
        return [_serialize_absolute_ns(item, key=key) for item in value]
    if key.endswith("_ns") and value is not None:
        return str(int(value))
    return value


def _as_optional_ns(value: Any) -> str | None:
    return str(int(value)) if value is not None else None


def _as_optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _in_scope(timestamp_ns: int, *, start_ns: int | None, end_ns: int | None) -> bool:
    return (start_ns is None or timestamp_ns >= start_ns) and (
        end_ns is None or timestamp_ns < end_ns
    )


def _read_event_tracks(
    reader: McapEpisodeReader,
    *,
    start_ns: int | None,
    end_ns: int | None,
    warnings: list[str],
) -> dict[str, list[dict[str, object]]]:
    tracks: dict[str, list[dict[str, object]]] = {}
    topic_map = {
        EPISODE_EVENT: "episode",
        ANNOTATION_EVENT: "annotation",
        ANNOTATION_QUALITY: "quality",
    }
    available = set(reader.list_topics())
    for topic, name in topic_map.items():
        if topic not in available:
            continue
        events: list[dict[str, object]] = []
        for item in reader.iter_messages(topic):
            if not _in_scope(item.log_time, start_ns=start_ns, end_ns=end_ns):
                continue
            events.append(_event_item(topic, item, warnings))
        if events:
            tracks[name] = events
    return tracks


def _event_item(topic: str, item: McapMessage, warnings: list[str]) -> dict[str, object]:
    message = item.message
    event: dict[str, object] = {
        "timestamp_ns": item.log_time,
        "event_type": getattr(message, "event_type", ""),
    }
    if topic == EPISODE_EVENT and event["event_type"] == "segment":
        raw_message = getattr(message, "message", "")
        try:
            parsed = json.loads(raw_message)
            if not isinstance(parsed, dict):
                raise ValueError("segment payload is not an object")
            event["parsed"] = True
            for key in ("v", "collector_id", "segment_id", "segment_index"):
                if key in parsed:
                    event[key] = parsed[key]
        except (TypeError, ValueError, json.JSONDecodeError):
            event["parsed"] = False
            event["raw_message"] = raw_message
            warnings.append("unparseable_segment_event")
    else:
        for key in ("label", "source", "message"):
            value = getattr(message, key, None)
            if value:
                event[key] = value
    return event


def _metadata_mount(metadata: EpisodeMetadata, reference_topic: str | None) -> str:
    for device in metadata.devices:
        if device.mount:
            return device.mount
    if reference_topic and reference_topic.startswith("/camera/"):
        parts = reference_topic.split("/")
        if len(parts) >= 3:
            return parts[2]
    return ""


def _calibration_topics(metadata: EpisodeMetadata) -> set[str]:
    topics: set[str] = set()
    for device in metadata.devices:
        for stream in device.streams:
            topics.add(stream.camera_info_topic)
            if stream.depth_camera_info_topic:
                topics.add(stream.depth_camera_info_topic)
    return topics


def _calibration_anchor(context: McapMessage, timestamp_ns: int) -> McapMessage:
    anchored_message = type(context.message)()
    anchored_message.CopyFrom(context.message)
    anchored_message.header.timestamp_ns = timestamp_ns
    return McapMessage(
        topic=context.topic,
        schema_name=context.schema_name,
        canonical_schema_name=context.canonical_schema_name,
        descriptor_format=context.descriptor_format,
        is_legacy_schema=context.is_legacy_schema,
        log_time=timestamp_ns,
        publish_time=timestamp_ns,
        message=anchored_message,
    )


def _prune_optional_unaligned_topics(
    metadata: EpisodeMetadata,
    selected: dict[str, list[McapMessage]],
    calibration_topics: set[str],
) -> None:
    """Keep a derived interval independently validator-ready.

    QRDF v0.2's training readiness check aligns every retained MCAP topic with
    the RGB timeline. A sparse optional stream (for example a single GPS fix
    late in a cut) can otherwise invalidate every frame. Raw remains complete;
    the derived metadata is reduced to the streams usable by the interval.
    """
    reference_topic = resolve_reference_topic(metadata, selected)
    reference_messages = selected.get(reference_topic or "") or []
    if not reference_messages:
        return
    reference_start_ns = reference_messages[0].log_time
    required_dynamic = _required_dynamic_topics(metadata)
    for topic, messages in list(selected.items()):
        if topic in calibration_topics or topic in required_dynamic or topic == EPISODE_EVENT:
            continue
        if not any(
            abs(message.log_time - reference_start_ns) <= 100_000_000 for message in messages
        ):
            del selected[topic]


def _required_dynamic_topics(metadata: EpisodeMetadata) -> set[str]:
    required = {entry.name for entry in metadata.topics if entry.required}
    for device in metadata.devices:
        for stream in device.streams:
            required.add(stream.rgb_topic)
            if stream.pose_topic:
                required.add(stream.pose_topic)
    return required


def _build_interval_metadata(
    metadata: EpisodeMetadata,
    *,
    output_episode_id: str,
    start_ns: int,
    end_ns: int,
    task_language: str | None,
    selected_topics: set[str],
    canonical_schema_for_topic: dict[str, str],
) -> EpisodeMetadata:
    metadata.episode_id = output_episode_id
    metadata.timing.start_timestamp_ns = start_ns
    metadata.timing.end_timestamp_ns = end_ns
    metadata.timing.duration_s = (end_ns - start_ns) / 1_000_000_000
    if task_language:
        if metadata.task is None:
            metadata.task = TaskInfo(name="ego_episode", language=task_language)
        else:
            metadata.task.language = task_language
    elif metadata.task is not None:
        metadata.task.language = None

    metadata.topics = [entry for entry in metadata.topics if entry.name in selected_topics]
    for entry in metadata.topics:
        entry.schema_name = canonical_schema_for_topic.get(entry.name, entry.schema_name)
    metadata.sensors.cameras = [
        camera for camera in metadata.sensors.cameras if camera.topic in selected_topics
    ]
    metadata.sensors.lowdim = [
        lowdim for lowdim in metadata.sensors.lowdim if lowdim.topic in selected_topics
    ]
    retained_devices = []
    for device in metadata.devices:
        retained_streams = []
        for stream in device.streams:
            if stream.rgb_topic not in selected_topics:
                continue
            for field in (
                "depth_topic",
                "depth_camera_info_topic",
                "pose_topic",
                "imu_topic",
                "magnetic_field_topic",
                "geofix_topic",
            ):
                topic = getattr(stream, field)
                if topic and topic not in selected_topics:
                    setattr(stream, field, None)
            retained_streams.append(stream)
        if retained_streams:
            device.streams = retained_streams
            retained_devices.append(device)
    metadata.devices = retained_devices
    return metadata
