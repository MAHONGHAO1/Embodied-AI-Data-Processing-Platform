"""SO-100 真机数据集（LeRobot v2.0）端到端验收。

与 scripts/verify_full_range.py 的 HDF5 验收并行存在，不改动其结论与证据文件。
本脚本验证第二条数据源接入后八节点闭环仍然成立：
导入 → 质检 → 筛选确认 → 标注审核 → 最终质检 → 官方转换 → 打包交付 → 独立验证。

转换链路与 HDF5 不同：SO-100 是 LeRobot v2.0，需要先补齐 v2.1 的
per-episode 统计（meta/episodes_stats.jsonl），再逐阶段转到 v3.0。
官方 convert_dataset() 末尾会 rmtree 并原地替换数据集，因此平台只调用
其阶段函数，源数据始终只读。

默认范围 5 条 / 3562 帧（约 53 MB）。以 ROBODATA_SO100_EPISODES=0-49
运行可覆盖全量 50 条 / 32068 帧。原始文件从不修改。
"""
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np

from robodata import business
from robodata.config import APP_VERSION, DEFAULT_DATA_ROOT, PROJECT_ROOT, SO100_EPISODES
from robodata.dataset import load_episode, load_metadata
from robodata.delivery import verified_delivery
from robodata.importing import preview_batch
from robodata.runtime import get_run, wait_run
from robodata.video import read_video_frame

