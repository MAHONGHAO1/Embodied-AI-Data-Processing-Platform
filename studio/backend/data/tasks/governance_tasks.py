"""Celery tasks for data-batch governance runs."""

from __future__ import annotations

import logging

from data.celery_app import celery_app
from data.database import SessionLocal
from data.services.governance_runs import run_governance

logger = logging.getLogger("quicdata.governance")


@celery_app.task(name="quicdata.governance.execute", bind=True, max_retries=3)
def execute_governance_run(self, batch_id: int) -> dict[str, object]:
    """Run governance stages for a data batch in a worker process."""
    db = SessionLocal()
    try:
        batch = run_governance(db, batch_id=batch_id, sync=True)
        db.commit()
        status = batch.governance_run.status if batch.governance_run else batch.status
        return {"batch_id": batch_id, "status": status}
    except Exception as exc:  # noqa: BLE001 — celery retry boundary
        db.rollback()
        logger.exception("governance run failed for batch_id=%s", batch_id)
        raise self.retry(exc=exc, countdown=30) from exc
    finally:
        db.close()
