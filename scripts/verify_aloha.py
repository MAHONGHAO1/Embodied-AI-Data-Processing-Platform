"""Run the pinned ALOHA sample through the supervised workbench workflow.

This manual acceptance creates an isolated business store and run directory. It
uses public source bytes, then simulates the annotation and review gate with
explicitly labeled acceptance annotations before exercising the production
conversion, packaging, and archive verification stages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from robodata.aloha import (ALOHA_CAMERAS, ALOHA_EPISODES, ALOHA_REPO_ID,
                            ALOHA_REVISION, episode_task_text)  # noqa: E402
from robodata.business import get_store, start_final_quality, start_import  # noqa: E402
from robodata.dataset import dataset_fingerprint, load_metadata  # noqa: E402
from robodata.importing import verify_managed_source  # noqa: E402
from robodata.runtime import get_run, wait_run  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _wait(run_id: str, runs_root: Path, timeout: float, notify) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"等待业务运行超时：{run_id}")
        try:
            state = wait_run(run_id, runs_root=runs_root, timeout=min(45, remaining))
            break
        except TimeoutError:
            state = get_run(run_id, runs_root=runs_root)
            current = state.get("current_node") or "等待后台处理"
            notify(f"运行 {run_id} 状态：{state['status']}；当前节点：{current}")
    if state["status"] != "completed":
        raise RuntimeError(f"业务运行 {run_id} 未完成：{state['status']}；{state.get('message', '')}")
    return state


def _copy_run_evidence(run_id: str, runs_root: Path, destination: Path) -> None:
    source = runs_root / run_id
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("state.json", "request.json", "events.jsonl", "registration.json",
                 "diagnostic.json", "conversion_spec.json", "coordinator.stdout.log",
                 "coordinator.stderr.log"):
        path = source / name
        if path.is_file():
            shutil.copy2(path, destination / name)
    for folder in ("steps", "reports"):
        path = source / folder
        if path.is_dir():
            shutil.copytree(path, destination / folder, dirs_exist_ok=True)


def _source_hashes(root: Path, manifest: dict) -> dict[str, str]:
    result = {}
    for item in manifest["files"]:
        path = root / item["path"]
        if not path.is_file() or path.stat().st_size != item["size"]:
            raise ValueError(f"来源文件缺失或大小变化：{item['path']}")
        actual = _sha256(path)
        if actual != item["sha256"]:
            raise ValueError(f"来源 SHA-256 与固定 revision 清单不符：{item['path']}")
        result[item["path"]] = actual
    return result


def _approve_acceptance_annotations(store, batch: dict, notify) -> dict:
    """Exercise the same store gates as the UI with transparent test labels."""
    failed = [ep["episode_index"] for ep in batch["episodes"] if ep["quality"]["status"] != "passed"]
    if failed:
        raise ValueError(f"ALOHA 基础质检未通过，不能确认候选集：{failed}")

    batch = store.confirm_cleaning(batch["batch_id"], expected_revision=batch["revision"])
    metadata = load_metadata(Path(batch["source"]["data_root"]))
    for episode in batch["episodes"]:
        index = episode["episode_index"]
        source_episode = metadata["episodes"][index]
        source_task = episode_task_text(metadata, source_episode)
        batch = store.save_annotation(
            batch["batch_id"], index,
            task=f"公开样例任务：{source_task}",
            outcome="uncertain",
            tags=["自动化集成验收", "公开样例"],
            notes="自动化验收夹具使用来源任务描述并模拟审核门禁；不代表人工业务审核或机器人执行效果。",
            expected_revision=batch["revision"],
        )
        batch = store.submit_annotation(batch["batch_id"], index, expected_revision=batch["revision"])
        batch = store.review_annotation(
            batch["batch_id"], index, approve=True,
            reason="自动化集成验收：仅为验证受控工作台后续流程而模拟审核通过。",
            expected_revision=batch["revision"],
        )
        notify(f"自动化验收已模拟完成 Episode {index} 的标注提交与审核门禁")
    return batch


def main() -> int:
    parser = argparse.ArgumentParser(description="固定 ALOHA Episode 0–4 工作台端到端集成验收")
    parser.add_argument("--work-root", type=Path, default=ROOT / "work" / "aloha-integration",
                        help="验收业务存储和运行目录；建议使用全新目录")
    parser.add_argument("--artifacts", type=Path, required=True, help="验收报告、日志和节点证据输出目录")
    parser.add_argument("--proxy", help="可选 HTTPS 代理地址")
    parser.add_argument("--timeout-seconds", type=int, default=10_500,
                        help="每段后台运行的最长等待秒数，默认 175 分钟")
    args = parser.parse_args()
    work_root = args.work_root.resolve()
    artifacts = args.artifacts.resolve()
    if args.timeout_seconds < 60:
        parser.error("--timeout-seconds 必须至少为 60 秒")
    if work_root == artifacts or work_root.is_relative_to(artifacts) or artifacts.is_relative_to(work_root):
        parser.error("验收工作目录与证据目录必须分开，避免上传大型原始数据或转换产物")
    work_root.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    log_path = artifacts / "integration.log"
    logging.basicConfig(filename=log_path, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    def notify(message: str) -> None:
        print(message, flush=True)
        logging.info(message)

    if any(work_root.iterdir()):
        raise ValueError(f"验收工作目录必须为空，以免复用旧状态：{work_root}")
    business_root = work_root / "business"
    runs_root = work_root / "runs"
    store = get_store(business_root)
    label = f"ALOHA Coffee v0.6 automated acceptance {datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    os.environ["ROBODATA_BUSINESS_ROOT"] = str(business_root)
    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["HF_DATASETS_OFFLINE"] = "0"
    if args.proxy:
        os.environ["HTTPS_PROXY"] = args.proxy
        os.environ["https_proxy"] = args.proxy

    try:
        notify(f"开始导入固定来源 {ALOHA_REPO_ID}@{ALOHA_REVISION}")
        import_run_id = start_import(
            f"pinned://{ALOHA_REPO_ID}@{ALOHA_REVISION}", "aloha", label,
            runs_root=runs_root, business_root=business_root,
            timeouts={"quality": 1800, "preview": 900},
        )
        import_state = _wait(import_run_id, runs_root, args.timeout_seconds, notify)
        batch_id = import_state["batch_id"]
        batch = store.get_batch(batch_id)
        source_root = Path(batch["source"]["data_root"]).resolve()
        source_manifest = json.loads((source_root / "source_manifest.json").read_text(encoding="utf-8"))
        source_hashes_before = _source_hashes(source_root, source_manifest)
        metadata = load_metadata(source_root)
        if batch["source"].get("repo_id") != ALOHA_REPO_ID or batch["source"].get("revision") != ALOHA_REVISION:
            raise ValueError("工作台登记的 ALOHA 来源身份或固定 revision 不匹配")
        if source_manifest["source"].get("license") != "MIT（固定版本数据集页面声明）":
            raise ValueError("固定来源许可证记录不匹配")
        if metadata["info"].get("fps") != 50:
            raise ValueError("ALOHA 固定来源帧率与预期 50 FPS 不符")
        if tuple(batch["source"].get("cameras", [])) != ALOHA_CAMERAS:
            raise ValueError("工作台登记的相机字段与固定四路配置不匹配")
        if tuple(ep["episode_index"] for ep in batch["episodes"]) != ALOHA_EPISODES:
            raise ValueError("工作台登记的任务范围不是完整的 Episode 0–4")
        expected_rows = sum(int(metadata["episodes"][index]["length"]) for index in ALOHA_EPISODES)
        if batch["total_rows"] != expected_rows:
            raise ValueError("工作台批次总帧数与来源元数据不一致")
        source_fingerprint_before = dataset_fingerprint(source_root)
        verify_managed_source(batch["source"])

        batch = _approve_acceptance_annotations(store, batch, notify)
        final_run_id = start_final_quality(
            batch_id, batch["revision"], auto_continue=True,
            runs_root=runs_root, business_root=business_root,
            timeouts={"quality": 1800, "final_quality": 1800, "format_write": 7200,
                      "official_verify": 1800, "output_quality": 1800, "package": 1800,
                      "verify": 1800, "archive_verify": 1800},
        )
        final_state = _wait(final_run_id, runs_root, args.timeout_seconds, notify)
        batch = store.get_batch(batch_id)

        source_fingerprint_after = dataset_fingerprint(source_root)
        source_hashes_after = _source_hashes(source_root, source_manifest)
        source_unchanged = (source_fingerprint_before == source_fingerprint_after
                            and source_hashes_before == source_hashes_after
                            and verify_managed_source(batch["source"]) == batch["source"]["input_fingerprint"])
        if not source_unchanged:
            raise ValueError("端到端验收期间来源文件或管理副本指纹发生变化")

        outputs = [item for item in batch["outputs"] if item["run_id"] == final_run_id]
        deliveries = [item for item in batch["deliveries"] if item["packaging_run_id"] == final_run_id]
        if len(outputs) != 1 or len(deliveries) != 1:
            raise ValueError("完整链路结束后必须恰好登记一个通过验证的输出和一个交付包")
        output = outputs[0]
        delivery = deliveries[0]
        output_manifest_path = Path(output["artifact_path"]) / "source_manifest.json"
        output_manifest = json.loads(output_manifest_path.read_text(encoding="utf-8"))
        camera_metadata = output_manifest.get("camera_metadata", {})
        if set(output_manifest.get("camera_features", [])) != set(ALOHA_CAMERAS):
            raise ValueError("LeRobot v3.0 输出未保留四路相机")
        if any(camera_metadata.get(camera, {}).get("fps") != 50 for camera in ALOHA_CAMERAS):
            raise ValueError("输出相机元数据缺少 50 FPS 记录")
        if final_state.get("outcome") != "passed" or final_state.get("status") != "completed":
            raise ValueError("完整链路运行状态没有通过")
        required_stages = {"source_verify", "final_quality", "final_registration", "file_validation",
                           "field_mapping", "format_write", "official_verify", "output_quality",
                           "conversion_registration", "package", "verify", "archive_verify", "registration"}
        node_status = {node["node_id"]: node["status"] for node in final_state["nodes"]}
        if any(node_status.get(stage) != "completed" for stage in required_stages):
            raise ValueError("完整链路的业务阶段未全部完成")

        acceptance = {
            "acceptance_version": 1,
            "source": {"repo_id": ALOHA_REPO_ID, "revision": ALOHA_REVISION,
                       "license": source_manifest["source"]["license"],
                       "files": source_manifest["files"], "file_sha256_before": source_hashes_before,
                       "file_sha256_after": source_hashes_after,
                       "source_fingerprint_before": source_fingerprint_before,
                       "source_fingerprint_after": source_fingerprint_after,
                       "unchanged": source_unchanged},
            "workflow": {"import_run_id": import_run_id, "final_run_id": final_run_id,
                         "batch_id": batch_id, "status": final_state["status"],
                         "outcome": final_state.get("outcome"), "nodes": node_status,
                         "synthetic_review_gate": True},
            "dataset": {"episodes": list(ALOHA_EPISODES), "episode_count": len(ALOHA_EPISODES),
                        "row_count": expected_rows, "fps": metadata["info"]["fps"],
                        "camera_features": list(ALOHA_CAMERAS),
                        "output_manifest_sha256": _sha256(output_manifest_path)},
            "conversion_validation": output["validation"],
            "delivery": {"delivery_id": delivery["delivery_id"], "zip_path": delivery["zip_path"],
                         "zip_sha256": delivery["zip_sha256"],
                         "archive_verified": True, "independent_unpack_verified": True},
        }
        _write(artifacts / "acceptance.json", acceptance)
        _copy_run_evidence(import_run_id, runs_root, artifacts / "import-run")
        _copy_run_evidence(final_run_id, runs_root, artifacts / "final-run")
        shutil.copy2(source_root / "source_manifest.json", artifacts / "source_manifest.json")
        shutil.copy2(output_manifest_path, artifacts / "output_manifest.json")
        logging.info("acceptance=%s", json.dumps(acceptance, ensure_ascii=False))
        print(json.dumps(acceptance, ensure_ascii=False, indent=2), flush=True)
        return 0
    except Exception:
        logging.exception("ALOHA 工作台端到端集成验收失败")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
