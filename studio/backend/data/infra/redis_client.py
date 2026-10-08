"""Redis: distributed locks, multipart upload cache, task status cache."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from data.config import settings

logger = logging.getLogger(__name__)

UPLOAD_CHUNK_PREFIX = "quicdata:upload:chunks:"
UPLOAD_STATUS_PREFIX = "quicdata:upload:status:"
TASK_STATUS_PREFIX = "quicdata:task:status:"
TASK_LOCK_PREFIX = "quicdata:task:lock:"
ANNOTATE_RF_PREFIX = "quicdata:annotate:rf:"
PREPROCESS_CANCEL_PREFIX = "quicdata:preprocess:cancel:"
PREPROCESS_JOB_PREFIX = "quicdata:preprocess:job:"
DEFAULT_UPLOAD_TTL = 86400 * 7  # 7 days
DEFAULT_STATUS_TTL = 86400


class RedisUnavailableError(RuntimeError):
    """Stable fail-closed error for required Redis operations."""

    def __init__(self) -> None:
        super().__init__("redis_unavailable")


class RedisService:
    def __init__(self) -> None:
        self._client = None
        self._connect_lock = threading.RLock()

    def connect_required(self):
        with self._connect_lock:
            if self._client is not None:
                return self._client
            if not str(settings.redis_url or "").strip():
                raise RedisUnavailableError()
            try:
                import redis

                client = redis.from_url(settings.redis_url, decode_responses=True)
                client.ping()
            except Exception as exc:
                self._client = None
                logger.error(
                    "Redis connection unavailable", extra={"error_type": type(exc).__name__}
                )
                raise RedisUnavailableError() from exc
            self._client = client
            return client

    def disconnect(self) -> None:
        with self._connect_lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                logger.warning(
                    "Redis client close failed", extra={"error_type": type(exc).__name__}
                )

    @contextmanager
    def operation(self) -> Iterator[Any]:
        client = self.connect_required()
        try:
            yield client
        except RedisUnavailableError:
            raise
        except Exception as exc:
            try:
                from redis.exceptions import RedisError
            except ImportError:
                RedisError = OSError  # type: ignore[assignment,misc]
            if isinstance(exc, (RedisError, OSError, TimeoutError)):
                self.disconnect()
                logger.error(
                    "Redis operation unavailable", extra={"error_type": type(exc).__name__}
                )
                raise RedisUnavailableError() from exc
            raise

    def ping_required(self) -> None:
        with self.operation() as client:
            client.ping()

    @property
    def client(self):
        return self.connect_required()

    # --- Task Lock ---
    def acquire_lock(self, key: str, holder: str, expire_seconds: int) -> bool:
        with self.operation() as client:
            lock_key = f"{TASK_LOCK_PREFIX}{key}"
            current = client.get(lock_key)
            if current == holder:
                client.set(lock_key, holder, ex=expire_seconds)
                return True
            return bool(client.set(lock_key, holder, nx=True, ex=expire_seconds))

    def get_lock_holder(self, key: str) -> str | None:
        with self.operation() as client:
            return client.get(f"{TASK_LOCK_PREFIX}{key}")

    def lock_ttl(self, key: str) -> int:
        with self.operation() as client:
            return int(client.ttl(f"{TASK_LOCK_PREFIX}{key}"))

    def release_lock(self, key: str) -> None:
        with self.operation() as client:
            client.delete(f"{TASK_LOCK_PREFIX}{key}")

    # --- Multipart Upload Cache ---
    def add_upload_chunk(self, upload_id: str, chunk_index: int, total_chunks: int) -> list[int]:
        with self.operation() as client:
            key = f"{UPLOAD_CHUNK_PREFIX}{upload_id}"
            pipe = client.pipeline()
            pipe.sadd(key, str(chunk_index))
            pipe.expire(key, DEFAULT_UPLOAD_TTL)
            pipe.smembers(key)
            _, _, members = pipe.execute()
            uploaded = sorted(int(x) for x in members)
            client.set(
                f"{UPLOAD_STATUS_PREFIX}{upload_id}",
                json.dumps({"uploaded_chunks": uploaded, "total_chunks": total_chunks}),
                ex=DEFAULT_UPLOAD_TTL,
            )
            return uploaded

    def get_upload_chunks(self, upload_id: str) -> list[int]:
        with self.operation() as client:
            members = client.smembers(f"{UPLOAD_CHUNK_PREFIX}{upload_id}")
            return sorted(int(x) for x in members)

    def set_upload_status(self, upload_id: str, data: dict[str, Any]) -> None:
        with self.operation() as client:
            client.set(
                f"{UPLOAD_STATUS_PREFIX}{upload_id}", json.dumps(data), ex=DEFAULT_UPLOAD_TTL
            )

    def get_upload_status(self, upload_id: str) -> dict[str, Any] | None:
        with self.operation() as client:
            raw = client.get(f"{UPLOAD_STATUS_PREFIX}{upload_id}")
            return json.loads(raw) if raw else None

    def clear_upload_cache(self, upload_id: str) -> None:
        with self.operation() as client:
            client.delete(f"{UPLOAD_CHUNK_PREFIX}{upload_id}", f"{UPLOAD_STATUS_PREFIX}{upload_id}")

    # --- Task Status Cache ---
    def set_task_status(
        self, task_id: int, payload: dict[str, Any], ttl: int = DEFAULT_STATUS_TTL
    ) -> None:
        with self.operation() as client:
            client.set(f"{TASK_STATUS_PREFIX}{task_id}", json.dumps(payload), ex=ttl)

    def get_task_status(self, task_id: int) -> dict[str, Any] | None:
        with self.operation() as client:
            raw = client.get(f"{TASK_STATUS_PREFIX}{task_id}")
            return json.loads(raw) if raw else None

    # --- Preprocessing Task Cancellation ---
    def set_preprocess_cancel(self, task_id: int, cancelled: bool = True) -> None:
        with self.operation() as client:
            key = f"{PREPROCESS_CANCEL_PREFIX}{task_id}"
            if cancelled:
                client.set(key, "1", ex=DEFAULT_STATUS_TTL)
            else:
                client.delete(key)

    def is_preprocess_cancelled(self, task_id: int) -> bool:
        with self.operation() as client:
            return bool(client.get(f"{PREPROCESS_CANCEL_PREFIX}{task_id}"))

    def set_preprocess_job(self, task_id: int, celery_id: str) -> None:
        with self.operation() as client:
            client.set(f"{PREPROCESS_JOB_PREFIX}{task_id}", celery_id, ex=DEFAULT_STATUS_TTL)

    def get_preprocess_job(self, task_id: int) -> str | None:
        with self.operation() as client:
            return client.get(f"{PREPROCESS_JOB_PREFIX}{task_id}")

    def clear_preprocess_job(self, task_id: int) -> None:
        with self.operation() as client:
            client.delete(
                f"{PREPROCESS_JOB_PREFIX}{task_id}", f"{PREPROCESS_CANCEL_PREFIX}{task_id}"
            )

    # --- Annotation Region Frame ID Session ---
    def register_region_frame_id(
        self,
        task_id: int,
        rf_id: str,
        *,
        start_frame: int,
        end_frame: int,
        ttl: int = DEFAULT_STATUS_TTL,
    ) -> None:
        with self.operation() as client:
            key = f"{ANNOTATE_RF_PREFIX}{task_id}"
            client.hset(
                key,
                rf_id,
                json.dumps({"start_frame": start_frame, "end_frame": end_frame}),
            )
            client.expire(key, ttl)

    def get_region_frame_ids(self, task_id: int) -> dict[str, dict[str, int]]:
        with self.operation() as client:
            raw = client.hgetall(f"{ANNOTATE_RF_PREFIX}{task_id}")
        result: dict[str, dict[str, int]] = {}
        for rf_id, payload in raw.items():
            try:
                result[rf_id] = json.loads(payload)
            except json.JSONDecodeError:
                continue
        return result

    def clear_region_frame_session(self, task_id: int) -> None:
        with self.operation() as client:
            client.delete(f"{ANNOTATE_RF_PREFIX}{task_id}")


redis_service = RedisService()
