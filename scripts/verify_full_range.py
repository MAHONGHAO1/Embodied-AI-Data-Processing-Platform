"""全量 HDF5 范围（demo_0-9 / 531 帧）端到端验收。

与 scripts/verify_business.py 的 3 条验收并行存在，不改动其结论与证据文件。
本脚本验证 ROBODATA_HDF5_EPISODES 扩容路径下八节点闭环仍然成立：
导入 → 质检 → 筛选确认 → 标注审核 → 最终质检 → 官方转换 → 打包交付 → 独立验证。

必须在 ROBODATA_HDF5_EPISODES=0-9 下运行；否则直接失败，避免产出与
范围不符的证据。原始文件从不修改。
"""
import json
import os
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np

from robodata import business
from robodata.config import APP_VERSION, HDF5_EPISODES, PROJECT_ROOT
from robodata.dataset import load_episode, load_metadata
from robodata.delivery import verified_delivery
from robodata.importing import preview_batch
from robodata.runtime import get_run, wait_run
from robodata.source import file_sha256
from robodata.sources import HDF5_PATH, HDF5_SHA256
from robodata.video import read_video_frame

EVIDENCE = PROJECT_ROOT / "work/full-range-acceptance.json"
EXPECTED_EPISODES = 10
EXPECTED_ROWS = 531
BATCH_LABEL = "Panda／Lift 全量范围验收（demo_0-9）"
store = business.get_store()
record = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.is_file() else {"runs": {}, "checks": {}}


def save():
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def run(name, start, expected="completed"):
    prior = record["runs"].get(name)
    if prior:
        state = get_run(prior)
        if state["status"] == expected:
            print(f"[skip] {name} {prior} 已完成", flush=True)
            return state
    run_id = start()
    record["runs"][name] = run_id
    save()
    print(f"[run ] {name} {run_id}", flush=True)
    state = wait_run(run_id, timeout=3000)
    assert state["status"] == expected, (name, state["status"], state["message"])
    save()
    return state


def current():
    return store.get_batch(record["batch_id"])


