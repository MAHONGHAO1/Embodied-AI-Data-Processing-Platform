"""S3-compatible object storage provider used by QuicStudio.

The provider deliberately exposes only logical bucket roles.  Callers never
choose a bucket name, and all object identity returned from the provider is
bounded to the configured environment.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

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


def _clean_etag(value: Any) -> str:
    return str(value or "").strip().strip('"')


class S3ObjectStorage:
    """Boto3 implementation for MinIO and other S3-compatible providers."""

    def __init__(
        self,
        bucket_config: StorageBucketConfig,
        *,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region_name: str = "us-east-1",
        presign_expires: int = 900,
        client: Any | None = None,
        browser_client: Any | None = None,
        browser_endpoint_url: str | None = None,
    ) -> None:
        self.bucket_config = bucket_config
        self.presign_expires = max(1, int(presign_expires))
        if client is not None:
            self.client = client
            self.browser_client = browser_client or client
            self.endpoint_url = endpoint_url or ""
            self.browser_endpoint_url = browser_endpoint_url or endpoint_url or ""
            return
        endpoint = str(endpoint_url or "").strip()
        access_key = str(access_key_id or "").strip()
        secret_key = str(secret_access_key or "").strip()
        if not endpoint or not access_key or not secret_key:
            raise StorageNotReady(
                "S3 object storage requires endpoint_url, access_key_id and secret_access_key"
            )
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise StorageNotReady("boto3 is not installed") from exc

        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )
        self.endpoint_url = endpoint
        self.browser_endpoint_url = str(browser_endpoint_url or endpoint).strip()
        self.browser_client = (
            self.client
            if self.browser_endpoint_url == endpoint
            else boto3.client(
                "s3",
                endpoint_url=self.browser_endpoint_url,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region_name,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            )
        )

    def _bucket(self, ref: StorageObjectRef) -> str:
        bucket = self.bucket_config.buckets.get(ref.bucket_role)
        if not bucket:
            raise ObjectStorageError(f"unknown storage bucket role: {ref.bucket_role}")
        if not ref.object_key.strip():
            raise ObjectStorageError("object_key must not be empty")
        if ref.object_key.startswith("/"):
            raise ObjectStorageError("object_key must not have a leading slash")
        return bucket

    @staticmethod
    def _validate_write_ref(ref: StorageObjectRef) -> None:
        if ref.version_id or ref.etag:
            raise ObjectStorageError(
                "new object writes require an incomplete identity without version_id or etag"
            )

    def _assert_absent(self, ref: StorageObjectRef) -> None:
        bucket = self._bucket(ref)
        try:
            self.client.head_object(Bucket=bucket, Key=ref.object_key)
        except ClientError as exc:
            code = str((exc.response.get("Error") or {}).get("Code") or "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return
            raise ObjectStorageError(f"S3 object existence check failed: {exc}") from exc
        except BotoCoreError as exc:
            raise ObjectStorageError(f"S3 object existence check failed: {exc}") from exc
        raise ObjectStorageError("object key already exists")

    def _call(self, operation: str, function: Any, **kwargs: Any) -> Any:
        try:
            return function(**kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError(f"S3 {operation} failed: {exc}") from exc

    @staticmethod
    def _raise_read_error(operation: str, exc: Exception) -> None:
        if isinstance(exc, ClientError):
            code = str((exc.response.get("Error") or {}).get("Code") or "")
            if code in {"404", "NoSuchKey", "NotFound", "NoSuchVersion"}:
                raise StorageObjectNotFound(f"S3 {operation} object is unavailable") from exc
            if code in {"412", "PreconditionFailed", "ConditionalRequestConflict"}:
                raise StorageObjectIntegrityError(
                    f"S3 {operation} object identity changed"
                ) from exc
        raise ObjectStorageError(f"S3 {operation} failed: {exc}") from exc

    def create_multipart(self, ref: StorageObjectRef) -> str:
        self._validate_write_ref(ref)
        bucket = self._bucket(ref)
        self._assert_absent(ref)
        metadata = {"sha256": ref.sha256} if ref.sha256 else None
        params: dict[str, Any] = {"Bucket": bucket, "Key": ref.object_key}
        if metadata:
            params["Metadata"] = metadata
        response = self._call(
            "create_multipart_upload", self.client.create_multipart_upload, **params
        )
        upload_id = str(response.get("UploadId") or "").strip()
        if not upload_id:
            raise ObjectStorageError("S3 create_multipart_upload returned no UploadId")
        return upload_id

    def sign_part(
        self,
        ref: StorageObjectRef,
        upload_id: str,
        part_number: int,
        *,
        content_md5: str | None = None,
    ) -> str:
        bucket = self._bucket(ref)
        if not str(upload_id).strip() or part_number < 1:
            raise ObjectStorageError("upload_id and positive part_number are required")
        params: dict[str, Any] = {
            "Bucket": bucket,
            "Key": ref.object_key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        }
        if content_md5:
            # Signed as a header: S3 rejects a part whose bytes do not match.
            params["ContentMD5"] = content_md5
        return str(
            self._call(
                "generate_presigned_url",
                self.browser_client.generate_presigned_url,
                ClientMethod="upload_part",
                Params=params,
                ExpiresIn=self.presign_expires,
            )
        )

    def list_parts(self, ref: StorageObjectRef, upload_id: str) -> Sequence[MultipartPart]:
        bucket = self._bucket(ref)
        if not str(upload_id).strip():
            raise ObjectStorageError("upload_id is required")
        marker: int | None = None
        result: list[MultipartPart] = []
        while True:
            params: dict[str, Any] = {
                "Bucket": bucket,
                "Key": ref.object_key,
                "UploadId": upload_id,
            }
            if marker is not None:
                params["PartNumberMarker"] = marker
            response = self._call("list_parts", self.client.list_parts, **params)
            for part in response.get("Parts") or []:
                result.append(
                    MultipartPart(
                        number=int(part["PartNumber"]),
                        etag=_clean_etag(part.get("ETag")),
                        size_bytes=int(part.get("Size") or 0),
                    )
                )
            if not response.get("IsTruncated"):
                break
            next_marker = response.get("NextPartNumberMarker")
            if next_marker is None:
                break
            marker = int(next_marker)
        return tuple(result)

    def complete_multipart(
        self,
        ref: StorageObjectRef,
        upload_id: str,
        parts: Sequence[MultipartPart],
    ) -> StorageObjectRef:
        self._validate_write_ref(ref)
        bucket = self._bucket(ref)
        if not str(upload_id).strip() or not parts:
            raise ObjectStorageError("upload_id and at least one part are required")
        completed = sorted(parts, key=lambda part: part.number)
        self._assert_absent(ref)
        self._call(
            "complete_multipart_upload",
            self.client.complete_multipart_upload,
            Bucket=bucket,
            Key=ref.object_key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [{"PartNumber": part.number, "ETag": part.etag} for part in completed]
            },
            IfNoneMatch="*",
        )
        return self.head(ref)

    def abort_multipart(self, ref: StorageObjectRef, upload_id: str) -> None:
        bucket = self._bucket(ref)
        if not str(upload_id).strip():
            raise ObjectStorageError("upload_id is required")
        self._call(
            "abort_multipart_upload",
            self.client.abort_multipart_upload,
            Bucket=bucket,
            Key=ref.object_key,
            UploadId=upload_id,
        )

    def list_multipart_upload_ids(self, ref: StorageObjectRef) -> Sequence[str]:
        """List unfinished uploads for this exact immutable key only."""
        bucket = self._bucket(ref)
        key_marker: str | None = None
        upload_id_marker: str | None = None
        upload_ids: list[str] = []
        while True:
            params: dict[str, Any] = {
                "Bucket": bucket,
                "Prefix": ref.object_key,
            }
            if key_marker:
                params["KeyMarker"] = key_marker
            if upload_id_marker:
                params["UploadIdMarker"] = upload_id_marker
            response = self._call(
                "list_multipart_uploads",
                self.client.list_multipart_uploads,
                **params,
            )
            for upload in response.get("Uploads") or []:
                if str(upload.get("Key") or "") != ref.object_key:
                    continue
                upload_id = str(upload.get("UploadId") or "").strip()
                if upload_id:
                    upload_ids.append(upload_id)
            if not response.get("IsTruncated"):
                break
            next_key = str(response.get("NextKeyMarker") or "").strip()
            next_upload = str(response.get("NextUploadIdMarker") or "").strip()
            if not next_key or (next_key == key_marker and next_upload == upload_id_marker):
                raise ObjectStorageError("S3 multipart continuation did not advance")
            key_marker = next_key
            upload_id_marker = next_upload or None
        return tuple(upload_ids)

    def head(self, ref: StorageObjectRef) -> StorageObjectRef:
        bucket = self._bucket(ref)
        params: dict[str, Any] = {"Bucket": bucket, "Key": ref.object_key}
        if ref.version_id:
            params["VersionId"] = ref.version_id
        try:
            response = self.client.head_object(**params)
        except (BotoCoreError, ClientError) as exc:
            self._raise_read_error("head_object", exc)
        metadata: Mapping[str, Any] = response.get("Metadata") or {}
        normalized_metadata = {str(key).lower(): value for key, value in metadata.items()}
        # A caller-provided digest is not evidence about the persisted object.
        # Only provider-returned metadata may populate the verified digest.
        sha256 = str(normalized_metadata.get("sha256") or "").strip() or None
        content_length = response.get("ContentLength")
        if content_length is None:
            raise ObjectStorageError("S3 head_object returned no ContentLength")
        return StorageObjectRef(
            bucket_role=ref.bucket_role,
            object_key=ref.object_key,
            version_id=str(response.get("VersionId") or ref.version_id or "") or None,
            etag=_clean_etag(response.get("ETag") or ref.etag),
            size_bytes=int(content_length),
            sha256=sha256,
        )

    def delete_exact(self, ref: StorageObjectRef) -> None:
        bucket = self._bucket(ref)
        params: dict[str, Any] = {"Bucket": bucket, "Key": ref.object_key}
        if ref.version_id:
            params["VersionId"] = ref.version_id
        self._call("delete_object", self.client.delete_object, **params)

    def _identity_params(self, ref: StorageObjectRef) -> dict[str, Any]:
        if ref.version_id:
            return {"VersionId": ref.version_id}
        if ref.etag:
            return {"IfMatch": ref.etag}
        raise ObjectStorageError("completed source requires version_id or etag")

    def download_file(self, ref: StorageObjectRef, destination: str) -> StorageObjectRef:
        bucket = self._bucket(ref)
        params = {"Bucket": bucket, "Key": ref.object_key, **self._identity_params(ref)}
        try:
            response = self.client.get_object(**params)
        except (BotoCoreError, ClientError) as exc:
            self._raise_read_error("get_object", exc)
        body = response.get("Body")
        if body is None:
            raise ObjectStorageError("S3 get_object returned no body")
        try:
            response_etag = _clean_etag(response.get("ETag"))
            response_version = str(response.get("VersionId") or "") or None
            if ref.version_id and response_version != ref.version_id:
                raise StorageObjectIntegrityError("object version changed during download")
            if not ref.version_id and response_etag != _clean_etag(ref.etag):
                raise StorageObjectIntegrityError("object etag changed during download")
            parent = os.path.dirname(os.path.abspath(destination)) or "."
            os.makedirs(parent, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix=".quic-download-", dir=parent)
            try:
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "wb") as target:
                    chunks = (
                        body.iter_chunks(chunk_size=1024 * 1024)
                        if hasattr(body, "iter_chunks")
                        else iter(lambda: body.read(1024 * 1024), b"")
                    )
                    for chunk in chunks:
                        if not chunk:
                            continue
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                calculated = digest.hexdigest()
                expected_size = int(ref.size_bytes)
                # ``0`` is a valid authoritative size.  Do not treat it as an
                # absent/legacy value: completed references must be checked too.
                if size != expected_size:
                    raise StorageObjectIntegrityError(
                        "downloaded object size does not match source identity"
                    )
                if ref.sha256 and calculated != ref.sha256:
                    raise StorageObjectIntegrityError(
                        "downloaded object sha256 does not match source identity"
                    )
                os.replace(temp_path, destination)
            except Exception:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
                raise
            return StorageObjectRef(
                ref.bucket_role,
                ref.object_key,
                response_version or ref.version_id,
                response_etag or ref.etag,
                size,
                calculated,
            )
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()

    def list_prefix(
        self, bucket_role: str, prefix: str, *, continuation_token: str | None, max_keys: int
    ) -> StorageListPage:
        if not isinstance(prefix, str) or not prefix or prefix.startswith("/") or "\\" in prefix:
            raise ObjectStorageError("prefix is invalid")
        if not isinstance(max_keys, int) or isinstance(max_keys, bool) or not 1 <= max_keys <= 1000:
            raise ObjectStorageError("prefix page size is invalid")
        ref = StorageObjectRef(bucket_role, prefix.rstrip("/") + "/probe", None, "", 0)
        bucket = self._bucket(ref)
        params: dict[str, Any] = {
            "Bucket": bucket,
            "Prefix": prefix.rstrip("/") + "/",
            "MaxKeys": max_keys,
        }
        if continuation_token:
            params["ContinuationToken"] = continuation_token
        response = self._call("list_objects_v2", self.client.list_objects_v2, **params)
        objects = []
        for item in response.get("Contents") or []:
            key = str(item.get("Key") or "")
            if not key.startswith(params["Prefix"]):
                continue
            objects.append(
                StorageObjectRef(
                    bucket_role,
                    key,
                    str(item.get("VersionId") or "") or None,
                    _clean_etag(item.get("ETag")),
                    int(item.get("Size") or 0),
                    None,
                )
            )
        token = str(response.get("NextContinuationToken") or "") or None
        if response.get("IsTruncated") and (not token or token == continuation_token):
            raise ObjectStorageError("S3 prefix continuation did not advance")
        return StorageListPage(tuple(objects), token)

    def sign_get(self, ref: StorageObjectRef, *, expires: int = 900) -> str:
        bucket = self._bucket(ref)
        params = {"Bucket": bucket, "Key": ref.object_key, **self._identity_params(ref)}
        return str(
            self._call(
                "generate_presigned_url",
                self.browser_client.generate_presigned_url,
                ClientMethod="get_object",
                Params=params,
                ExpiresIn=max(1, int(expires)),
            )
        )

    def object_uri(self, ref: StorageObjectRef) -> str:
        bucket = self._bucket(ref)
        # Provider-neutral object identity is deliberately not a fetch URL.
        # Signed browser access is exposed separately by sign_get().
        return f"oss://{bucket}/{ref.object_key}"

    def healthcheck(self) -> None:
        """Probe the configured endpoint and raw bucket without exposing credentials."""
        for role, bucket in self.bucket_config.buckets.items():
            if not bucket:
                raise StorageNotReady(f"{role} storage bucket is not configured")
            self._call("head_bucket", self.client.head_bucket, Bucket=bucket)

    def put_worker_object(self, ref: StorageObjectRef, source_path: str) -> StorageObjectRef:
        self._validate_write_ref(ref)
        bucket = self._bucket(ref)
        digest = hashlib.sha256()
        size = 0
        with open(source_path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        calculated = digest.hexdigest()
        with open(source_path, "rb") as source:
            self._call(
                "put_object",
                self.client.put_object,
                Bucket=bucket,
                Key=ref.object_key,
                Body=source,
                ContentLength=size,
                Metadata={"sha256": calculated},
                IfNoneMatch="*",
            )
        persisted = self.head(ref)
        # Size zero is a real object size and must be verified just like any
        # other size.
        if persisted.size_bytes != size:
            raise ObjectStorageError("persisted worker object size does not match source")
        if persisted.sha256 != calculated:
            raise ObjectStorageError("persisted worker object sha256 does not match source")
        return StorageObjectRef(
            persisted.bucket_role,
            persisted.object_key,
            persisted.version_id,
            persisted.etag,
            persisted.size_bytes,
            calculated,
        )
