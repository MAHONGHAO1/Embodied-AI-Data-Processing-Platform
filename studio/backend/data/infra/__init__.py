"""Infrastructure clients (PostgreSQL via SQLAlchemy, Redis, Celery)."""

from data.infra.redis_client import redis_service

__all__ = ["redis_service"]
