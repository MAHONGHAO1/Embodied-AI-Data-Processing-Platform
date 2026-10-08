"""Retired routes/jobs stay off while the current intake/catalog paths remain."""

import pytest

from data.main import http_app as app
from data.services.job_runs import RECOVERABLE_JOB_KINDS, create_or_get_job_in_transaction
from data.services.legacy_retirement import RETIRED_JOB_KINDS
from data.tasks.batch_workers import _HANDLERS


def test_retired_routes_and_handlers_are_not_exposed():
    # FastAPI keeps included routers in the OpenAPI route graph on the
    # current runtime, while ``app.routes`` also contains mount wrappers.
    routes = set(app.openapi()["paths"])
    assert not any("/workspace/task-set/" in r for r in routes)
    assert not any(r.startswith("/api/v1/native-lerobot-datasets") for r in routes)
    assert "/api/v1/collection-projects" in routes
    assert "/api/v1/fetch-manifests" in routes
    assert not RETIRED_JOB_KINDS.intersection(_HANDLERS)
    assert not RETIRED_JOB_KINDS.intersection(RECOVERABLE_JOB_KINDS)
    assert {
        "collection_upload_parse",
        "collection_upload_admission",
        "native_lerobot_direct_validate",
        "catalog_export",
    }.issubset(_HANDLERS)


@pytest.mark.parametrize("kind", sorted(RETIRED_JOB_KINDS))
def test_retired_jobs_cannot_be_created(db_session, kind):
    with pytest.raises(ValueError, match="legacy_workflow_retired"):
        create_or_get_job_in_transaction(
            db_session,
            kind=kind,
            resource_type="platform",
            resource_id="retired",
            idempotency_key=f"retired:{kind}",
            queue="ingest",
        )