def main():
    assert HDF5_EPISODES == list(range(EXPECTED_EPISODES)), (
        "请以 ROBODATA_HDF5_EPISODES=0-9 运行；当前范围：" + repr(HDF5_EPISODES))
    assert file_sha256(HDF5_PATH) == HDF5_SHA256, "固定样本哈希与预期不一致"

    imported = run("import", lambda: business.start_import(HDF5_PATH, "hdf5", BATCH_LABEL))
    record["batch_id"] = imported["batch_id"]
    save()
    batch = current()
    assert batch["total_episodes"] == EXPECTED_EPISODES, batch["total_episodes"]
    assert batch["total_rows"] == EXPECTED_ROWS, batch["total_rows"]
    assert batch["source"]["source_statistics"] == {"declared_total": 9666, "actual_total": 531, "demo_count": 10}
    print(f"      批次 {batch['batch_id']}：{batch['total_episodes']} 条 / {batch['total_rows']} 帧", flush=True)

    checks = []
    for ep in batch["episodes"]:
        for row in (0, ep["row_count"] // 2, ep["row_count"] - 1):
            preview = preview_batch(batch, ep["episode_index"], row)
            assert preview["image"].shape == (84, 84, 3)
            assert int(preview["table"].iloc[row]["frame_index"]) == row
            assert len(preview["state_names"]) == 9 and len(preview["action_names"]) == 7
            checks.append({"episode": ep["episode_index"], "row": row, "image_shape": [84, 84, 3]})
    record["checks"]["raw_preview"] = checks
    print(f"      原始预览：{len(checks)} 个采样位置全部通过", flush=True)

    quality = run("quality", lambda: business.start_batch_quality(batch["batch_id"], current()["revision"]))
    assert quality["summary"]["coverage_performed"] == EXPECTED_EPISODES * 10, quality["summary"]
    assert quality["summary"]["issue_count"] == 0, quality["summary"]
    record["checks"]["input_quality"] = quality["summary"]
    print(f"      质检：{quality['summary']['coverage_performed']} 项覆盖 / 0 问题", flush=True)

    # 质检通过的任务自动进入候选集；此处只确认，不做排除。
    if not record.get("review_completed"):
        b = current()
        b = store.confirm_cleaning(b["batch_id"], expected_revision=b["revision"])
        kept = [e["episode_index"] for e in b["episodes"] if e["disposition"] == "keep"]
        assert kept == list(range(EXPECTED_EPISODES)), kept
        for index in kept:
            b = store.save_annotation(b["batch_id"], index, task="Lift：提起方块",
                                      outcome="failure" if index == 0 else "uncertain",
                                      tags=["全量验收标注"],
                                      notes="人工流程标签，不作为源机器人成功率结论；原始任务为 Lift。",
                                      expected_revision=b["revision"])
            b = store.submit_annotation(b["batch_id"], index, expected_revision=b["revision"])
            if index == 0:
                b = store.review_annotation(b["batch_id"], index, approve=False,
                                            reason="补充该执行结果是验收标签的说明",
                                            expected_revision=b["revision"])
                assert b["episodes"][index]["review"]["status"] == "returned"
                b = store.save_annotation(b["batch_id"], index, task="Lift：提起方块", outcome="failure",
                                          tags=["全量验收标注"],
                                          notes="流程验收人为标注为失败，证明有效失败示范可保留；不宣称公开源任务实际失败。",
                                          expected_revision=b["revision"])
                b = store.submit_annotation(b["batch_id"], index, expected_revision=b["revision"])
            b = store.review_annotation(b["batch_id"], index, approve=True, expected_revision=b["revision"])
        record["review_completed"] = True
        save()
        print(f"      筛选确认与审核：{len(kept)} 条全部通过（含 1 次退回重提）", flush=True)

    b = current()
    spec = store.conversion_spec(b["batch_id"])
    source_indices = [e["source_episode_index"] for e in spec["episodes"]]
    assert source_indices == list(range(EXPECTED_EPISODES)), source_indices
    record["checks"]["selection"] = {"source": source_indices, "frames": EXPECTED_ROWS}
    print(f"      候选映射：源 {source_indices[0]}–{source_indices[-1]} → 输出 0–{len(source_indices) - 1}", flush=True)

    converted = run("conversion", lambda: business.start_batch_conversion(b["batch_id"], current()["revision"]))
    summary = converted["summary"]
    assert summary["row_count"] == EXPECTED_ROWS, summary
    record["checks"]["conversion"] = summary
    print(f"      转换：{summary['coverage_performed']} 项输出质检 / {summary['row_count']} 帧", flush=True)

    b = current()
    output = next(o for o in b["outputs"] if o["run_id"] == converted["run_id"])
    record["output_id"] = output["output_id"]
    record["output_path"] = output["artifact_path"]
    conversion_dir = PROJECT_ROOT / "work/runs" / converted["run_id"]
    official = json.loads((conversion_dir / "steps/official_verify.json").read_text(encoding="utf-8"))
    assert official["status"] == "passed"
    assert official["details"]["offline"] is True
    assert official["details"]["checked_numeric_frames"] == EXPECTED_ROWS, official["details"]
    record["checks"]["official_verify"] = official["details"]
    print(f"      官方验证：离线加载通过，{official['details']['checked_numeric_frames']} 帧数值比对", flush=True)

    root = Path(output["artifact_path"])
    metadata = load_metadata(root)
    assert metadata["episode_indices"] == list(range(EXPECTED_EPISODES)), metadata["episode_indices"]
    assert metadata["info"]["total_frames"] == EXPECTED_ROWS
    assert metadata["info"]["splits"] == {"train": f"0:{EXPECTED_EPISODES}"}, metadata["info"]["splits"]
    decoded = np.load(conversion_dir / "decoded_samples.npz")
    matching = []
    for ix, (episode, row) in enumerate(zip(decoded["episode_indices"], decoded["frame_indices"])):
        ep = load_episode(root, int(episode))
        frame = read_video_frame(ep.video_path, float(ep.table.iloc[int(row)]["timestamp"]) + ep.video_start_time)
        np.testing.assert_array_equal(frame["image"], decoded["images"][ix])
        matching.append({"episode": int(episode), "row": int(row),
                         "video_start_time": ep.video_start_time, "official_pixels_equal": True})
    record["checks"]["output_frames"] = matching
    print(f"      输出读取：{len(matching)} 个采样位置像素一致", flush=True)

    delivered = run("delivery", lambda: business.start_delivery(b["batch_id"], output["output_id"], current()["revision"]))
    b = current()
    delivery = next(d for d in b["deliveries"] if d["packaging_run_id"] == delivered["run_id"])
    verified = verified_delivery(b, delivery["delivery_id"])
    z = Path(verified["path"])
    assert verified["row_count"] == EXPECTED_ROWS and verified["episode_count"] == EXPECTED_EPISODES, verified
    with zipfile.ZipFile(z) as archive:
        for name in archive.namelist():
            assert not name.startswith(("/", "\\")) and ".." not in Path(name).parts
            if name.endswith((".json", ".txt", ".md")):
                text = archive.read(name).decode("utf-8")
                assert not re.search(r"\b[A-Za-z]:[\\/]", text), (name, "local absolute path")
                assert ".venv" not in name and ".env" not in name
    record["delivery_id"] = delivery["delivery_id"]
    record["delivery"] = verified
    print(f"      交付包：{verified['row_count']} 帧 / {verified['episode_count']} 条，路径可移植", flush=True)

    verify_again = run("independent_verify", lambda: business.start_verify_delivery(z))
    assert verify_again["status"] == "completed"
    before_revision = b["revision"]
    duplicate = run("duplicate_import", lambda: business.start_import(HDF5_PATH, "hdf5", BATCH_LABEL))
    assert duplicate["batch_id"] == b["batch_id"] and current()["revision"] == before_revision
    record["checks"].update(duplicate_reuses_batch=True, zip_portable=True,
                            independent_official_load=True,
                            raw_unchanged=file_sha256(HDF5_PATH) == HDF5_SHA256)
    record.update(status="passed", app_version=APP_VERSION, source_sha256=HDF5_SHA256,
                  data_range=HDF5_EPISODES, episode_count=EXPECTED_EPISODES, frame_count=EXPECTED_ROWS)
    save()
    print("PASS", EVIDENCE, flush=True)


if __name__ == "__main__":
    main()
