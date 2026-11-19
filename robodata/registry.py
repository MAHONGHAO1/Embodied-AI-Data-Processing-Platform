"""Local dataset registry (SQLite).

The registry is an index, not a second source of truth. Every row points at a
verified ``dataset_card.json``; the card digest is what makes the row
re-checkable. Registering the same dataset/version with a different digest is
a conflict, not a silent overwrite.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .dataset_card import load_card, verify_card


class RegistryConflict(ValueError):
    """The same dataset/version already exists with different content."""


def _path(root=None) -> Path:
    if root is None:
        from .business import get_store
        root = get_store().root
    return Path(root) / "registry.sqlite3"


def _connect(root=None) -> sqlite3.Connection:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS datasets (
        dataset_id TEXT PRIMARY KEY, label TEXT, created_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS versions (
        dataset_id TEXT NOT NULL, version TEXT NOT NULL, card_digest TEXT NOT NULL,
        card_path TEXT, zip_path TEXT, batch_id TEXT, output_id TEXT,
        content_version INTEGER, source_kind TEXT, format_version TEXT,
        artifact_path TEXT, created_at TEXT NOT NULL,
        PRIMARY KEY (dataset_id, version))""")
    return connection


def register_card(card: dict, *, root=None, card_path: Path | None = None,
                  zip_path: Path | None = None, artifact_path: Path | None = None) -> dict:
    valid, message = verify_card(card)
    if not valid:
        raise ValueError(f"拒绝登记未通过自校验的数据卡：{message}")
    dataset_id = str(card["dataset_id"])
    version = str(card["version"])
    with _connect(root) as connection:
        dataset = connection.execute("SELECT * FROM datasets WHERE dataset_id = ?",
                                     (dataset_id,)).fetchone()
        if dataset is None:
            connection.execute("INSERT INTO datasets (dataset_id, label, created_at) VALUES (?, ?, ?)",
                               (dataset_id, card.get("label"), card["created_at"]))
        existing = connection.execute(
            "SELECT * FROM versions WHERE dataset_id = ? AND version = ?",
            (dataset_id, version)).fetchone()
        if existing is not None:
            if existing["card_digest"] != card["card_digest"]:
                raise RegistryConflict(
                    f"数据集 {dataset_id} 版本 {version} 已存在且内容不同；请换新版本，不能覆盖已登记内容")
            return {"status": "unchanged", "dataset_id": dataset_id, "version": version,
                    "card_digest": card["card_digest"], "row": dict(existing)}
        connection.execute("""INSERT INTO versions (
            dataset_id, version, card_digest, card_path, zip_path, batch_id, output_id,
            content_version, source_kind, format_version, artifact_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            dataset_id, version, card["card_digest"], str(card_path) if card_path else None,
            str(zip_path) if zip_path else None, card.get("batch_id"), card.get("output_id"),
            card.get("content_version"),
            (card.get("source") or {}).get("kind"), (card.get("format") or {}).get("version"),
            str(artifact_path) if artifact_path else None, card["created_at"]))
        connection.commit()
    return {"status": "registered", "dataset_id": dataset_id, "version": version,
            "card_digest": card["card_digest"]}


def register_card_file(path: Path, *, root=None, zip_path: Path | None = None,
                       artifact_path: Path | None = None) -> dict:
    card = load_card(Path(path))
    return register_card(card, root=root, card_path=Path(path), zip_path=zip_path,
                         artifact_path=artifact_path)


def list_datasets(root=None) -> dict:
    with _connect(root) as connection:
        datasets = [dict(row) for row in connection.execute(
            "SELECT * FROM datasets ORDER BY created_at DESC")]
        versions = [dict(row) for row in connection.execute(
            "SELECT * FROM versions ORDER BY created_at DESC")]
    for dataset in datasets:
        dataset["versions"] = [item for item in versions
                               if item["dataset_id"] == dataset["dataset_id"]]
    return {"datasets": datasets, "count": len(datasets)}


def show(dataset_id: str, version: str | None = None, root=None) -> dict:
    with _connect(root) as connection:
        dataset = connection.execute("SELECT * FROM datasets WHERE dataset_id = ?",
                                     (str(dataset_id),)).fetchone()
        if dataset is None:
            raise KeyError(f"未登记的数据集：{dataset_id}")
        query = "SELECT * FROM versions WHERE dataset_id = ?"
        params = [str(dataset_id)]
        if version is not None:
            query += " AND version = ?"
            params.append(str(version))
        rows = [dict(row) for row in connection.execute(query + " ORDER BY created_at DESC", params)]
    if not rows:
        raise KeyError(f"数据集 {dataset_id} 没有版本 {version}")
    for row in rows:
        if row.get("card_path") and Path(row["card_path"]).is_file():
            row["card"] = json.loads(Path(row["card_path"]).read_text(encoding="utf-8"))
    return {"dataset": dict(dataset), "versions": rows}


def verify_card_path(path: Path) -> dict:
    card = load_card(Path(path))
    valid, message = verify_card(card)
    return {"valid": valid, "message": message, "dataset_id": card.get("dataset_id"),
            "version": card.get("version"), "card_digest": card.get("card_digest")}
