"""One-command, reproducible SO-100 acceptance pipeline.

The script takes a downloaded sample through the real business workflow:

    source manifest -> import -> basic quality -> review fixture
    -> conversion -> delivery -> independent ZIP verification

It writes a machine-readable report with per-stage status, duration, counts and
hashes. ``--mode source`` stops after review and is suitable for Linux CI where
the isolated LeRobot/PyTorch conversion environment is not installed.
``--mode fast`` runs conversion and delivery but skips video decode spot checks.
``--mode full`` additionally decodes raw and output video frames.

This is not a policy-training or hardware-synchronization claim. The annotations
written here are explicit acceptance fixtures, not source task outcomes.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robodata import business  # noqa: E402
from robodata.config import APP_VERSION, PROJECT_ROOT, parse_episode_range  # noqa: E402
from robodata.dataset import load_episode, load_metadata  # noqa: E402
from robodata.delivery import verified_delivery  # noqa: E402
from robodata.importing import preview_batch  # noqa: E402
from robodata.runtime import wait_run  # noqa: E402
from robodata.source import MANIFEST_NAME, file_sha256, verify_manifest, verify_sample  # noqa: E402
from robodata.video import read_video_frame  # noqa: E402

BOUNDARIES = [
    "The review labels are acceptance fixtures; they are not source task outcomes.",
    "A passed conversion proves format and numeric consistency for this subset, not policy success.",
    "No hardware synchronization or real-robot execution is measured.",
    "The sample is a complete upstream episode subset; frames are not trimmed or rewritten.",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def stage_start(report: dict, name: str) -> dict:
    entry = {"name": name, "status": "running", "started_at_utc": now()}
    report["stages"].append(entry)
    return entry


def stage_finish(entry: dict, status: str, **details) -> None:
    entry["status"] = status
    entry["seconds"] = round(time.perf_counter() - entry.pop("_started"), 3)
    entry["finished_at_utc"] = now()
    if details:
        entry["details"] = details


def timed(report: dict, name: str):
    class Timer:
        def __enter__(self):
            self.entry = stage_start(report, name)
            self.entry["_started"] = time.perf_counter()
            return self.entry

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                stage_finish(self.entry, "passed")
            else:
                stage_finish(self.entry, "failed", error=f"{exc_type.__name__}: {exc}")
            return False
    return Timer()


def run_operation(report: dict, name: str, starter, runs_root: Path, timeout: float) -> dict:
    with timed(report, name) as entry:
        run_id = starter()
        entry["run_id"] = run_id
        state = wait_run(run_id, runs_root=runs_root, timeout=timeout)
        entry["details"] = {
            "status": state.get("status"),
            "message": state.get("message"),
            "summary": state.get("summary"),
            "outcome": state.get("outcome"),
        }
        if state.get("status") != "completed":
            raise RuntimeError(f"{name} 未完成：{state.get('status')} {state.get('message', '')}")
        if state.get("outcome") == "rejected":
            raise RuntimeError(f"{name} 被拒绝：{state.get('message', '')}")
        return state


def review_fixture(store, batch: dict) -> dict:
    batch = store.confirm_cleaning(batch["batch_id"], expected_revision=batch["revision"])
    kept = [episode for episode in batch["episodes"] if episode["disposition"] == "keep"]
    if not kept:
        raise ValueError("筛选后没有保留任何 episode")
    for episode in kept:
        index = episode["episode_index"]
        batch = store.save_annotation(
            batch["batch_id"], index,
            task="red sweet pick",
            outcome="uncertain",
            tags=["reproducible-acceptance"],
            notes="验收脚本填写的审核夹具；不代表源任务实际成功或失败。",
            expected_revision=batch["revision"],
        )
        batch = store.submit_annotation(batch["batch_id"], index, expected_revision=batch["revision"])
        batch = store.review_annotation(batch["batch_id"], index, approve=True,
                                        expected_revision=batch["revision"])
    return batch


def check_raw_preview(report: dict, batch: dict, full: bool) -> None:
    if not full:
        return
    with timed(report, "raw_preview") as entry:
        samples = []
        for episode in batch["episodes"]:
            for row in (0, episode["row_count"] // 2, episode["row_count"] - 1):
                preview = preview_batch(batch, episode["episode_index"], row)
                image = preview["image"]
                if image is None or image.shape != (480, 640, 3):
                    raise ValueError(f"原始预览图像不符合预期：episode {episode['episode_index']} row {row}")
                if preview["issues"]:
                    raise ValueError(f"原始预览报告问题：{preview['issues']}")
                samples.append({"episode": episode["episode_index"], "row": row,
                                "shape": list(image.shape)})
        entry["details"] = {"samples": samples, "count": len(samples)}


def check_output_frames(report: dict, runs_root: Path, run_id: str, output_root: Path,
                        expected_episodes: int, expected_rows: int, full: bool) -> None:
    metadata = load_metadata(output_root)
    if metadata["episode_indices"] != list(range(expected_episodes)):
        raise ValueError(f"输出 episode 范围不正确：{metadata['episode_indices']}")
    if metadata["info"]["total_frames"] != expected_rows:
        raise ValueError(f"输出帧数不正确：{metadata['info']['total_frames']} != {expected_rows}")
    if not full:
        with timed(report, "output_metadata") as entry:
            entry["details"] = {"episodes": metadata["episode_indices"],
                                "total_frames": metadata["info"]["total_frames"],
                                "fps": metadata["info"]["fps"]}
        return
    with timed(report, "output_frames") as entry:
        decoded = np.load(runs_root / run_id / "decoded_samples.npz")
        matches = []
        for index, (episode_index, row) in enumerate(zip(decoded["episode_indices"],
                                                          decoded["frame_indices"])):
            episode = load_episode(output_root, int(episode_index))
            frame = read_video_frame(episode.video_path,
                                     float(episode.table.iloc[int(row)]["timestamp"]) + episode.video_start_time)
            np.testing.assert_array_equal(frame["image"], decoded["images"][index])
            matches.append({"episode": int(episode_index), "row": int(row)})
        entry["details"] = {"samples": matches, "count": len(matches),
                            "episodes": metadata["episode_indices"],
                            "total_frames": metadata["info"]["total_frames"]}


def probe_training_loader(report: dict, conversion_python: Path, output_root: Path,
                          runs_root: Path, run_id: str, batch_size: int = 8) -> dict:
    """Run the isolated-environment DataLoader probe on the converted output."""
    with timed(report, "dataloader_probe") as entry:
        probe_report = runs_root / run_id / "dataloader_probe.json"
        environment = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                       "TRANSFORMERS_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
        completed = subprocess.run(
            [str(conversion_python), str(ROOT / "scripts/probe_dataloader.py"),
             "--dataset", str(output_root), "--batch-size", str(batch_size),
             "--batches", "4", "--output", str(probe_report)],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=1200,
            encoding="utf-8", errors="replace",
        )
        log_path = runs_root / run_id / "dataloader_probe.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
        if not probe_report.is_file():
            raise RuntimeError(f"DataLoader 探针没有写出报告：{completed.stdout[-800:]} {completed.stderr[-800:]}")
        value = json.loads(probe_report.read_text(encoding="utf-8"))
        summary = value.get("summary", {})
        if completed.returncode != 0 or not summary.get("passed"):
            raise RuntimeError(f"DataLoader 探针未通过：{summary.get('error') or summary}")
        entry["details"] = {
            "summary": summary,
            "loader": value.get("loader"),
            "environment": value.get("environment"),
            "dataset_load_seconds": value.get("dataset_load_seconds"),
            "dataload_seconds": value.get("dataload_seconds"),
            "checks": value.get("checks"),
            "report": str(probe_report),
            "log": str(log_path),
        }
        report["dataloader_probe"] = {key: entry["details"][key] for key in
                                      ("summary", "loader", "environment", "dataset_load_seconds",
                                       "dataload_seconds", "checks")}
        return entry["details"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, default=PROJECT_ROOT / "work/samples/so100-e0-1",
                        help="已下载的 SO-100 样本目录（含 source_manifest.json）")
    parser.add_argument("--episodes", default=None, help="样本 episode 范围，如 0-1；缺省按清单")
    parser.add_argument("--mode", choices=("source", "fast", "full"), default="fast")
    parser.add_argument("--business-root", type=Path, default=PROJECT_ROOT / "work/acceptance/business")
    parser.add_argument("--runs-root", type=Path, default=PROJECT_ROOT / "work/acceptance/runs")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--online", action="store_true",
                        help="额外联网复核固定 revision 的上游文件标识")
    parser.add_argument("--endpoint", default=None, help="联网复核使用的传输端点")
    args = parser.parse_args()

    sample = args.sample.resolve()
    episodes = parse_episode_range(args.episodes, ()) if args.episodes else None
    if episodes == []:
        parser.error("--episodes 不能为空")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    report_path = args.output or (PROJECT_ROOT / "work/acceptance" / f"pipeline-{args.mode}-{stamp}.json")
    report = {
        "schema_version": 1,
        "pipeline": "robodata-so100-acceptance",
        "mode": args.mode,
        "app_version": APP_VERSION,
        "started_at_utc": now(),
        "sample": {"path": str(sample), "episodes_requested": episodes},
        "stages": [],
        "boundaries": BOUNDARIES,
    }
    store = business.get_store(args.business_root)
    batch = None
    try:
        with timed(report, "source_verify") as entry:
            local = verify_manifest(sample, episodes=episodes)
            if not local["ok"]:
                raise ValueError(f"本地来源校验失败：missing={local['missing']} mismatched={local['mismatched']}")
            details = {"local": {key: local[key] for key in
                                  ("ok", "repo_id", "revision", "episodes", "file_count", "total_bytes",
                                   "missing", "mismatched", "scope")},
                       "manifest_sha256": file_sha256(sample / MANIFEST_NAME)}
            manifest = json.loads((sample / MANIFEST_NAME).read_text(encoding="utf-8"))
            details["transport_endpoint"] = manifest.get("transport_endpoint")
            if episodes is None:
                episodes = list(manifest["episodes"])
                report["sample"]["episodes_requested"] = episodes
            if args.online:
                upstream = verify_sample(sample, episodes=episodes, endpoint=args.endpoint)
                details["upstream"] = {key: upstream[key] for key in
                                       ("ok", "repo_id", "revision", "episodes", "file_count",
                                        "missing", "mismatched", "scope")}
                if not upstream["ok"]:
                    raise ValueError("上游来源复核失败")
            entry["details"] = details

        label = f"SO-100 reproducible {args.mode} {stamp}"
        imported = run_operation(
            report, "import_quality",
            lambda: business.start_import(sample, "so100", label, runs_root=args.runs_root,
                                          business_root=args.business_root),
            args.runs_root, args.timeout,
        )
        batch = store.get_batch(imported["batch_id"])
        report["batch_id"] = batch["batch_id"]
        expected_episodes = len(batch["episodes"])
        expected_rows = batch["total_rows"]
        report["sample"].update({"episodes": [e["episode_index"] for e in batch["episodes"]],
                                 "rows": expected_rows,
                                 "input_fingerprint": batch["source"]["input_fingerprint"]})
        if sorted(episodes) != [e["episode_index"] for e in batch["episodes"]]:
            raise ValueError("导入批次的 episode 范围与来源清单不一致")
        if imported["summary"].get("data_issue_count") or imported["summary"].get("load_failure_count"):
            raise ValueError(f"基础质检未通过：{imported['summary']}")

        check_raw_preview(report, batch, full=args.mode == "full")

        with timed(report, "review_fixture") as entry:
            batch = review_fixture(store, batch)
            entry["details"] = {"revision": batch["revision"],
                                "reviewed": len([e for e in batch["episodes"] if e["disposition"] == "keep"]),
                                "note": "acceptance fixture only"}

        if args.mode == "source":
            report["summary"] = {
                "passed": True,
                "scope": "source + import + basic quality + review fixture; conversion not run",
                "episodes": expected_episodes,
                "rows": expected_rows,
            }
        else:
            conversion_python = ROOT / "tools/conversion/.venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            if not conversion_python.is_file():
                raise FileNotFoundError(
                    "缺少转换环境。先运行：uv sync --project tools/conversion --locked；"
                    "只想验证来源与表格质检请用 --mode source"
                )
            converted = run_operation(
                report, "conversion",
                lambda: business.start_batch_conversion(batch["batch_id"], batch["revision"],
                                                        runs_root=args.runs_root,
                                                        business_root=args.business_root),
                args.runs_root, args.timeout,
            )
            if converted["summary"].get("row_count") != expected_rows:
                raise ValueError(f"转换帧数不一致：{converted['summary'].get('row_count')} != {expected_rows}")
            batch = store.get_batch(batch["batch_id"])
            output = next((item for item in batch["outputs"] if item["run_id"] == converted["run_id"]), None)
            if output is None:
                raise ValueError("转换完成但没有登记输出")
            report["conversion"] = {"run_id": converted["run_id"],
                                    "output_id": output["output_id"],
                                    "output_path": output["artifact_path"],
                                    "summary": converted["summary"]}
            for step in ("metadata_upgrade", "official_verify"):
                path = args.runs_root / converted["run_id"] / "steps" / f"{step}.json"
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("status") != "passed":
                    raise ValueError(f"转换阶段 {step} 未通过")
                report["conversion"][step] = value.get("details")
            check_output_frames(report, args.runs_root, converted["run_id"],
                                Path(output["artifact_path"]), expected_episodes, expected_rows,
                                full=args.mode == "full")
            probe_training_loader(report, conversion_python, Path(output["artifact_path"]),
                                  args.runs_root, converted["run_id"])

            delivered = run_operation(
                report, "delivery",
                lambda: business.start_delivery(batch["batch_id"], output["output_id"], batch["revision"],
                                                runs_root=args.runs_root,
                                                business_root=args.business_root),
                args.runs_root, args.timeout,
            )
            batch = store.get_batch(batch["batch_id"])
            delivery = next((item for item in batch["deliveries"]
                             if item["packaging_run_id"] == delivered["run_id"]), None)
            if delivery is None:
                raise ValueError("打包完成但没有登记交付记录")
            with timed(report, "zip_verify") as entry:
                verified = verified_delivery(batch, delivery["delivery_id"])
                archive = Path(verified["path"])
                with zipfile.ZipFile(archive) as handle:
                    for name in handle.namelist():
                        if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                            raise ValueError(f"ZIP 包含不安全路径：{name}")
                archive_step = args.runs_root / delivered["run_id"] / "steps/archive_verify.json"
                archive_value = json.loads(archive_step.read_text(encoding="utf-8"))
                if archive_value.get("status") != "passed":
                    raise ValueError("交付包官方离线加载未通过")
                entry["details"] = {
                    "path": str(archive),
                    "sha256": file_sha256(archive),
                    "size_bytes": archive.stat().st_size,
                    "episode_count": verified["episode_count"],
                    "row_count": verified["row_count"],
                    "archive_verify": archive_value.get("details"),
                }
            report["delivery"] = entry["details"]
            report["summary"] = {
                "passed": True,
                "scope": ("source + import + quality + review + conversion + official DataLoader + delivery + independent ZIP verify"
                          if args.mode == "fast" else
                          "source + import + quality + review + conversion + official DataLoader + delivery + independent ZIP verify + video spot checks"),
                "episodes": expected_episodes,
                "rows": expected_rows,
                "zip_sha256": report["delivery"]["sha256"],
                "zip_bytes": report["delivery"]["size_bytes"],
            }
    except Exception as exc:
        report["summary"] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        report["finished_at_utc"] = now()
        write_json(report_path, report)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"报告：{report_path}")
        return 1

    report["finished_at_utc"] = now()
    report["run_count"] = len([entry for entry in report["stages"] if entry.get("run_id")])
    write_json(report_path, report)
    print(json.dumps({**report["summary"], "stages": len(report["stages"]),
                      "app_version": APP_VERSION}, ensure_ascii=False, indent=2))
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
