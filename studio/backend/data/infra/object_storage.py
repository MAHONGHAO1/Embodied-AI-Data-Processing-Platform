"""Provider-neutral object storage types used by the QuicStudio path.

The legacy ``oss_client`` module remains available while callers migrate. New
code must pass an immutable object reference and a logical bucket role instead
of accepting arbitrary bucket names or user supplied object URLs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

BucketRole = Literal["raw", "process", "export"]


class ObjectStorageError(RuntimeError):
    """Base error for provider failures exposed to the business layer."""


class StorageObjectNotFound(ObjectStorageError):
    """The immutable provider object no longer exists."""


class StorageObjectIntegrityError(ObjectStorageError):
    """The provider object no longer matches its frozen identity."""


class StorageNotReady(ObjectStorageError):
    """The configured provider is unavailable or has no usable credentials."""


@dataclass(frozen=True)
class StorageObjectRef:
    """Immutable identity captured after a provider HEAD/complete operation."""

    bucket_role: BucketRole
    object_key: str
    version_id: str | None
    etag: str
    size_bytes: int
    sha256: str | None = None
    # Optional provider integrity evidence; not part of immutable object identity.
    crc64: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class MultipartPart:
    """Provider-confirmed multipart part metadata."""

    number: int
    etag: str
    size_bytes: int


@dataclass(frozen=True)
class StorageListPage:
    """One bounded page of objects under a provider prefix."""

    objects: Sequence[StorageObjectRef]
    next_token: str | None


class ObjectStorageProvider(Protocol):
    """Minimal provider contract; implementations own signing and error mapping."""

    def create_multipart(self, ref: StorageObjectRef) -> str: ...

    def sign_part(
        self,
        ref: StorageObjectRef,
        upload_id: str,
        part_number: int,
        *,
        content_md5: str | None = None,
    ) -> str: ...

    def list_parts(self, ref: StorageObjectRef, upload_id: str) -> Sequence[MultipartPart]: ...

    def complete_multipart(
        self,
        ref: StorageObjectRef,
        upload_id: str,
        parts: Sequence[MultipartPart],
    ) -> StorageObjectRef: ...

    def abort_multipart(self, ref: StorageObjectRef, upload_id: str) -> None: ...

    def list_multipart_upload_ids(self, ref: StorageObjectRef) -> Sequence[str]: ...

    def head(self, ref: StorageObjectRef) -> StorageObjectRef: ...

    def put_worker_object(self, ref: StorageObjectRef, source_path: str) -> StorageObjectRef: ...

    def delete_exact(self, ref: StorageObjectRef) -> None: ...

    def download_file(self, ref: StorageObjectRef, destination: str) -> StorageObjectRef: ...

    def list_prefix(
        self, bucket_role: BucketRole, prefix: str, *, continuation_token: str | None, max_keys: int
    ) -> StorageListPage: ...

    def sign_get(self, ref: StorageObjectRef, *, expires: int = 900) -> str: ...

    def object_uri(self, ref: StorageObjectRef) -> str: ...

    def healthcheck(self) -> None: ...