EVIDENCE = PROJECT_ROOT / "work/so100-acceptance.json"
SOURCE_ROOT = Path(DEFAULT_DATA_ROOT)
BATCH_LABEL = "SO-100 真机验收（jmrog/so100_sweet_pick）"
EXPECTED_EPISODES = len(SO100_EPISODES)
IMAGE_SHAPE = (480, 640, 3)
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
    assert SOURCE_ROOT.is_dir(), f"缺少 SO-100 源数据：{SOURCE_ROOT}"
    assert (SOURCE_ROOT / "meta" / "info.json").is_file(), "源目录不是有效的 LeRobot 数据集"

    info = json.loads((SOURCE_ROOT / "meta" / "info.json").read_text(encoding="utf-8"))
    record["checks"]["source"] = {"repo_id": info.get("repo_id"), "fps": info.get("fps"),
                                  "robot_type": info.get("robot_type"),
                                  "codebase_version": info.get("codebase_version"),
                                  "selected_episodes": list(SO100_EPISODES)}
    print(f"      源：{info.get('repo_id')} / {info.get('robot_type')} / {info.get('fps')} Hz，"
          f"选取 {EXPECTED_EPISODES} 条", flush=True)

    imported = run("import", lambda: business.start_import(SOURCE_ROOT, "so100", BATCH_LABEL))
    record["batch_id"] = imported["batch_id"]
    save()
    batch = current()
    assert batch["total_episodes"] == EXPECTED_EPISODES, batch["total_episodes"]
    expected_rows = batch["total_rows"]
    record["expected_rows"] = expected_rows
    save()
    print(f"      批次 {batch['batch_id']}：{batch['total_episodes']} 条 / {expected_rows} 帧", flush=True)

    checks = []
    for ep in batch["episodes"]:
        for row in (0, ep["row_count"] // 2, ep["row_count"] - 1):
            preview = preview_batch(batch, ep["episode_index"], row)
            assert preview["image"].shape == IMAGE_SHAPE, preview["image"].shape
            assert not preview["issues"], preview["issues"]
            assert len(preview["state_names"]) == 6 and len(preview["action_names"]) == 6
            checks.append({"episode": ep["episode_index"], "row": row, "image_shape": list(IMAGE_SHAPE)})
    record["checks"]["raw_preview"] = checks
    print(f"      原始预览：{len(checks)} 个采样位置全部通过（{IMAGE_SHAPE[1]}×{IMAGE_SHAPE[0]}）", flush=True)

    quality = run("quality", lambda: business.start_batch_quality(batch["batch_id"], current()["revision"]))
    from robodata.quality import RULES
    assert quality["summary"]["coverage_performed"] == EXPECTED_EPISODES * len(RULES), quality["summary"]
    assert quality["summary"]["issue_count"] == 0, quality["summary"]
    record["checks"]["input_quality"] = quality["summary"]
    print(f"      质检：{quality['summary']['coverage_performed']} 项覆盖 / 0 问题", flush=True)

    if not record.get("review_completed"):
        b = current()
        b = store.confirm_cleaning(b["batch_id"], expected_revision=b["revision"])
        kept = [e["episode_index"] for e in b["episodes"] if e["disposition"] == "keep"]
        assert kept == list(SO100_EPISODES), kept
        for index in kept:
            b = store.save_annotation(b["batch_id"], index, task="SO-100：拾取糖果",
                                      outcome="failure" if index == kept[0] else "uncertain",
                                      tags=["SO-100 验收标注"],
                                      notes="人工流程标签，不作为源机器人成功率结论；原始任务为 red sweet pick。",
                                      expected_revision=b["revision"])
            b = store.submit_annotation(b["batch_id"], index, expected_revision=b["revision"])
            if index == kept[0]:
                b = store.review_annotation(b["batch_id"], index, approve=False,
                                            reason="补充该执行结果是验收标签的说明",
                                            expected_revision=b["revision"])
                assert b["episodes"][index]["review"]["status"] == "returned"
                b = store.save_annotation(b["batch_id"], index, task="SO-100：拾取糖果", outcome="failure",
                                          tags=["SO-100 验收标注"],
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
    assert source_indices == list(SO100_EPISODES), source_indices
    record["checks"]["selection"] = {"source": source_indices, "frames": expected_rows}
    print(f"      候选映射：源 {source_indices} → 输出 0–{len(source_indices) - 1}", flush=True)

    converted = run("conversion", lambda: business.start_batch_conversion(b["batch_id"], current()["revision"]))
    summary = converted["summary"]
    assert summary["row_count"] == expected_rows, summary
    record["checks"]["conversion"] = summary
    print(f"      转换：{summary['coverage_performed']} 项输出质检 / {summary['row_count']} 帧", flush=True)

    b = current()
    output = next(o for o in b["outputs"] if o["run_id"] == converted["run_id"])
    record["output_id"] = output["output_id"]
    record["output_path"] = output["artifact_path"]
    conversion_dir = PROJECT_ROOT / "work/runs" / converted["run_id"]

    # SO-100 转换链路的阶段证据：v2.0 → v2.1 元数据补齐，再逐阶段转 v3.0。
    upgrade = json.loads((conversion_dir / "steps/metadata_upgrade.json").read_text(encoding="utf-8"))
    assert upgrade["status"] == "passed", upgrade.get("diagnostic")
    record["checks"]["metadata_upgrade"] = upgrade["details"]
    print(f"      元数据补齐：{upgrade['details'].get('episode_count')} 条 per-episode 统计写入", flush=True)

    official = json.loads((conversion_dir / "steps/official_verify.json").read_text(encoding="utf-8"))
    assert official["status"] == "passed"
    assert official["details"]["offline"] is True
    assert official["details"]["checked_numeric_frames"] == expected_rows, official["details"]
    record["checks"]["official_verify"] = official["details"]
    print(f"      官方验证：离线加载通过，{official['details']['checked_numeric_frames']} 帧数值比对", flush=True)

    root = Path(output["artifact_path"])
    metadata = load_metadata(root)
    assert metadata["episode_indices"] == list(range(EXPECTED_EPISODES)), metadata["episode_indices"]
    assert metadata["info"]["total_frames"] == expected_rows, metadata["info"]["total_frames"]
    record["checks"]["output_metadata"] = {"episodes": metadata["episode_indices"],
                                           "total_frames": metadata["info"]["total_frames"],
                                           "fps": metadata["info"]["fps"],
                                           "camera": metadata["source"].get("camera")}
    print(f"      输出元数据：{len(metadata['episode_indices'])} 条 / {metadata['info']['total_frames']} 帧 / "
          f"{metadata['info']['fps']} Hz", flush=True)

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
    assert verified["row_count"] == expected_rows and verified["episode_count"] == EXPECTED_EPISODES, verified
    with zipfile.ZipFile(z) as archive:
        for name in archive.namelist():
            assert not name.startswith(("/", "\\")) and ".." not in Path(name).parts
    record["checks"]["delivery"] = {"zip": z.name, "row_count": verified["row_count"],
                                    "episode_count": verified["episode_count"], "size_bytes": z.stat().st_size}
    print(f"      交付：{z.name}（{z.stat().st_size} 字节）", flush=True)

    # 交付包的官方离线加载属于交付流程的最后一个阶段，不在转换运行目录里。
    delivery_dir = PROJECT_ROOT / "work/runs" / delivered["run_id"]
    archive_verify = json.loads((delivery_dir / "steps/archive_verify.json").read_text(encoding="utf-8"))
    assert archive_verify["status"] == "passed", archive_verify.get("diagnostic")
    assert archive_verify["details"]["row_count"] == expected_rows, archive_verify["details"]
    record["checks"]["archive_verify"] = archive_verify["details"]
    print(f"      交付包验证：解包后官方离线加载通过", flush=True)

    record["summary"] = {"source": "SO-100（jmrog/so100_sweet_pick）", "app_version": APP_VERSION,
                         "source_format": "LeRobot v2.0", "output_format": "LeRobot v3.0",
                         "episodes": EXPECTED_EPISODES, "rows": expected_rows,
                         "selected_episodes": list(SO100_EPISODES),
                         "conversion_path": "v2.0 → v2.1（补齐 per-episode 统计）→ v3.0（逐阶段转换）",
                         "source_readonly": True,
                         "note": "官方 convert_dataset() 会删除并原地替换源数据集，平台只调用其阶段函数，源数据始终只读。"}
    save()
    print(f"\nSO-100 八节点验收通过：{EXPECTED_EPISODES} 条 / {expected_rows} 帧")
    print(f"证据：{EVIDENCE}")


if __name__ == "__main__":
    main()
