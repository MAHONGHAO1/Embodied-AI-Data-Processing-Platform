"""Collect actual local evidence; runtime inputs and paths stay out of Git."""
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robodata.config import PROJECT_ROOT
from robodata.source import file_sha256


def main():
    work = PROJECT_ROOT / "work"
    release = json.loads((work / "eight-node-release-acceptance.json").read_text(encoding="utf-8"))
    failures = json.loads((work / "v04-failure-acceptance.json").read_text(encoding="utf-8"))
    runs = {"eight-node-success": release["run_id"],
            "hdf5-isolation": failures["runs"]["fault_quality"],
            "so100-anomalies": failures["runs"]["so100_anomalies_quality"],
            "missing-video": failures["runs"]["so100_missing_video_quality"],
            "tampered-zip": failures["runs"]["tampered_zip"]}
    target = work / "examples-v04"
    manifest = []
    for name, run_id in runs.items():
        source = work / "runs" / run_id
        output = target / name
        for pattern in ("request.json", "state.json", "events.jsonl", "diagnostic.json",
                        "reports/*", "steps/*.json", "registrations/*.json", "requests/*.json"):
            for item in source.glob(pattern):
                if item.is_file():
                    dest = output / item.relative_to(source)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
                    manifest.append(dict(path=dest.relative_to(target).as_posix(), run_id=run_id,
                                         sha256=file_sha256(dest), size=dest.stat().st_size))
    target.mkdir(parents=True, exist_ok=True)
    (target / "index.json").write_text(json.dumps(dict(runs=runs, files=manifest), ensure_ascii=False, indent=2), encoding="utf-8")
    evidence = PROJECT_ROOT.parents[1] / "02_过程文件" / "V0.4_验收证据"
    evidence.mkdir(parents=True, exist_ok=True)
    for name in ("eight-node-acceptance.json", "eight-node-release-acceptance.json", "v04-acceptance.json",
                 "v04-failure-acceptance.json", "v04-browser-acceptance.json", "v04-tests.xml",
                 "v04-tests.log", "v04-source-manifest.json"):
        if (work / name).is_file():
            shutil.copy2(work / name, evidence / name)
    shutil.copytree(target, evidence / "示例日志与报告", dirs_exist_ok=True)
    print(target)
    print(evidence)


if __name__ == "__main__":
    main()
