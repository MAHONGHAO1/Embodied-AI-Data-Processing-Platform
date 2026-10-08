"""Celery application: long-running tasks such as preprocessing and export."""

from celery import Celery
from celery.signals import worker_process_init
from kombu import Exchange, Queue

from data.config import settings

job_exchange = Exchange("general", type="direct")

celery_app = Celery(
    "quicdata",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "data.tasks.batch_workers",
        "data.tasks.governance_tasks",
        "data.tasks.asset_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="control",
    task_default_exchange=job_exchange.name,
    task_default_exchange_type=job_exchange.type,
    task_default_routing_key="general",
    task_queues=(
        Queue("ingest", exchange=job_exchange, routing_key="ingest"),
        Queue("media", exchange=job_exchange, routing_key="media"),
        Queue("publish", exchange=job_exchange, routing_key="publish"),
        Queue("control", exchange=job_exchange, routing_key="control"),
        Queue("analytics", exchange=job_exchange, routing_key="analytics"),
        Queue("general", exchange=job_exchange, routing_key="general"),
        Queue("export", exchange=job_exchange, routing_key="export"),
        Queue("ai", exchange=job_exchange, routing_key="ai"),
        Queue("governance", exchange=job_exchange, routing_key="governance"),
    ),
    task_routes={
        "quicdata.batch.execute": {"queue": "media"},
        "quicdata.batch.recover-expired": {"queue": "control"},
        "quicdata.dashboard.etl": {"queue": "analytics"},
        "quicdata.runtime.retention": {"queue": "control"},
        "quicdata.governance.execute": {"queue": "governance"},
        "quicdata.assets.publish": {"queue": "publish"},
    },
    beat_schedule={
        "recover-expired-batch-jobs": {
            "task": "quicdata.batch.recover-expired",
            "schedule": 30.0,
        },
        "dashboard-etl-5m": {
            "task": "quicdata.dashboard.etl",
            "schedule": 300.0,
        },
        "runtime-retention-hourly": {
            "task": "quicdata.runtime.retention",
            "schedule": 3600.0,
        },
    },
    broker_connection_retry_on_startup=False,
    broker_transport_options={"visibility_timeout": 3600},
)


@worker_process_init.connect
def verify_worker_schema(**_kwargs) -> None:
    """Do not let a worker process jobs against an un-migrated database."""
    from data.runtime import assert_schema_current
    from data.services.job_queue_limits import validate_queue_slot_config

    assert_schema_current()
    validate_queue_slot_config()
