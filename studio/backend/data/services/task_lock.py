"""Task claim locks backed by required Redis state."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from data.database import Task
from data.infra.redis_client import redis_service

DEFAULT_LOCK_SECONDS = 3600


class TaskLockService:
    def claim(
        self, db: Session, task: Task, user_id: str, expire_seconds: int = DEFAULT_LOCK_SECONDS
    ) -> dict:
        now = datetime.utcnow()
        expire_at = now + timedelta(seconds=expire_seconds)
        lock_key = str(task.id)

        token = uuid.uuid4().hex
        acquired = redis_service.acquire_lock(lock_key, user_id, expire_seconds)
        if not acquired:
            holder = redis_service.get_lock_holder(lock_key) or "unknown"
            ttl = redis_service.lock_ttl(lock_key)
            raise ValueError(f"任务已被 {holder} 领取，剩余 {ttl}s")
        task.claimed_by = user_id
        task.claim_token = token
        task.claim_expire_at = expire_at
        db.commit()
        return {"success": True, "lock_expire": expire_seconds, "lock_token": token}

    def release(self, db: Session, task: Task, user_id: str) -> None:
        if task.claimed_by and task.claimed_by != user_id:
            raise ValueError("无权释放他人领取的任务")
        redis_service.release_lock(str(task.id))
        task.claimed_by = None
        task.claim_token = None
        task.claim_expire_at = None
        db.commit()

    def require_claim(self, task: Task, user_id: str) -> None:
        holder = redis_service.get_lock_holder(str(task.id))
        if holder and holder != user_id:
            raise ValueError("请先领取任务后再操作")


task_lock_service = TaskLockService()
