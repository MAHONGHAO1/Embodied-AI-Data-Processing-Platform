"""Recheck and capture evidence from an existing completed eight-node run."""
import argparse
import json
from pathlib import Path
import sys
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robodata import business
from robodata.config import APP_VERSION, PROJECT_ROOT
from robodata.delivery import verified_delivery
from robodata.runtime import get_run
from robodata.source import file_sha256
from robodata.sources import HDF5_PATH, HDF5_SHA256
from robodata.workflow import get_flow
from robodata.dataset import load_episode, load_metadata
from robodata.video import read_video_frame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    args = parser.parse_args()
    state = get_run(args.run_id)
    assert state["status"] == "completed" and state["outcome"] == "passed"
    batch = business.get_store().get_batch(state["batch_id"])
    final = business.get_final_quality(batch["batch_id"])
    assert final["current"] and final["accepted"] and final["evidence_valid"]
    run = PROJECT_ROOT / "work/runs" / args.run_id
    for registration in (run / "registrations").glob("*.json"):
        saved = json.loads(registration.read_text(encoding="utf-8"))
        for name, digest in saved["evidence_sha256"].items():
            assert file_sha256(run / name) == digest, (registration, name)
    official = json.loads((run / "steps/official_verify.json").read_text(encoding="utf-8"))
    assert official["status"] == "passed"
    details = official["details"]
    assert details["checked_numeric_frames"] == 116 and details["sample_count"] == 6
    assert state["summary"]["coverage_performed"] == state["summary"]["coverage_total"] == 22
    delivery = next(d for d in batch["deliveries"] if d["packaging_run_id"] == args.run_id)
    verified = verified_delivery(batch, delivery["delivery_id"])
    assert verified["row_count"] == 116 and verified["episode_count"] == 2
    output = next(o for o in batch["outputs"] if o["run_id"] == args.run_id)
    output_root = Path(output["artifact_path"])
    assert load_metadata(output_root)["info"]["splits"] == {"train": "0:2"}
    matches = []
    with np.load(run / "decoded_samples.npz") as decoded:
        for i, (episode, row) in enumerate(zip(decoded["episode_indices"], decoded["frame_indices"])):
            ep = load_episode(output_root, int(episode))
            frame = read_video_frame(ep.video_path, float(ep.table.iloc[int(row)]["timestamp"]) + ep.video_start_time)
            np.testing.assert_array_equal(frame["image"], decoded["images"][i])
            matches.append(dict(episode=int(episode), row=int(row), video_start_time=ep.video_start_time,
                                official_pixels_equal=True))
    assert len(matches) == 6 and any(m["episode"] == 1 and m["video_start_time"] == 2.95 for m in matches)
    flow = get_flow(batch)
    assert all(n["status"] == "completed" for n in flow["nodes"])
    assert file_sha256(HDF5_PATH) == HDF5_SHA256
    record = dict(status="passed", app_version=APP_VERSION, run_id=args.run_id,
                  batch_id=batch["batch_id"], content_version=batch["content_version"],
                  final_quality=final, official_verification=official, summary=state["summary"],
                  delivery=verified, workflow=flow, output_frames=matches, raw_unchanged=True)
    target = PROJECT_ROOT / "work/eight-node-release-acceptance.json"
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS", target)


if __name__ == "__main__":
    main()
