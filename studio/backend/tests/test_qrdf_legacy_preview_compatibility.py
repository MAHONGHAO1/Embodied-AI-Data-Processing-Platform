"""Compatibility contracts for historical QRDF ``preview_file`` metadata."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def _write_metadata(episode_dir: Path, payload: dict[str, object]) -> None:
    (episode_dir / "metadata.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_legacy_preview_path_uses_safe_relative_metadata_value(tmp_path: Path) -> None:
    from data.integrations.qrdf.paths import resolve_legacy_qrdf_preview_file

    episode_dir = tmp_path / "episode_000001"
    episode_dir.mkdir()
    _write_metadata(episode_dir, {"preview_file": "legacy/preview.mp4"})

    assert resolve_legacy_qrdf_preview_file(episode_dir) == episode_dir / "legacy" / "preview.mp4"


def test_legacy_preview_path_defaults_when_new_metadata_has_no_legacy_field(tmp_path: Path) -> None:
    from data.integrations.qrdf.paths import resolve_legacy_qrdf_preview_file

    episode_dir = tmp_path / "episode_000001"
    episode_dir.mkdir()
    _write_metadata(episode_dir, {"episode_id": "episode_000001"})

    assert resolve_legacy_qrdf_preview_file(episode_dir) == episode_dir / "preview.mp4"


def test_legacy_preview_path_rejects_escape_and_falls_back_safely(tmp_path: Path, caplog) -> None:
    from data.integrations.qrdf.paths import resolve_legacy_qrdf_preview_file

    episode_dir = tmp_path / "episode_000001"
    episode_dir.mkdir()
    _write_metadata(episode_dir, {"preview_file": "../outside.mp4"})

    assert resolve_legacy_qrdf_preview_file(episode_dir) == episode_dir / "preview.mp4"
    assert "unsafe legacy QRDF preview_file" in caplog.text


def test_legacy_preview_path_rejects_symlink_escape_and_falls_back_safely(tmp_path: Path) -> None:
    from data.integrations.qrdf.paths import resolve_legacy_qrdf_preview_file

    episode_dir = tmp_path / "episode_000001"
    episode_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (episode_dir / "legacy").symlink_to(outside, target_is_directory=True)
    _write_metadata(episode_dir, {"preview_file": "legacy/preview.mp4"})

    assert resolve_legacy_qrdf_preview_file(episode_dir) == episode_dir / "preview.mp4"


def test_legacy_preview_path_does_not_follow_metadata_symlink(tmp_path: Path, caplog) -> None:
    from data.integrations.qrdf.paths import resolve_legacy_qrdf_preview_file

    episode_dir = tmp_path / "episode_000001"
    episode_dir.mkdir()
    external_metadata = tmp_path / "external-metadata.json"
    external_metadata.write_text(json.dumps({"preview_file": "external.mp4"}), encoding="utf-8")
    (episode_dir / "metadata.json").symlink_to(external_metadata)

    assert resolve_legacy_qrdf_preview_file(episode_dir) == episode_dir / "preview.mp4"
    assert "legacy QRDF metadata.json symbolic link" in caplog.text


def test_service_summary_does_not_require_removed_sdk_preview_property(
    tmp_path: Path, monkeypatch
) -> None:
    from data.integrations.qrdf import episode_metrics, service

    episode_dir = tmp_path / "episode_000001"
    episode_dir.mkdir()
    (episode_dir / "preview.mp4").write_bytes(b"legacy-preview")
    _write_metadata(episode_dir, {"preview_file": "preview.mp4"})
    episode = SimpleNamespace(
        episode_id="episode_000001",
        path=episode_dir,
        metadata=SimpleNamespace(timing=SimpleNamespace(duration_s=1.5)),
    )
    monkeypatch.setattr(
        episode_metrics, "episode_topic_names", lambda _episode: ["/camera/head/rgb"]
    )
    monkeypatch.setattr(
        episode_metrics,
        "load_metrics_json",
        lambda _episode_path: {"message_count": {"/camera/head/rgb": 3}},
    )
    monkeypatch.setattr(
        episode_metrics,
        "pick_camera_topic_from_names",
        lambda _topics: "/camera/head/rgb",
    )

    summary = service._summarize_episode(episode, allow_mcap=False)

    assert summary["preview_file"] == str(episode_dir / "preview.mp4")


def test_preview_export_default_does_not_require_removed_sdk_preview_property(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from qrdf.reader import episode as episode_module

    from data.integrations.qrdf import preview_export

    episode_dir = tmp_path / "episode_000001"
    episode_dir.mkdir()
    _write_metadata(episode_dir, {"preview_file": "legacy/preview.mp4"})
    fake_episode = SimpleNamespace(metadata=SimpleNamespace())
    monkeypatch.setattr(episode_module, "Episode", lambda _path: fake_episode)
    monkeypatch.setattr(
        preview_export,
        "write_browser_mp4",
        lambda _factory, output_path, *, fps: (Path(output_path), (1,)),
    )

    facts = preview_export.export_episode_preview_facts(
        episode_dir,
        camera_topic="/camera/head/rgb",
        fps=15.0,
    )

    assert facts.path == episode_dir / "legacy" / "preview.mp4"
