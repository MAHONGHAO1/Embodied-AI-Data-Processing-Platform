#!/usr/bin/env python3
"""Upload an unchanged QRDF Episode with a QuicStudio qs_ API key (stdlib only)."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


class UploadError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UploadError("API redirected the request; use the final Studio HTTPS origin")


class Client:
    def __init__(self, origin: str, key: str, timeout: float = 120):
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise UploadError("Use HTTPS (HTTP is allowed only for a loopback test server)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise UploadError(
                "Studio URL must not contain credentials, query parameters or fragments"
            )
        if parsed.path.rstrip("/") not in {"", "/api/v1"}:
            raise UploadError("Use the Studio origin or its /api/v1 URL")
        if not key.startswith("qs_") or any(ch.isspace() for ch in key):
            raise UploadError(
                "Use a qs_ API key from Studio API tokens, not a browser session token"
            )
        self.base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/api/v1", "", ""))
        self.key = key
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, *, body=None, query=None, content=None, retry=False):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": f"Bearer {self.key}"}
        if body is not None:
            content = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content is not None:
            headers["Content-Type"] = "application/octet-stream"
        # Only explicitly replayable requests retry. Creating a session is not replayed.
        attempts = 3 if retry else 1
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(url, data=content, headers=headers, method=method)
                with self.opener.open(req, timeout=self.timeout) as response:
                    payload = json.load(response)
                if payload.get("code") != 200:
                    raise UploadError(str(payload.get("message") or "API rejected the request"))
                return payload["data"]
            except urllib.error.HTTPError as exc:
                if retry and exc.code in {429, 502, 503, 504} and attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 4))
                    continue
                try:
                    detail = json.loads(exc.read()).get("detail", exc.reason)
                except (ValueError, OSError):
                    detail = exc.reason
                raise UploadError(f"HTTP {exc.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise UploadError(
                    f"Network failure ({type(exc).__name__}); rerun using the same state file"
                ) from None

    def upload_signed_part(
        self,
        url: str,
        headers: dict[str, str],
        source_file: Path,
        *,
        offset: int,
        size: int,
    ) -> str:
        """Stream one bounded part to a server-signed HTTPS URL.

        The Studio bearer token is deliberately not involved in this request. The
        signed URL and the headers returned by Studio are the complete capability
        for this one PUT. Redirects stay disabled so a capability cannot be sent
        to another origin.
        """
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise UploadError("Studio returned a non-HTTPS or credential-bearing signed URL")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise UploadError("Studio returned an invalid signed part size")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise UploadError("signed part offset is invalid")
        signed_headers: dict[str, str] = {}
        for name, value in dict(headers or {}).items():
            header_name = str(name)
            if header_name.lower() in {"authorization", "proxy-authorization", "cookie"}:
                raise UploadError("Studio returned credential-bearing signed headers")
            if not isinstance(value, str):
                raise UploadError("Studio returned invalid signed headers")
            if header_name.lower() == "content-length" and value != str(size):
                raise UploadError("Studio signed part length does not match the declared part")
            signed_headers[header_name] = value
        signed_headers["Content-Length"] = str(size)
        try:
            with source_file.open("rb") as stream:
                stream.seek(offset)
                request = urllib.request.Request(
                    url,
                    data=stream,
                    headers=signed_headers,
                    method="PUT",
                )
                with self.opener.open(request, timeout=self.timeout) as response:
                    if not 200 <= response.status < 300:
                        raise UploadError(f"Signed part upload returned HTTP {response.status}")
                    etag = str(response.headers.get("ETag") or "").strip().strip('"')
        except urllib.error.HTTPError as exc:
            raise UploadError(f"Signed part upload returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise UploadError(
                f"Signed part upload network failure ({type(exc).__name__}); rerun using the same state file"
            ) from None
        except OSError as exc:
            raise UploadError(f"Unable to read source part: {exc}") from None
        if not etag:
            raise UploadError("Signed part response did not include an ETag")
        return etag


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def episode_declaration(directory: Path, package_uid: str):
    root = directory.resolve(strict=True)
    raw = (root / "metadata.json").read_bytes()
    if len(raw) > 1024 * 1024:
        raise UploadError("metadata.json exceeds the declaration limit (1 MiB)")
    metadata_text = raw.decode("utf-8")
    metadata = json.loads(metadata_text)
    data_name = metadata.get("data_file")
    if not isinstance(data_name, str) or not data_name or "\\" in data_name:
        raise UploadError("metadata.data_file must be a relative file path")
    relative = PurePosixPath(data_name)
    if relative.is_absolute() or ".." in relative.parts or str(relative) != data_name:
        raise UploadError("metadata.data_file must stay inside the Episode directory")
    path = (root / data_name).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise UploadError("data_file is outside the Episode directory or is not a regular file")
    timing = metadata.get("timing") or {}
    start, end = timing.get("start_timestamp_ns"), timing.get("end_timestamp_ns")
    if any(isinstance(value, bool) or not str(value).isdigit() for value in (start, end)):
        raise UploadError("Episode timestamps must be exact integer nanoseconds")
    if int(start) >= int(end) or not metadata.get("episode_id"):
        raise UploadError("Episode identity or time range is invalid")
    size = path.stat().st_size
    if not size:
        raise UploadError("data_file must not be empty")
    digest = sha256_file(path)
    declaration = {
        "package_uid": package_uid,
        "source": {
            "episode_id": metadata["episode_id"],
            "start_ns": str(start),
            "end_ns": str(end),
            "metadata_sha256": hashlib.sha256(raw).hexdigest(),
            "data_mcap_sha256": digest,
        },
        "metadata_text": metadata_text,
        "data_file": {"path": data_name, "size_bytes": size, "sha256": digest},
    }
    return declaration, path


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def state_lock(path: Path):
    """Prevent two local processes from creating sessions for the same state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UploadError("Another upload is using this state file") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _episode_directories(args) -> list[Path]:
    directories = getattr(args, "episodes", None)
    if directories is None:
        directory = getattr(args, "episode", None)
        if isinstance(directory, (list, tuple)):
            directories = directory
        else:
            directories = [directory] if directory is not None else []
    if not directories:
        raise UploadError("At least one --episode directory is required")
    return [Path(directory) for directory in directories]


