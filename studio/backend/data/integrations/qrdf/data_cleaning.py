"""Automated QRDF dataset cleaning (P1: filtering empty packages / invalid frames / noise / duplicates)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qrdf.mcap.codec import encode_message
from qrdf.mcap.reader import McapMessage
from qrdf.reader.episode import Episode
from qrdf.registry.topics import EPISODE_EVENT

from data.integrations.qrdf.mcap_ops import (
    clone_mcap_message,
    collect_messages_by_topic,
    remove_empty_files,
    rewrite_episode_mcap,
)

STATE_TOPIC_PREFIXES = ("/observation/", "/action/")
NOISE_POSITION_JUMP = 5.0


@dataclass
class CleaningStats:
    episodes_processed: int = 0
    removed_empty: int = 0
    removed_invalid: int = 0
    removed_duplicate: int = 0
    removed_noise: int = 0
    removed_empty_files: int = 0
    messages_before: int = 0
    messages_after: int = 0
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "episodes_processed": self.episodes_processed,
            "removed_empty": self.removed_empty,
            "removed_invalid": self.removed_invalid,
            "removed_duplicate": self.removed_duplicate,
            "removed_noise": self.removed_noise,
            "removed_empty_files": self.removed_empty_files,
            "messages_before": self.messages_before,
            "messages_after": self.messages_after,
            "details": self.details,
        }


def _message_fingerprint(msg: McapMessage) -> str:
    payload = encode_message(msg.message)
    raw = f"{msg.topic}|{msg.log_time}|{msg.schema_name}|{payload.hex()}"
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()


def _is_empty_message(msg: McapMessage) -> bool:
    try:
        if not msg.message.HasField("header"):
            return True
        payload = encode_message(msg.message)
        if len(payload) == 0:
            return True
        if msg.topic.endswith("/rgb") or msg.topic.endswith("/depth"):
            if msg.message.ByteSize() <= msg.message.header.ByteSize():
                return True
    except Exception:
        return True
    return False


def _is_invalid_message(msg: McapMessage) -> bool:
    try:
        if not msg.message.HasField("header"):
            return True
        ts = int(msg.message.header.timestamp_ns)
        if ts <= 0:
            return True
        if msg.log_time <= 0:
            return True
    except Exception:
        return True
    return False


def _position_values(msg: McapMessage) -> list[float] | None:
    message = msg.message
    if hasattr(message, "position") and message.position:
        return list(message.position)
    return None


def _is_noise_message(msg: McapMessage, previous: McapMessage | None) -> bool:
    if previous is None:
        return False
    if not any(msg.topic.startswith(p) for p in STATE_TOPIC_PREFIXES):
        return False
    current_pos = _position_values(msg)
    previous_pos = _position_values(previous)
    if not current_pos or not previous_pos or len(current_pos) != len(previous_pos):
        return False
    max_delta = max(abs(a - b) for a, b in zip(current_pos, previous_pos, strict=False))
    return max_delta > NOISE_POSITION_JUMP


def _filter_topic_messages(messages: list[McapMessage], stats: CleaningStats) -> list[McapMessage]:
    kept: list[McapMessage] = []
    seen_fingerprints: set[str] = set()
    previous_kept: McapMessage | None = None

    for msg in sorted(messages, key=lambda m: m.log_time):
        if _is_empty_message(msg):
            stats.removed_empty += 1
            continue
        if _is_invalid_message(msg):
            stats.removed_invalid += 1
            continue
        fingerprint = _message_fingerprint(msg)
        if fingerprint in seen_fingerprints:
            stats.removed_duplicate += 1
            continue
        if _is_noise_message(msg, previous_kept):
            stats.removed_noise += 1
            continue
        seen_fingerprints.add(fingerprint)
        kept.append(clone_mcap_message(msg))
        previous_kept = msg

    return kept


def clean_episode(episode_path: Path) -> CleaningStats:
    stats = CleaningStats(episodes_processed=1)
    episode = Episode(episode_path)
    reader = episode.mcap_reader
    messages_by_topic = collect_messages_by_topic(reader)

    stats.messages_before = sum(len(msgs) for msgs in messages_by_topic.values())
    cleaned_by_topic: dict[str, list[McapMessage]] = {}

    for topic, messages in messages_by_topic.items():
        if topic == EPISODE_EVENT:
            cleaned_by_topic[topic] = [clone_mcap_message(m) for m in messages]
            continue
        cleaned = _filter_topic_messages(messages, stats)
        if cleaned:
            cleaned_by_topic[topic] = cleaned

    stats.messages_after = sum(len(msgs) for msgs in cleaned_by_topic.values())
    if stats.messages_after == 0:
        stats.details.append(f"{episode_path.name}: 清洗后无有效消息")
        return stats

    if stats.messages_after != stats.messages_before:
        counts = rewrite_episode_mcap(episode_path, episode.metadata.data_file, cleaned_by_topic)
        stats.details.append(
            f"{episode_path.name}: {stats.messages_before}→{sum(counts.values())} 条消息"
        )
    return stats


def clean_dataset(dataset_path: Path) -> dict[str, Any]:
    stats = CleaningStats()
    removed_files = remove_empty_files(dataset_path)
    stats.removed_empty_files = len(removed_files)

    episodes_dir = dataset_path / "episodes"
    if not episodes_dir.is_dir():
        return stats.to_dict()

    for ep_path in sorted(episodes_dir.iterdir()):
        if not ep_path.is_dir():
            continue
        try:
            ep_stats = clean_episode(ep_path)
            stats.episodes_processed += ep_stats.episodes_processed
            stats.removed_empty += ep_stats.removed_empty
            stats.removed_invalid += ep_stats.removed_invalid
            stats.removed_duplicate += ep_stats.removed_duplicate
            stats.removed_noise += ep_stats.removed_noise
            stats.messages_before += ep_stats.messages_before
            stats.messages_after += ep_stats.messages_after
            stats.details.extend(ep_stats.details)
        except Exception as exc:
            stats.details.append(f"{ep_path.name}: 清洗跳过 ({exc})")

    return stats.to_dict()
