"""Read-only registry API: list, detail, and version card."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from robodata.dataset_card import card_digest
from robodata.registry import register_card
from robodata.web import create_app


def _card() -> dict:
    card = {
        "schema_version": 1, "dataset_id": "dataset_demo", "version": "1",
        "created_at": "2026-11-19T00:00:00+00:00", "label": "demo",
        "format": {"name": "LeRobot", "version": "v3.0"},
        "source": {"kind": "matching_cable", "repo_id": "local"},
        "content": {"episode_count": 1, "frame_count": 5},
        "quality": {}, "conversion": {}, "artifacts": {}, "evidence": {},
        "limitations": ["synthetic"],
    }
    card["card_digest"] = card_digest(card)
    return card


def test_registry_api_lists_and_returns_card(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBODATA_BUSINESS_ROOT", str(tmp_path))
    card = _card()
    card_path = tmp_path / "card.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    register_card(card, root=tmp_path, card_path=card_path)
    client = TestClient(create_app())
    listing = client.get("/api/registry")
    assert listing.status_code == 200
    assert listing.json()["count"] == 1
    detail = client.get("/api/registry/dataset_demo")
    assert detail.status_code == 200
    assert detail.json()["dataset"]["dataset_id"] == "dataset_demo"
    version = client.get("/api/registry/dataset_demo/1")
    assert version.status_code == 200
    assert version.json()["card"]["version"] == "1"
    assert client.get("/api/registry/missing").status_code == 404
