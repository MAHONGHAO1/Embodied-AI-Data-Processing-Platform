"""Idempotently rebuild data package dashboard facts from durable rows."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from data.models.data_package import DataPackage
from data.services.package_dashboard_facts import recompute_package_dashboard_facts

_HOUR_TOLERANCE = Decimal("0.005")


def backfill(db: Session, *, batch_size: int = 200) -> dict[str, Any]:
    last_id = 0
    count = 0
    mismatches: list[dict[str, Any]] = []
    while True:
        packages = (
            db.query(DataPackage)
            .filter(DataPackage.id > last_id)
            .order_by(DataPackage.id.asc())
            .limit(batch_size)
            .all()
        )
        if not packages:
            break
        for package in packages:
            recompute_package_dashboard_facts(db, package)
            count += 1
            new_seconds = package.intake_valid_duration_s
            old_hours = package.intake_valid_duration_hours
            if new_seconds is not None and old_hours is not None:
                delta = abs(Decimal(new_seconds) / Decimal(3600) - Decimal(old_hours))
                if delta > _HOUR_TOLERANCE:
                    mismatches.append(
                        {
                            "data_package_id": package.id,
                            "intake_valid_duration_s": str(new_seconds),
                            "intake_valid_duration_hours": str(old_hours),
                        }
                    )
            last_id = package.id
        db.commit()
    return {"packages": count, "mismatches": mismatches}


def main() -> None:
    from data.database import SessionLocal

    db = SessionLocal()
    try:
        print(json.dumps(backfill(db), ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
