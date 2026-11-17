"""The 100-step smoke must actually optimize, not just run without crashing."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = spec_from_file_location("train_smoke", ROOT / "scripts" / "train_smoke.py")
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_synthetic_linear_probe_reduces_loss_in_100_steps() -> None:
    script = _load_script()
    state, action = script.synthetic_rows(seed=7, rows=512)
    result = script.train_linear_probe(state, action, steps=100, batch_size=64)

    assert result["steps"] == 100
    assert result["finite"] is True
    assert result["final_mse"] < result["initial_mse"]
    assert result["loss_ratio"] is not None and result["loss_ratio"] < 0.5
