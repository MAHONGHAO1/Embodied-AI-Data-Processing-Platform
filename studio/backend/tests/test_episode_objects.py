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
        (lambda e: e[0].update(path="."), "episode_object_path_unsafe"),
        (lambda e: e[0].update(path="./"), "episode_object_path_unsafe"),
        (lambda e: e[0].update(path="data\x00.mcap"), "episode_object_path_unsafe"),
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


@pytest.mark.parametrize(
    "mutate, code",
    [
        # metadata/admission_report/preview_manifest are bound to a fixed standard path.
        (lambda e: e[1].update(path="meta/metadata.json"), "episode_object_path_kind_mismatch"),
        (
            lambda e: e[2].update(path="admission_report.json"),
            "episode_object_path_kind_mismatch",
        ),
        (
            lambda e: e[3].update(path="media/preview/manifests.json"),
            "episode_object_path_kind_mismatch",
        ),
        # preview_video/preview_timeline must live under media/preview/.
        (
            lambda e: e[4].update(path="generations/g1/head_rgb.mp4"),
            "episode_object_path_kind_mismatch",
        ),
        (
            lambda e: e[5].update(path="generations/g1/head_rgb.timeline.json"),
            "episode_object_path_kind_mismatch",
        ),
        # Every non-data kind must have role "process".
        (
            lambda e: (
                e[1].update(role="raw"),
                e[1]["ref"].update(bucket_role="raw"),
            ),
            "episode_object_role_mismatch",
        ),
    ],
)
def test_require_verified_objects_binds_kind_to_path_and_role(mutate, code):
    entries = verified_entries()
    mutate(entries)
    with pytest.raises(EpisodeObjectsError) as excinfo:
        require_verified_objects(parse_objects(entries))
    assert excinfo.value.code == code


def test_non_rgb_episode_needs_no_preview_objects():
    entries = [e for e in verified_entries() if not e["kind"].startswith("preview_")]
    objects = parse_objects(entries)
    require_verified_objects(objects)
    assert preview_streams(objects) == []


def test_parse_accepts_empty_and_rejects_non_list():
    assert parse_objects([]) == []
    with pytest.raises(EpisodeObjectsError):
        parse_objects({"object_key": "x"})
