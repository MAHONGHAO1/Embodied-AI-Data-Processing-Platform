"""Safe local housekeeping: scan, protect, and remove abandoned run directories."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from .layout import Layout

TERMINAL = frozenset({"completed", "completed_with_errors", "failed", "interrupted"})
PROTECTED_KEYS = ("path", "root", "artifact", "archive", "zip", "card", "output")


def _walk_paths(value, found: set[Path]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and any(word in str(key).lower() for word in PROTECTED_KEYS):
                if item and not item.startswith(("http://", "https://", "pinned://")):
                    found.add(Path(item).expanduser())
            _walk_paths(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_paths(item, found)


def protected_paths(layout: Layout) -> set[Path]:
    """Every path mentioned by a batch or registry row is protected from GC."""
    found: set[Path] = set()
    if layout.business.is_dir():
        for path in layout.business.glob("batch_*.json"):
            try:
                _walk_paths(json.loads(path.read_text(encoding="utf-8")), found)
            except (OSError, ValueError):
                found.add(path)
    if layout.registry.is_file():
        try:
            from .registry import list_datasets
            _walk_paths(list_datasets(layout.business), found)
        except (OSError, ValueError):
            found.add(layout.registry)
    resolved = set()
    for path in found:
        try:
            resolved.add(path.resolve())
        except OSError:
            continue
    return resolved


def _size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _is_protected(path: Path, protected: set[Path]) -> bool:
    try:
        current = path.resolve()
    except OSError:
        return True
    for item in protected:
        if current == item or current.is_relative_to(item) or item.is_relative_to(current):
            return True
    return False


def scan(layout: Layout | None = None, *, keep_last: int = 5) -> dict:
    layout = (layout or Layout.current()).ensure()
    protected = protected_paths(layout)
    candidates = []
    kept = []
    if not layout.runs.is_dir():
        return {"candidates": [], "kept": [], "protected_count": len(protected),
                "root": str(layout.runs)}
    for directory in sorted(layout.runs.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        state_path = directory / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        status = str(state.get("status") or "")
        item = {"run_id": directory.name, "path": str(directory), "status": status,
                "bytes": 0, "batch_id": state.get("batch_id"),
                "protected": _is_protected(directory, protected)}
        if item["protected"]:
            kept.append({**item, "reason": "被批次/登记表引用"})
        elif status not in TERMINAL:
            kept.append({**item, "reason": "运行未结束"})
        elif status == "completed":
            kept.append({**item, "reason": "完成记录默认保留"})
        else:
            item["bytes"] = _size(directory)
            candidates.append({**item, "reason": "失败/中断且未被引用"})
    candidates.sort(key=lambda item: item["run_id"], reverse=True)
    removable = candidates[max(0, int(keep_last)):]
    for item in candidates[:max(0, int(keep_last))]:
        kept.append({**item, "reason": f"保留最近 {keep_last} 个候选"})
    return {"candidates": removable, "kept": kept, "protected_count": len(protected),
            "root": str(layout.runs)}


def apply(layout: Layout | None = None, *, keep_last: int = 5) -> dict:
    layout = (layout or Layout.current()).ensure()
    plan = scan(layout, keep_last=keep_last)
    deleted, freed, refused = [], 0, []
    root = layout.runs.resolve()
    for item in plan["candidates"]:
        path = Path(item["path"])
        try:
            resolved = path.resolve()
        except OSError:
            refused.append({**item, "reason": "路径无法解析"})
            continue
        if resolved.parent != root or resolved.is_symlink():
            refused.append({**item, "reason": "路径不在运行根目录"})
            continue
        if not (resolved / "state.json").is_file() or _is_protected(resolved, protected_paths(layout)):
            refused.append({**item, "reason": "复查时发现已被引用"})
            continue
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            refused.append({**item, "reason": f"删除失败：{exc}"})
            continue
        deleted.append(item)
        freed += int(item["bytes"])
    return {"deleted": deleted, "refused": refused, "freed_bytes": freed,
            "kept": plan["kept"], "root": plan["root"]}
