"""Export file path resolution (compatible with historical Windows absolute paths)."""

import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from data.config import settings
from data.database import ExportJob
from data.utils.storage_paths import resolve_file_for_serve


def resolve_export_file(job: ExportJob) -> Path | None:
    """Resolve local export zip by task ID and download_url."""
    if job.download_url and "path=" in job.download_url:
        raw = job.download_url.split("path=", 1)[1]
        raw = unquote(raw.split("&", 1)[0])
        resolved = resolve_file_for_serve(raw)
        if resolved:
            return resolved

    export_dir = Path(settings.storage_root) / "exports"
    if not export_dir.is_dir():
        return None

    for f in sorted(export_dir.glob(f"export_{job.id}_*.zip")):
        if f.is_file():
            return f.resolve()

    for d in sorted(export_dir.glob(f"export_{job.id}_*")):
        if d.is_dir():
            nested = d / "lerobot_export.zip"
            if nested.is_file():
                return nested.resolve()

    return None


def list_export_output_files(job: ExportJob, *, max_files: int = 200) -> list[dict[str, Any]]:
    """List files in export artifact (zip entries or directory files)."""
    file_path = resolve_export_file(job)
    if not file_path:
        return []

    files: list[dict[str, Any]] = []
    if file_path.is_dir():
        for item in sorted(file_path.rglob("*")):
            if not item.is_file():
                continue
            files.append(
                {
                    "relative_path": item.relative_to(file_path).as_posix(),
                    "size_bytes": item.stat().st_size,
                }
            )
            if len(files) >= max_files:
                break
        return files

    if file_path.suffix.lower() == ".zip" and zipfile.is_zipfile(file_path):
        with zipfile.ZipFile(file_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                files.append(
                    {
                        "relative_path": info.filename,
                        "size_bytes": info.file_size,
                    }
                )
                if len(files) >= max_files:
                    break
        return files

    return [
        {
            "relative_path": file_path.name,
            "size_bytes": file_path.stat().st_size,
        }
    ]
