"""Celery tasks for data-batch → data-asset publication."""

from __future__ import annotations

import logging

from data.celery_app import celery_app
from data.database import SessionLocal
from data.services.data_assets import publish_data_asset

logger = logging.getLogger("quicdata.assets")


@celery_app.task(name="quicdata.assets.publish", bind=True, max_retries=3)
def execute_asset_publish(self, batch_id: int) -> dict[str, object]:
    """Publish at most one data asset for a data batch."""
    db = SessionLocal()
    try:
        asset = publish_data_asset(db, batch_id=batch_id)
        db.commit()
        return {
            "batch_id": batch_id,
            "data_asset_id": asset.id if asset is not None else None,
        }
    except Exception as exc:  # noqa: BLE001 — celery retry boundary
        db.rollback()
        logger.exception("asset publish failed for batch_id=%s", batch_id)
        raise self.retry(exc=exc, countdown=30) from exc
    finally:
        db.close()
