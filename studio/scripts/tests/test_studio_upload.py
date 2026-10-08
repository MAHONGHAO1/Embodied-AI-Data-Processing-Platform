"""Contract tests for the resumable Studio upload CLI."""

import argparse
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "studio_upload", Path(__file__).parents[1] / "studio_upload.py"
)
upload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upload)


class FakeResponse:
    status = 200

    def __init__(self, headers=None):
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class CaptureOpener:
    def __init__(self, *, etag="signed-etag"):
        self.requests = []
        self.payloads = []
        self.etag = etag

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        length = int(request.get_header("Content-length") or request.get_header("Content-Length"))
        self.payloads.append(request.data.read(length))
        return FakeResponse({"ETag": f'"{self.etag}"'})


class FakeServer:
    base = "https://studio.example/api/v1"

    def __init__(self, *, indices=None, status="init", part_size=4):
        self.indices = indices
        self.status = status
        self.initial_status = status
        self.part_size = part_size
        self.calls = []
        self.chunks = {}
        self.parts = {}
        self.part_attempts = []
        self.declarations = []
        self.source_ids = {}
        self.completed_sources = set()
        self.fail_part_once = None
        self.fail_complete_once = False

    def _session(self, mode="chunked"):
        return {
            "id": "session",
            "workspace_id": 1,
            "collection_project_id": 2,
            "packages": [{"package_uid": "package"}],
            "upload_mode": mode,
            "status": self.status,
        }

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        body = kwargs.get("body") or {}
        if path == "/upload-sessions":
            self.status = self.initial_status
            return self._session(body.get("upload_mode", "chunked"))
        if method == "GET":
            mode = self.calls[0][2]["body"].get("upload_mode", "chunked")
            return self._session(mode)
        if path.endswith("/declarations"):
            self.declarations = list(body.get("items") or [])
            self.source_ids = {
                item["source"]["episode_id"]: hashlib.sha256(
                    item["source"]["episode_id"].encode()
                ).hexdigest()
                for item in self.declarations
            }
            return {
                "sources": [
                    {
                        "source_id": self.source_ids[item["source"]["episode_id"]],
                        "package_uid": item["package_uid"],
                        "episode_id": item["source"]["episode_id"],
                    }
                    for item in self.declarations
                ]
            }
        source_id = body.get("source_id") or kwargs.get("query", {}).get("source_id")
        if path.endswith("/chunked/init"):
            total = body["total_chunks"]
            if self.indices is None:
                return {"uploaded_chunks": 0, "total_chunks": total}
            return {
                "uploaded_chunks": len(self.indices),
                "uploaded_chunk_indices": self.indices,
                "total_chunks": total,
            }
        if "/chunked/" in path and method == "PUT":
            index = int(path.rsplit("/", 1)[-1])
            self.chunks[(source_id, index)] = kwargs["content"]
            self.status = "uploading"
            return {}
        if path.endswith("/chunked/complete"):
            self.status = "uploaded"
            return self._session("chunked")
        if path.endswith("/oss/init"):
            total_size = body["total_size_bytes"]
            total_parts = (total_size + self.part_size - 1) // self.part_size
            self.status = "uploading"
            return {
                "id": "session",
                "status": "uploading",
                "total_size_bytes": total_size,
                "part_size_bytes": self.part_size,
                "total_parts": total_parts,
            }
        if path.endswith("/oss/sign-part"):
            part_number = body["part_number"]
            declaration = next(
                item
                for item in self.declarations
                if self.source_ids[item["source"]["episode_id"]] == source_id
            )
            size = declaration["data_file"]["size_bytes"]
            total_parts = (size + self.part_size - 1) // self.part_size
            expected = (
                self.part_size
                if part_number < total_parts
                else size - self.part_size * (total_parts - 1)
            )
            return {
                "method": "PUT",
                "url": f"https://uploads.example.test/{source_id}/{part_number}",
                "headers": {"Content-Type": "application/octet-stream"},
                "content_length": expected,
                "expires_in": 300,
                "part_number": part_number,
            }
        if path.endswith("/oss/complete"):
            manifest = body.get("parts") or []
            expected_manifest = [
                {
                    "part_number": number,
                    "etag": hashlib.sha256(self.parts[(source_id, number)]["content"]).hexdigest()[
                        :16
                    ],
                }
                for number in range(1, len(manifest) + 1)
            ]
            if manifest != expected_manifest:
                raise upload.UploadError("multipart part manifest integrity mismatch")
            if self.fail_complete_once:
                self.fail_complete_once = False
                self.status = "uploaded"
                raise upload.UploadError("response lost after OSS completion")
            self.completed_sources.add(source_id)
            if len(self.completed_sources) == len(self.source_ids):
                self.status = "uploaded"
            return self._session("oss_multipart")
        raise AssertionError(f"unexpected request {method} {path}")

    def upload_signed_part(self, url, headers, source_file, *, offset, size):
        part_number = int(url.rsplit("/", 1)[-1])
        source_id = url.rstrip("/").split("/")[-2]
        if self.fail_part_once == (source_id, part_number):
            self.fail_part_once = None
            raise upload.UploadError("temporary signed upload failure")
        with source_file.open("rb") as stream:
            stream.seek(offset)
            content = stream.read(size)
        if len(content) != size:
            raise upload.UploadError("short direct part")
        self.parts[(source_id, part_number)] = {
            "content": content,
            "headers": headers,
        }
        self.part_attempts.append((source_id, part_number))
        return hashlib.sha256(content).hexdigest()[:16]


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.episode = self.make_episode("episode", b"a" * 10)
        self.raw = (self.episode / "metadata.json").read_bytes()
        self.args = argparse.Namespace(
            episode=self.episode,
            package="package",
            chunk_mib=1,
            workspace=1,
            project=2,
            state=self.root / "state.json",
            session_id=None,
            wait_seconds=0,
            poll_seconds=1,
        )

    def make_episode(self, episode_id, payload, *, start=0):
        directory = self.root / episode_id
        directory.mkdir()
        metadata = {
            "episode_id": episode_id,
            "data_file": "data.mcap",
            "timing": {
                "start_timestamp_ns": str(start),
                "end_timestamp_ns": str(start + 10),
            },
        }
        (directory / "metadata.json").write_text(json.dumps(metadata, separators=(",", ":")))
        (directory / "data.mcap").write_bytes(payload)
        return directory

    def run_client(self, server, args=None):
        return upload.run_upload(server, args or self.args, progress=lambda _: None)

    def test_exact_metadata_bytes_and_nanoseconds(self):
        data, path = upload.episode_declaration(self.episode, "package")
        self.assertEqual(data["metadata_text"].encode(), self.raw)
        self.assertEqual(data["source"]["metadata_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(data["source"]["start_ns"], "0")
        self.assertEqual(path.read_bytes(), b"a" * 10)

    def test_multi_episode_oss_uploads_same_relative_path_independently(self):
        second = self.make_episode("second", b"b" * 9, start=20)
        values = vars(self.args).copy()
        values.update(episode=[self.episode, second], transport="oss_multipart")
        args = argparse.Namespace(**values)
        server = FakeServer(part_size=4)
        result = self.run_client(server, args)
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(len(server.declarations), 2)
        self.assertEqual(len(server.completed_sources), 2)
        self.assertEqual(
            {payload["content"] for payload in server.parts.values()},
            {b"a" * 4, b"a" * 2, b"b" * 4, b"b"},
        )
        persisted = json.loads(args.state.read_text())
        self.assertEqual(len(persisted["sources"]), 2)
        self.assertNotIn("qs_private", args.state.read_text())

    def test_oss_resume_is_independent_per_episode_and_replays_only_missing_parts(self):
        second = self.make_episode("second", b"b" * 9, start=20)
        values = vars(self.args).copy()
        values.update(episode=[self.episode, second], transport="oss_multipart")
        args = argparse.Namespace(**values)
        server = FakeServer(part_size=4)
        second_id = hashlib.sha256(b"second").hexdigest()
        server.fail_part_once = (second_id, 2)
        with self.assertRaisesRegex(upload.UploadError, "temporary signed upload failure"):
            self.run_client(server, args)
        first_id = hashlib.sha256(b"episode").hexdigest()
        self.assertEqual(
            sorted(number for source, number in server.parts if source == first_id), [1, 2, 3]
        )
        self.assertEqual(
            sorted(number for source, number in server.parts if source == second_id), [1]
        )
        first_attempts = len([source for source, _ in server.part_attempts if source == first_id])
        self.run_client(server, args)
        self.assertEqual(
            len([source for source, _ in server.part_attempts if source == first_id]),
            first_attempts,
        )
        self.assertEqual(
            sorted(number for source, number in server.parts if source == first_id), [1, 2, 3]
        )
        self.assertEqual(
            sorted(number for source, number in server.parts if source == second_id), [1, 2, 3]
        )

    def test_oss_completion_recovery_does_not_create_new_session_or_reupload_parts(self):
        args = argparse.Namespace(**vars(self.args), transport="oss_multipart")
        server = FakeServer(part_size=4)
        server.fail_complete_once = True
        with self.assertRaisesRegex(upload.UploadError, "response lost"):
            self.run_client(server, args)
        create_calls = [
            call for call in server.calls if call[0] == "POST" and call[1] == "/upload-sessions"
        ]
        uploaded_parts = len(server.parts)
        result = self.run_client(server, args)
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(len(server.parts), uploaded_parts)

    def test_signed_part_request_has_no_api_bearer_or_redirect(self):
        client = upload.Client("https://studio.example", "qs_private")
        opener = CaptureOpener()
        client.opener = opener
        etag = client.upload_signed_part(
            "https://uploads.example.test/object?signature=opaque",
            {"Content-Type": "application/octet-stream"},
            self.episode / "data.mcap",
            offset=0,
            size=4,
        )
        request, _timeout = opener.requests[0]
        self.assertEqual(etag, "signed-etag")
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Cookie", request.headers)
        self.assertEqual(request.get_header("Content-length"), "4")
        self.assertTrue(hasattr(request.data, "read"))
        self.assertEqual(opener.payloads, [b"aaaa"])
        with self.assertRaisesRegex(upload.UploadError, "non-HTTPS"):
            client.upload_signed_part(
                "http://uploads.example.test/object",
                {},
                self.episode / "data.mcap",
                offset=0,
                size=4,
            )

    def test_oss_rejects_signed_origin_change(self):
        args = argparse.Namespace(**vars(self.args), transport="oss_multipart")
        server = FakeServer(part_size=4)
        original = server.request

        def changed_origin(method, path, **kwargs):
            response = original(method, path, **kwargs)
            if path.endswith("/oss/sign-part") and kwargs["body"]["part_number"] == 2:
                response["url"] = response["url"].replace(
                    "uploads.example.test", "other.example.test"
                )
            return response

        server.request = changed_origin
        with self.assertRaisesRegex(upload.UploadError, "origin changed"):
            self.run_client(server, args)

    def test_chunked_transport_remains_compatible_for_multiple_episodes(self):
        second = self.make_episode("second", b"b" * 9, start=20)
        values = vars(self.args).copy()
        values.update(episode=[self.episode, second], transport="chunked")
        args = argparse.Namespace(**values)
        server = FakeServer(indices=None)
        result = self.run_client(server, args)
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual({key[1] for key in server.chunks}, {0})
        self.assertEqual(len(server.chunks), 2)

    def test_ambiguous_session_creation_is_not_replayed(self):
        server = FakeServer()
        original = server.request

        def request(method, path, **kwargs):
            if path == "/upload-sessions":
                raise upload.UploadError("response lost")
            return original(method, path, **kwargs)

        server.request = request
        with self.assertRaisesRegex(upload.UploadError, "response lost"):
            self.run_client(server)
        with self.assertRaisesRegex(upload.UploadError, "outcome is unknown"):
            self.run_client(FakeServer())

    def test_changed_source_cannot_resume_old_state(self):
        self.run_client(FakeServer())
        (self.episode / "data.mcap").write_bytes(b"changed")
        with self.assertRaisesRegex(upload.UploadError, "different data"):
            self.run_client(FakeServer())

    def test_session_scope_mismatch_stops_before_upload(self):
        server = FakeServer()
        original = server.request

        def request(method, path, **kwargs):
            value = original(method, path, **kwargs)
            if method == "GET":
                value["workspace_id"] = 3
            return value

        server.request = request
        with self.assertRaisesRegex(upload.UploadError, "does not match"):
            self.run_client(server)
        self.assertFalse(server.chunks)

    def test_failed_parse_is_not_reported_as_success(self):
        with self.assertRaisesRegex(upload.UploadError, "Upload failed"):
            self.run_client(FakeServer(status="failed"))

    def test_invalid_indices_do_not_skip_upload(self):
        with self.assertRaisesRegex(upload.UploadError, "invalid uploaded chunk"):
            self.run_client(FakeServer(indices=[True]))

    def test_lock_prevents_concurrent_session_creation(self):
        with upload.state_lock(self.args.state):  # noqa: SIM117
            with self.assertRaisesRegex(upload.UploadError, "Another upload"):
                with upload.state_lock(self.args.state):
                    self.fail("must not acquire twice")
        with upload.state_lock(self.args.state):
            pass

    def test_rejects_traversal_and_symlink_escape(self):
        outside = self.episode.parent / "outside"
        outside.write_text("data")
        (self.episode / "data.mcap").unlink()
        (self.episode / "data.mcap").symlink_to(outside)
        with self.assertRaisesRegex(upload.UploadError, "outside"):
            upload.episode_declaration(self.episode, "package")

    def test_rejects_insecure_origin_and_browser_token(self):
        for origin, key in [
            ("http://example.com", "qs_test"),
            ("https://u:p@example.com", "qs_test"),
            ("https://example.com", "jwt"),
        ]:
            with self.assertRaises(upload.UploadError):
                upload.Client(origin, key)

    def test_does_not_follow_auth_redirects(self):
        with self.assertRaisesRegex(upload.UploadError, "redirected"):
            upload.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere")


if __name__ == "__main__":
    unittest.main()
