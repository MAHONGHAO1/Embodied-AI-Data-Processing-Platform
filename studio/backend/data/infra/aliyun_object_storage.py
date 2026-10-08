"""Aliyun OSS implementation with the same immutable identity contract."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from typing import Any

from data.infra.object_storage import (
    MultipartPart,
    ObjectStorageError,
    StorageListPage,
    StorageNotReady,
    StorageObjectIntegrityError,
    StorageObjectNotFound,
    StorageObjectRef,
)
from data.services.storage_bucket_config import StorageBucketConfig


class AliyunObjectStorage:
    def __init__(
        self,
        bucket_config: StorageBucketConfig,
        *,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
        region_name: str = "cn-beijing",
        presign_expires: int = 900,
        browser_endpoint_url: str | None = None,
        bucket_factory: Any | None = None,
    ) -> None:
        self.bucket_config = bucket_config
        self.region_name = region_name
        endpoint = str(endpoint_url or "").strip()
        key = str(access_key_id or "").strip()
        secret = str(access_key_secret or "").strip()
        if not endpoint or not key or not secret:
            raise StorageNotReady(
                "Aliyun OSS requires endpoint_url, access_key_id and access_key_secret"
            )
        self.presign_expires = max(1, int(presign_expires))
        self.endpoint_url = endpoint if endpoint.startswith("http") else "https://" + endpoint
        browser = str(browser_endpoint_url or self.endpoint_url).strip()
        self.browser_endpoint_url = browser if browser.startswith("http") else "https://" + browser
        try:
            import oss2
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise StorageNotReady("oss2 is not installed") from exc
        auth = oss2.Auth(key, secret)
        self._oss2 = oss2
        self._bucket_factory = bucket_factory or (
            lambda endpoint, name: oss2.Bucket(auth, endpoint, name)
        )

    def _bucket(self, ref: StorageObjectRef, endpoint: str | None = None):
        name = self.bucket_config.buckets.get(ref.bucket_role)
        if not name or not ref.object_key.strip():
            raise ObjectStorageError("invalid storage object identity")
        if ref.object_key.startswith("/"):
            raise ObjectStorageError("object_key must not have a leading slash")
        return self._bucket_factory(endpoint or self.endpoint_url, name)

    @staticmethod
    def _validate_write_ref(ref: StorageObjectRef) -> None:
        if ref.version_id or ref.etag:
            raise ObjectStorageError(
                "new object writes require an incomplete identity without version_id or etag"
            )

    def _assert_absent(self, ref: StorageObjectRef) -> None:
        try:
            self._bucket(ref).head_object(ref.object_key)
        except Exception as exc:
            status = getattr(exc, "status", None)
            code = str(getattr(exc, "code", "") or "").lower()
            if status == 404 or code in {"nosuchkey", "notfound", "no_such_key"}:
                return
            message = str(exc).lower()
            if "404" in message or "not found" in message:
                return
            raise ObjectStorageError(f"OSS object existence check failed: {exc}") from exc
        raise ObjectStorageError("object key already exists")

    @staticmethod
    def _metadata(meta: Any) -> dict[str, Any]:
        # oss2 HeadObjectResult exposes user metadata through ``headers``.
        # Keep the old ``metadata`` fallback only for alternate compatible
        # clients; the SDK itself does not provide that attribute.
        values = getattr(meta, "headers", None)
        if not isinstance(values, Mapping):
            values = getattr(meta, "metadata", {})
            return {
                str(key).lower().removeprefix("x-oss-meta-"): value
                for key, value in (values or {}).items()
            }
        return {
            str(key).lower().removeprefix("x-oss-meta-"): value
            for key, value in values.items()
            if str(key).lower().startswith("x-oss-meta-")
        }

    @staticmethod
    def _version_id(meta: Any) -> str | None:
        # oss2 uses ``versionid`` (without an underscore) on RequestResult.
        # The fallback keeps the provider usable with S3-shaped compatible
        # clients used by local integrations.
        value = getattr(meta, "versionid", None)
        if value is None or not str(value).strip():
            value = getattr(meta, "version_id", None)
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _is_missing_error(exc: Exception) -> bool:
        status = getattr(exc, "status", None)
        code = str(getattr(exc, "code", "") or "").lower()
        message = str(exc).lower()
        return bool(
            status == 404
            or code in {"nosuchkey", "notfound", "no_such_key", "nosuchversion"}
            or "404" in message
            or "not found" in message
        )

    @staticmethod
    def _is_integrity_error(exc: Exception) -> bool:
        status = getattr(exc, "status", None)
        code = str(getattr(exc, "code", "") or "").lower()
        return bool(
            status == 412
            or code
            in {
                "preconditionfailed",
                "conditionnotmatch",
                "condition_not_match",
                "conditionalrequestconflict",
            }
        )

    def object_uri(self, ref: StorageObjectRef) -> str:
        self._bucket(ref)
        return f"oss://{self.bucket_config.buckets[ref.bucket_role]}/{ref.object_key}"

    def healthcheck(self) -> None:
        try:
            for role, bucket in self.bucket_config.buckets.items():
                if not bucket:
                    raise StorageNotReady(f"{role} storage bucket is not configured")
                self._bucket(StorageObjectRef(role, "healthcheck", None, "", 0)).get_bucket_info()
        except Exception as exc:
            raise StorageNotReady("OSS endpoint or raw bucket is unavailable") from exc

    def sign_get(self, ref: StorageObjectRef, *, expires: int = 900) -> str:
        if not ref.version_id and not ref.etag:
            raise ObjectStorageError("completed source requires version_id or etag")
        bucket = self._bucket(ref, self.browser_endpoint_url)
        params = {"versionId": ref.version_id} if ref.version_id else {}
        headers = {"If-Match": ref.etag} if not ref.version_id and ref.etag else {}
        try:
            return str(
                bucket.sign_url(
                    "GET",
                    ref.object_key,
                    max(1, int(expires)),
                    params=params,
                    headers=headers,
                )
            )
        except Exception as exc:
            raise ObjectStorageError(f"OSS sign_get failed: {exc}") from exc

    def download_file(self, ref: StorageObjectRef, destination: str) -> StorageObjectRef:
        if not ref.version_id and not ref.etag:
            raise ObjectStorageError("completed source requires version_id or etag")
        bucket = self._bucket(ref)
        params = {"versionId": ref.version_id} if ref.version_id else None
        headers = {"If-Match": ref.etag} if not ref.version_id else None
        response = None
        temp = ""
        try:
            response = bucket.get_object(ref.object_key, headers=headers, params=params)
            parent = os.path.dirname(os.path.abspath(destination)) or "."
            os.makedirs(parent, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".quic-download-", dir=parent)
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "wb") as target:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if size != ref.size_bytes:
                raise StorageObjectIntegrityError(
                    "downloaded object size does not match source identity"
                )
            calculated = digest.hexdigest()
            if ref.sha256 and calculated != ref.sha256:
                raise StorageObjectIntegrityError(
                    "downloaded object sha256 does not match source identity"
                )
            meta = bucket.head_object(ref.object_key, headers=headers, params=params)
            raw_etag = str(getattr(meta, "etag", "") or "").strip('"')
            raw_version = self._version_id(meta)
            if ref.version_id and raw_version != ref.version_id:
                raise StorageObjectIntegrityError("object version changed during download")
            if not ref.version_id and raw_etag != ref.etag.strip('"'):
                raise StorageObjectIntegrityError("object etag changed during download")
            os.replace(temp, destination)
            temp = ""
            return StorageObjectRef(
                ref.bucket_role,
                ref.object_key,
                raw_version or ref.version_id,
                raw_etag or ref.etag,
                size,
                calculated,
            )
        except ObjectStorageError:
            raise
        except Exception as exc:
            if self._is_missing_error(exc):
                raise StorageObjectNotFound("OSS download object is unavailable") from exc
            if self._is_integrity_error(exc):
                raise StorageObjectIntegrityError("OSS download object identity changed") from exc
            raise ObjectStorageError(f"OSS download failed: {exc}") from exc
        finally:
            if response is not None and hasattr(response, "close"):
                response.close()
            if temp:
                try:
                    os.unlink(temp)
                except FileNotFoundError:
                    pass

    def put_worker_object(self, ref: StorageObjectRef, source_path: str) -> StorageObjectRef:
        try:
            self._validate_write_ref(ref)
            bucket = self._bucket(ref)
            self._assert_absent(ref)
            digest = hashlib.sha256()
            size = 0
            with open(source_path, "rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            calculated = digest.hexdigest()
            bucket.put_object_from_file(
                ref.object_key,
                source_path,
                headers={
                    "x-oss-meta-sha256": calculated,
                    # OSS does not implement the generic HTTP conditional
                    # header here.  Use its native guard so a worker can
                    # never overwrite an immutable process object.
                    "x-oss-forbid-overwrite": "true",
                },
            )
            persisted = self.head(ref)
            if persisted.size_bytes != size or str(persisted.sha256 or "").strip() != calculated:
                raise ObjectStorageError("persisted worker object does not match source")
            return StorageObjectRef(
                ref.bucket_role,
                ref.object_key,
                persisted.version_id,
                persisted.etag,
                size,
                calculated,
            )
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError(f"OSS worker upload failed: {exc}") from exc

    def list_prefix(
        self, bucket_role: str, prefix: str, *, continuation_token: str | None, max_keys: int
    ) -> StorageListPage:
        if not isinstance(prefix, str) or not prefix or prefix.startswith("/") or "\\" in prefix:
            raise ObjectStorageError("prefix is invalid")
        if not isinstance(max_keys, int) or isinstance(max_keys, bool) or not 1 <= max_keys <= 1000:
            raise ObjectStorageError("prefix page size is invalid")
        checked_prefix = prefix.rstrip("/") + "/"
        try:
            result = self._bucket(
                StorageObjectRef(bucket_role, checked_prefix + "probe", None, "", 0)
            ).list_objects(
                prefix=checked_prefix, marker=continuation_token or "", max_keys=max_keys
            )
            objects = []
            for item in getattr(result, "object_list", ()) or ():
                key = str(getattr(item, "key", "") or "")
                if key.startswith(checked_prefix):
                    objects.append(
                        StorageObjectRef(
                            bucket_role,
                            key,
                            self._version_id(item),
                            str(getattr(item, "etag", "") or "").strip('"'),
                            int(getattr(item, "size", 0) or 0),
                            None,
                        )
                    )
            token = str(getattr(result, "next_marker", "") or "") or None
            if getattr(result, "is_truncated", False) and (
                not token or token == continuation_token
            ):
                raise ObjectStorageError("OSS prefix continuation did not advance")
            return StorageListPage(tuple(objects), token)
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError(f"OSS prefix listing failed: {exc}") from exc

    def create_multipart(self, ref: StorageObjectRef) -> str:
        try:
            self._validate_write_ref(ref)
            self._assert_absent(ref)
            # OSS requires its provider-specific overwrite guard for
            # multipart initiation; If-None-Match is rejected by OSS2/OSS.
            headers = {"x-oss-forbid-overwrite": "true"}
            if ref.sha256:
                headers["x-oss-meta-sha256"] = ref.sha256
            result = self._bucket(ref).init_multipart_upload(
                ref.object_key,
                headers=headers,
            )
            return str(result.upload_id)
        except Exception as exc:
            raise ObjectStorageError(f"OSS multipart create failed: {exc}") from exc

    def sign_part(
        self,
        ref: StorageObjectRef,
        upload_id: str,
        part_number: int,
        *,
        content_md5: str | None = None,
    ) -> str:
        if not upload_id or part_number < 1:
            raise ObjectStorageError("upload_id and positive part_number are required")
        headers = {"Content-Type": "application/octet-stream"}
        if content_md5:
            headers["Content-MD5"] = content_md5
        try:
            return str(
                self._bucket(ref, self.browser_endpoint_url).sign_url(
                    "PUT",
                    ref.object_key,
                    self.presign_expires,
                    params={"partNumber": str(part_number), "uploadId": upload_id},
                    headers=headers,
                )
            )
        except Exception as exc:
            raise ObjectStorageError(f"OSS multipart sign failed: {exc}") from exc

    def list_parts(self, ref: StorageObjectRef, upload_id: str):
        if not upload_id:
            raise ObjectStorageError("upload_id is required")
        try:
            result = self._bucket(ref).list_parts(ref.object_key, upload_id)
            return tuple(
                MultipartPart(
                    int(part.part_number),
                    str(part.etag).strip('"'),
                    int(part.size),
                )
                for part in result.parts
            )
        except Exception as exc:
            raise ObjectStorageError(f"OSS multipart list failed: {exc}") from exc

    def complete_multipart(self, ref: StorageObjectRef, upload_id: str, parts):
        if not upload_id or not parts:
            raise ObjectStorageError("upload_id and at least one part are required")
        self._validate_write_ref(ref)
        try:
            self._assert_absent(ref)
            self._bucket(ref).complete_multipart_upload(
                ref.object_key,
                upload_id,
                [
                    self._oss2.models.PartInfo(part.number, part.etag, part.size_bytes)
                    for part in sorted(parts, key=lambda item: item.number)
                ],
                # Keep the final merge immutable as well.  OSS documents this
                # header for CompleteMultipartUpload; If-None-Match is not
                # implemented by the OSS endpoint.
                headers={"x-oss-forbid-overwrite": "true"},
            )
            completed = self.head(ref)
            if ref.size_bytes and completed.size_bytes != ref.size_bytes:
                raise ObjectStorageError(
                    "completed multipart object size does not match source identity"
                )
            if ref.sha256 and completed.sha256 != ref.sha256:
                raise ObjectStorageError("completed multipart object lacks a verified hash")
            return completed
        except Exception as exc:
            raise ObjectStorageError(f"OSS multipart complete failed: {exc}") from exc

    def abort_multipart(self, ref: StorageObjectRef, upload_id: str) -> None:
        if not upload_id:
            raise ObjectStorageError("upload_id is required")
        try:
            self._bucket(ref).abort_multipart_upload(ref.object_key, upload_id)
        except Exception as exc:
            raise ObjectStorageError(f"OSS multipart abort failed: {exc}") from exc

    def list_multipart_upload_ids(self, ref: StorageObjectRef) -> tuple[str, ...]:
        """List unfinished uploads for this exact immutable key only."""
        key_marker = ""
        upload_id_marker = ""
        upload_ids: list[str] = []
        try:
            while True:
                result = self._bucket(ref).list_multipart_uploads(
                    prefix=ref.object_key,
                    key_marker=key_marker,
                    upload_id_marker=upload_id_marker,
                    max_uploads=1000,
                )
                for upload in getattr(result, "upload_list", ()) or ():
                    if str(getattr(upload, "key", "") or "") != ref.object_key:
                        continue
                    upload_id = str(getattr(upload, "upload_id", "") or "").strip()
                    if upload_id:
                        upload_ids.append(upload_id)
                if not getattr(result, "is_truncated", False):
                    break
                next_key = str(getattr(result, "next_key_marker", "") or "").strip()
                next_upload = str(getattr(result, "next_upload_id_marker", "") or "").strip()
                if not next_key or (next_key == key_marker and next_upload == upload_id_marker):
                    raise ObjectStorageError("OSS multipart continuation did not advance")
                key_marker = next_key
                upload_id_marker = next_upload
            return tuple(upload_ids)
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError(f"OSS multipart listing failed: {exc}") from exc

    def head(self, ref: StorageObjectRef) -> StorageObjectRef:
        try:
            params = {"versionId": ref.version_id} if ref.version_id else None
            meta = self._bucket(ref).head_object(ref.object_key, params=params)
            content_length = getattr(meta, "content_length", None)
            if content_length is None:
                raise ObjectStorageError("OSS head_object returned no content length")
            server_crc = getattr(meta, "server_crc", None)
            return StorageObjectRef(
                ref.bucket_role,
                ref.object_key,
                self._version_id(meta) or ref.version_id,
                str(getattr(meta, "etag", "") or ref.etag).strip('"'),
                int(content_length),
                str(self._metadata(meta).get("sha256") or "").strip() or None,
                crc64=str(server_crc) if server_crc is not None else None,
            )
        except Exception as exc:
            if isinstance(exc, ObjectStorageError):
                raise
            if self._is_missing_error(exc):
                raise StorageObjectNotFound("OSS head object is unavailable") from exc
            raise ObjectStorageError(f"OSS head failed: {exc}") from exc

    def delete_exact(self, ref: StorageObjectRef) -> None:
        try:
            self._bucket(ref).delete_object(
                ref.object_key,
                params={"versionId": ref.version_id} if ref.version_id else None,
            )
        except Exception as exc:
            raise ObjectStorageError(f"OSS delete failed: {exc}") from exc