def _source_state_key(source: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _source_entries(args) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen_episode_ids: set[str] = set()
    seen_source_keys: set[str] = set()
    for directory in _episode_directories(args):
        declaration, source_file = episode_declaration(directory, args.package)
        episode_id = declaration["source"]["episode_id"]
        if episode_id in seen_episode_ids:
            raise UploadError("Episode IDs must be unique within one upload session")
        source_key = _source_state_key(declaration["source"])
        if source_key in seen_source_keys:
            raise UploadError("Duplicate Episode source identity")
        seen_episode_ids.add(episode_id)
        seen_source_keys.add(source_key)
        entries.append(
            {
                "declaration": declaration,
                "path": source_file,
                "key": source_key,
            }
        )
    return entries


def _load_state(path: Path, identity: dict) -> dict:
    if not path.exists():
        state = {"identity": identity}
    else:
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise UploadError("Resume state is not valid JSON; do not overwrite it") from exc
        if not isinstance(state, dict):
            raise UploadError("Resume state must contain a JSON object")
    if state.get("identity") != identity:
        raise UploadError("State belongs to different data or upload parameters; do not reuse it")
    sources = state.setdefault("sources", {})
    if not isinstance(sources, dict):
        raise UploadError("Resume state source records are invalid")
    for entry in identity["episodes"]:
        source_state = sources.setdefault(entry["state_key"], {})
        if not isinstance(source_state, dict):
            raise UploadError("Resume state source record is invalid")
    return state


def _state_for_entry(state: dict, entry: dict[str, object]) -> dict:
    return state["sources"][entry["key"]]


def _bind_declared_sources(
    declared: dict,
    entries: list[dict[str, object]],
    state: dict,
    state_path: Path,
) -> None:
    sources = declared.get("sources")
    if not isinstance(sources, list) or len(sources) != len(entries):
        raise UploadError("Declaration response did not identify every uploaded Episode")
    unused = list(sources)
    for entry in entries:
        declaration = entry["declaration"]
        expected_id = declaration["source"]["episode_id"]
        matches = [
            item
            for item in unused
            if isinstance(item, dict)
            and item.get("package_uid") == declaration["package_uid"]
            and item.get("episode_id") == expected_id
        ]
        if len(matches) != 1:
            raise UploadError("Declaration response did not identify the uploaded Episode")
        source_id = matches[0].get("source_id")
        if (
            not isinstance(source_id, str)
            or len(source_id) != 64
            or any(character not in "0123456789abcdef" for character in source_id)
        ):
            raise UploadError("Declaration response contained an invalid source ID")
        source_state = _state_for_entry(state, entry)
        if source_state.get("source_id") not in {None, source_id}:
            raise UploadError("Resume state source ID disagrees with the server declaration")
        source_state["source_id"] = source_id
        unused.remove(matches[0])
    save_state(state_path, state)


def _signed_origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise UploadError("Studio returned a non-HTTPS or credential-bearing signed URL")
    return f"https://{parsed.netloc.lower()}"


def _part_size(total_size: int, part_size: object, total_parts: object) -> tuple[int, int]:
    if (
        isinstance(part_size, bool)
        or not isinstance(part_size, int)
        or part_size <= 0
        or isinstance(total_parts, bool)
        or not isinstance(total_parts, int)
        or not 1 <= total_parts <= 10_000
        or math.ceil(total_size / part_size) != total_parts
    ):
        raise UploadError("Studio returned an invalid OSS multipart layout")
    return part_size, total_parts


def _expected_part_bytes(total_size: int, part_size: int, part_number: int) -> int:
    offset = (part_number - 1) * part_size
    return min(part_size, total_size - offset)


def check_session(session, identity):
    expected = {identity["package_uid"]}
    actual = {package["package_uid"] for package in session.get("packages", [])}
    if (
        session.get("workspace_id") != identity["workspace_id"]
        or session.get("collection_project_id") != identity["collection_project_id"]
        or actual != expected
        or session.get("upload_mode") != identity["transport"]
    ):
        raise UploadError(
            "Upload session does not match the selected workspace, project, package or transport"
        )


def _upload_chunked(
    client,
    args,
    entries: list[dict[str, object]],
    state: dict,
    state_path: Path,
    prefix: str,
    workspace: dict[str, int],
    *,
    progress=print,
):
    chunk_bytes = args.chunk_mib * 1024 * 1024
    for entry in entries:
        declaration = entry["declaration"]
        source_file = entry["path"]
        source_state = _state_for_entry(state, entry)
        source_id = source_state.get("source_id")
        total = math.ceil(source_file.stat().st_size / chunk_bytes)
        if total > 20_000:
            raise UploadError("More than 20,000 chunks; select a larger chunk size")
        status = client.request(
            "POST",
            prefix + "/chunked/init",
            body={
                **workspace,
                "total_chunks": total,
                "source_id": source_id,
            },
            retry=True,
        )
        if status.get("total_chunks") != total:
            raise UploadError("Server returned an unexpected chunk count")
        indices = status.get("uploaded_chunk_indices", [])
        if not isinstance(indices, list) or any(
            type(index) is not int or not 0 <= index < total for index in indices
        ):
            raise UploadError("Server returned invalid uploaded chunk indices")
        uploaded = set(indices)
        before = source_file.stat()
        with source_file.open("rb") as stream:
            for index in range(total):
                if index in uploaded:
                    continue
                expected = min(chunk_bytes, before.st_size - index * chunk_bytes)
                stream.seek(index * chunk_bytes)
                content = stream.read(expected)
                if len(content) != expected:
                    raise UploadError("Source changed while reading a chunk")
                client.request(
                    "PUT",
                    prefix + f"/chunked/{index}",
                    query={"source_id": source_id},
                    content=content,
                    retry=True,
                )
                uploaded.add(index)
                source_state["uploaded_chunk_indices"] = sorted(uploaded)
                save_state(state_path, state)
                progress(
                    f"Uploaded {declaration['source']['episode_id']} chunk {index + 1}/{total}"
                )
        after = source_file.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise UploadError("Source changed during upload; completion was not submitted")
    state["completion_pending"] = True
    save_state(state_path, state)
    session = client.request("POST", prefix + "/chunked/complete", body=workspace)
    state.pop("completion_pending", None)
    save_state(state_path, state)
    return session


def _upload_oss_multipart(
    client,
    args,
    entries: list[dict[str, object]],
    state: dict,
    state_path: Path,
    prefix: str,
    workspace: dict[str, int],
    *,
    progress=print,
):
    content_type = getattr(args, "content_type", None) or "application/octet-stream"
    session = None
    for entry in entries:
        declaration = entry["declaration"]
        source_file = entry["path"]
        source_state = _state_for_entry(state, entry)
        source_id = source_state.get("source_id")
        total_size = source_file.stat().st_size
        initialized = client.request(
            "POST",
            prefix + "/oss/init",
            body={
                **workspace,
                "total_size_bytes": total_size,
                "content_type": content_type,
                "source_id": source_id,
            },
            retry=True,
        )
        part_size, total_parts = _part_size(
            total_size,
            initialized.get("part_size_bytes"),
            initialized.get("total_parts"),
        )
        if source_state.get("part_size_bytes") not in {None, part_size} or source_state.get(
            "total_parts"
        ) not in {None, total_parts}:
            raise UploadError("Resume state OSS multipart layout disagrees with the server")
        source_state["part_size_bytes"] = part_size
        source_state["total_parts"] = total_parts
        parts = source_state.setdefault("parts", {})
        if not isinstance(parts, dict):
            raise UploadError("Resume state OSS part manifest is invalid")
        before = source_file.stat()
        for part_number in range(1, total_parts + 1):
            expected = _expected_part_bytes(total_size, part_size, part_number)
            saved_part = parts.get(str(part_number))
            if saved_part is not None:
                if (
                    not isinstance(saved_part, dict)
                    or not saved_part.get("etag")
                    or saved_part.get("size_bytes") != expected
                ):
                    raise UploadError("Resume state contains an invalid OSS part manifest")
                continue
            signed = client.request(
                "POST",
                prefix + "/oss/sign-part",
                body={
                    **workspace,
                    "part_number": part_number,
                    "source_id": source_id,
                },
                retry=True,
            )
            if signed.get("method") != "PUT":
                raise UploadError("Studio returned an invalid signed part method")
            if signed.get("content_length") != expected:
                raise UploadError("Studio signed part length does not match the source")
            url = signed.get("url")
            if not isinstance(url, str):
                raise UploadError("Studio did not return a signed part URL")
            origin = _signed_origin(url)
            prior_origin = source_state.get("signed_origin")
            if prior_origin not in {None, origin}:
                raise UploadError("Signed part URL origin changed during one upload")
            source_state["signed_origin"] = origin
            offset = (part_number - 1) * part_size
            etag = client.upload_signed_part(
                url,
                signed.get("headers") or {},
                source_file,
                offset=offset,
                size=expected,
            )
            parts[str(part_number)] = {"etag": etag, "size_bytes": expected}
            save_state(state_path, state)
            progress(
                f"Uploaded {declaration['source']['episode_id']} part {part_number}/{total_parts}"
            )
        after = source_file.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise UploadError("Source changed during upload; completion was not submitted")
        manifest = [
            {"part_number": part_number, "etag": parts[str(part_number)]["etag"]}
            for part_number in range(1, total_parts + 1)
        ]
        source_state["completion_pending"] = True
        save_state(state_path, state)
        session = client.request(
            "POST",
            prefix + "/oss/complete",
            body={**workspace, "parts": manifest, "source_id": source_id},
        )
        source_state.pop("completion_pending", None)
        source_state["completed"] = True
        save_state(state_path, state)
    return session


def run_upload(client, args, *, progress=print):
    transport = getattr(args, "transport", "chunked")
    if transport not in {"chunked", "oss_multipart"}:
        raise UploadError("Unsupported upload transport")
    entries = _source_entries(args)
    chunk_bytes = args.chunk_mib * 1024 * 1024 if transport == "chunked" else None
    identity = {
        "base": client.base,
        "workspace_id": args.workspace,
        "collection_project_id": args.project,
        "package_uid": args.package,
        "transport": transport,
        "chunk_bytes": chunk_bytes,
        "episodes": [
            {
                "state_key": entry["key"],
                "source": entry["declaration"]["source"],
                "data_file": entry["declaration"]["data_file"],
            }
            for entry in entries
        ],
    }
    state = _load_state(args.state, identity)
    if args.session_id:
        if state.get("session_id") not in {None, args.session_id}:
            raise UploadError("Explicit session ID disagrees with the state file")
        state["session_id"] = args.session_id
        state.pop("creating", None)
    if not state.get("session_id"):
        if state.get("creating"):
            raise UploadError(
                "Session creation outcome is unknown. Find the existing session and supply --session-id; no duplicate session was created"
            )
        state["creating"] = True
        save_state(args.state, state)
        created = client.request(
            "POST",
            "/upload-sessions",
            body={
                "workspace_id": args.workspace,
                "collection_project_id": args.project,
                "package_uids": [args.package],
                "upload_mode": transport,
            },
        )
        state["session_id"] = created["id"]
        state.pop("creating", None)
        save_state(args.state, state)
    session_id = state["session_id"]
    prefix = "/upload-sessions/" + urllib.parse.quote(session_id, safe="")
    workspace = {"workspace_id": args.workspace}
    session = client.request("GET", prefix, query=workspace, retry=True)
    check_session(session, identity)
    save_state(args.state, state)
    progress(f"Session {session_id}: {session['status']}")
    if session["status"] in {"init", "uploading"}:
        declarations = [entry["declaration"] for entry in entries]
        declared = client.request(
            "POST",
            prefix + "/declarations",
            body={**workspace, "items": declarations},
            retry=True,
        )
        _bind_declared_sources(declared, entries, state, args.state)
        if transport == "chunked":
            session = _upload_chunked(
                client,
                args,
                entries,
                state,
                args.state,
                prefix,
                workspace,
                progress=progress,
            )
        else:
            session = _upload_oss_multipart(
                client,
                args,
                entries,
                state,
                args.state,
                prefix,
                workspace,
                progress=progress,
            )
    deadline = time.monotonic() + args.wait_seconds
    while args.wait_seconds and session["status"] not in {"succeeded", "failed", "cancelled"}:
        if time.monotonic() >= deadline:
            save_state(args.state, {**state, "last_status": session["status"]})
            raise UploadError(
                f"Processing still {session['status']}; resume with the same command to continue waiting"
            )
        time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
        session = client.request("GET", prefix, query=workspace, retry=True)
        progress(f"Session {session_id}: {session['status']}")
    save_state(args.state, {**state, "last_status": session["status"]})
    if session["status"] in {"failed", "cancelled"}:
        raise UploadError(
            f"Upload {session['status']}: {session.get('error_code') or ''} {session.get('error_message') or ''}"
        )
    return session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Studio HTTPS origin")
    parser.add_argument(
        "--key-file", type=Path, help="Private file containing qs_ key; otherwise STUDIO_API_KEY"
    )
    parser.add_argument("--workspace", required=True, type=int)
    parser.add_argument("--project", required=True, type=int)
    parser.add_argument(
        "--package", required=True, help="Assigned package UID from the task manifest"
    )
    parser.add_argument(
        "--episode",
        dest="episode",
        action="append",
        required=True,
        type=Path,
        help="Episode directory; repeat to upload multiple Episodes into the same package",
    )
    parser.add_argument(
        "--transport",
        choices=("oss_multipart", "chunked"),
        default="oss_multipart",
        help="Upload transport; OSS direct upload is the default, chunked is the compatibility path",
    )
    parser.add_argument(
        "--state",
        required=True,
        type=Path,
        help="Resume state outside the source Episode; contains no API key",
    )
    parser.add_argument(
        "--session-id", help="Explicitly resume a session if its creation response was lost"
    )
    parser.add_argument("--chunk-mib", type=int, default=8, choices=range(1, 65))
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=1800,
        help="Wait for parsing; 0 returns after upload acceptance",
    )
    parser.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args()
    if min(args.workspace, args.project, args.poll_seconds) <= 0 or args.wait_seconds < 0:
        parser.error("IDs and poll interval must be positive; wait cannot be negative")
    if any(args.state.resolve().is_relative_to(directory.resolve()) for directory in args.episode):
        parser.error("Store resume state outside the original Episode directory")
    key = ""
    try:
        key = (
            args.key_file.read_text().strip()
            if args.key_file
            else os.environ.get("STUDIO_API_KEY", "").strip()
        )
        with state_lock(args.state):
            result = run_upload(
                Client(args.url, key), args, progress=lambda text: print(text, file=sys.stderr)
            )
        print(json.dumps(result, ensure_ascii=False))
    except (UploadError, OSError, ValueError, KeyError) as exc:
        message = str(exc).replace(key, "[redacted]") if key else str(exc)
        print(f"Upload failed: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; run the same command to resume.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
