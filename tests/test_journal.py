"""Unit journal only reuses verified, digest-matching artifacts."""
from __future__ import annotations

from robodata.journal import UnitJournal, artifact_record, file_sha256


def test_journal_reuses_only_matching_artifacts(tmp_path):
    artifact = tmp_path / "episode.bin"
    artifact.write_bytes(b"first")
    journal = UnitJournal(tmp_path / "journal.jsonl", root=tmp_path)
    record = artifact_record(artifact, root=tmp_path)
    journal.complete("preview:episode:1", "digest-a", [record], meta={"length": 1})
    assert journal.is_done("preview:episode:1", "digest-a") is True
    assert journal.is_done("preview:episode:1", "digest-b") is False

    artifact.write_bytes(b"tampered")
    assert journal.is_done("preview:episode:1", "digest-a") is False


def test_journal_directory_artifact_and_failure(tmp_path):
    directory = tmp_path / "episode"
    directory.mkdir()
    (directory / "info.json").write_text("{}", encoding="utf-8")
    (directory / "video.mp4").write_bytes(b"video")
    journal = UnitJournal(tmp_path / "journal.jsonl", root=tmp_path)
    record = artifact_record(directory, root=tmp_path)
    assert record["kind"] == "directory"
    journal.complete("preview:episode:2", "digest", [record])
    assert journal.is_done("preview:episode:2", "digest") is True
    (directory / "video.mp4").write_bytes(b"changed")
    assert journal.is_done("preview:episode:2", "digest") is False
    journal.fail("preview:episode:3", "digest", "boom")
    assert journal.is_done("preview:episode:3", "digest") is False
    assert journal.summary()["failed"] == 1
    assert file_sha256(directory / "info.json")
