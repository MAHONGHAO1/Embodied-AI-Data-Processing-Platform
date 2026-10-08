"""Guard the retained Batch/Episode runtime from retired Task/EGO modules."""

from __future__ import annotations

import importlib
import importlib.util

import pytest

RETIRED_MODULES = (
    "data.routers.ego",
    "data.routers.task",
    "data.schemas.ego",
    "data.services.annotation_revisions",
    "data.services.behavior_ai_inputs",
    "data.services.cut_plans",
    "data.services.ego_annotation_context",
    "data.services.ego_annotation_scope",
    "data.services.ego_annotations",
    "data.services.ego_cut_index",
    "data.services.ego_derivation",
    "data.services.ego_ingest",
    "data.services.ego_offline_attribution",
    "data.services.ego_preview_manifest",
    "data.services.ego_publish",
    "data.services.ego_quality",
    "data.services.ego_review",
    "data.services.ego_source_store",
    "data.services.ego_workflow",
    "data.services.ego_workflow_projection",
    "data.services.episode_publication",
    "data.services.ego_batch_import",
    "data.services.review_decisions",
    "data.services.work_items",
    "data.services.workflow_events",
    "data.tasks.export_tasks",
    "data.tasks.media_tasks",
)


def test_active_application_and_celery_imports_only_current_runtime() -> None:
    main = importlib.import_module("data.main")
    importlib.import_module("data.celery_app")
    importlib.import_module("data.tasks.batch_workers")

    http_app = getattr(main, "http_app", main.app)
    paths = {route.path for route in http_app.routes if hasattr(route, "path")}
    assert not any(
        path == "/api/v1" + suffix or path.startswith("/api/v1" + suffix + "/")
        for path in paths
        for suffix in ("/ego", "/qrdf", "/task", "/collect", "/preprocess")
    )


@pytest.mark.parametrize("module_name", RETIRED_MODULES)
def test_retired_runtime_modules_are_not_importable(module_name: str) -> None:
    assert importlib.util.find_spec(module_name) is None


def test_historical_import_adapter_has_an_explicit_narrow_name() -> None:
    module = importlib.import_module("data.services.historical_ego_import")
    assert callable(module.scan_ego_episode_candidates)
    assert callable(module.load_ego_episode_import_source)
