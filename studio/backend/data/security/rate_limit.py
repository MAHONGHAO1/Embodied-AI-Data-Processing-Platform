"""Redis-backed sliding-window limits for login brute-force protection."""

from __future__ import annotations

import time
from uuid import uuid4

from data.infra.redis_client import redis_service


def _redis_count(key: str, window_seconds: int) -> int:
    with redis_service.operation() as client:
        now = time.time()
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zcard(key)
        pipe.expire(key, window_seconds + 60)
        _, count, _ = pipe.execute()
        return int(count or 0)


def _redis_hit(key: str, window_seconds: int) -> int:
    with redis_service.operation() as client:
        now = time.time()
        member = f"{now}:{uuid4().hex}"
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zadd(key, {member: now})
        pipe.zcard(key)
        pipe.expire(key, window_seconds + 60)
        _, _, count, _ = pipe.execute()
        return int(count or 0)


def get_hit_count(bucket: str, *, window_seconds: int) -> int:
    """Read-only check for hit count in the current window."""
    key = f"quicdata:rl:{bucket}"
    return _redis_count(key, window_seconds)


def record_hit(bucket: str, *, window_seconds: int) -> int:
    """Record a hit and return the hit count within the window."""
    key = f"quicdata:rl:{bucket}"
    return _redis_hit(key, window_seconds)


def is_rate_limited(bucket: str, *, threshold: int, window_seconds: int) -> bool:
    if threshold <= 0:
        return False
    return get_hit_count(bucket, window_seconds=window_seconds) >= threshold
