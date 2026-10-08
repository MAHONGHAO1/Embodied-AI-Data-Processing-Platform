import runpy
import subprocess
from pathlib import Path

BOOTSTRAP_MODULE = runpy.run_path(
    Path(__file__).parents[2] / "scripts" / "bootstrap_cpfs_workspace.py"
)
BOOTSTRAP_COMMAND = BOOTSTRAP_MODULE["BOOTSTRAP_COMMAND"]
build_launch_spec = BOOTSTRAP_MODULE["build_launch_spec"]


def test_cpfs_bootstrap_requires_official_tokenizer(monkeypatch):
    monkeypatch.setenv("QUICTRAIN_CPFS_ROOT_URI", "bmcpfs://filesystem")
    monkeypatch.setenv("QUICTRAIN_CPFS_BOOTSTRAP_PUSHT_URI", "oss://fixture/pusht")
    monkeypatch.delenv("QUICTRAIN_PI05_TOKENIZER_SOURCE_URI", raising=False)

    spec = build_launch_spec(30)

    assert len(spec.mounts) == 2
    assert "MODEL_ASSET_MISSING: official PaliGemma tokenizer" in BOOTSTRAP_COMMAND
    assert "policy_preprocessor.upstream.json" in BOOTSTRAP_COMMAND
    assert "remove_disabled_relative_actions_processor" in BOOTSTRAP_COMMAND
    subprocess.run(["bash", "-n", "-c", BOOTSTRAP_COMMAND], check=True)


def test_cpfs_bootstrap_mounts_pre_staged_official_tokenizer(monkeypatch):
    monkeypatch.setenv("QUICTRAIN_CPFS_ROOT_URI", "bmcpfs://filesystem")
    monkeypatch.setenv("QUICTRAIN_CPFS_BOOTSTRAP_PUSHT_URI", "oss://fixture/pusht")
    monkeypatch.setenv(
        "QUICTRAIN_PI05_TOKENIZER_SOURCE_URI",
        "oss://private-assets/google-paligemma-3b-pt-224-tokenizer",
    )

    spec = build_launch_spec(30)

    assert spec.mounts[2] == {
        "source_uri": "oss://private-assets/google-paligemma-3b-pt-224-tokenizer",
        "target": "/quictrain/source/paligemma-tokenizer",
        "read_only": "true",
    }
