import pytest
from quictrain_config import ActConfig, Pi05Config, canonical_hash, resolve_config


def test_schema_contains_quic_ui_metadata():
    schema = Pi05Config.model_json_schema()
    steps = schema["$defs"]["TrainingConfig"]["properties"]["steps"]
    assert steps["x-quic"]["group"] == "training"
    assert steps["x-quic"]["level"] == 0


def test_pi05_defaults_match_h20_acceptance():
    config = Pi05Config()
    assert config.training.batch_size == 1
    assert config.action_expert_only is True
    assert config.training.gradient_checkpointing is True


def test_act_defaults_are_offline_and_oss_mount_safe():
    config = ActConfig()

    assert config.pretrained_backbone_weights is None
    assert config.training.num_workers == 0


def test_layered_override_and_diff():
    resolved, diff = resolve_config(
        "pi05",
        {"training.batch_size": 1, "optimizer.learning_rate": 0.0001},
        preset={"training.steps": 12000},
    )
    assert resolved["training"]["steps"] == 12000
    assert resolved["training"]["batch_size"] == 1
    assert {item["path"] for item in diff} == {
        "training.batch_size",
        "optimizer.learning_rate",
    }


def test_system_field_override_is_forbidden():
    with pytest.raises(PermissionError):
        resolve_config("pi05", {"runtime.output_uri": "file:///tmp/escape"})


def test_unknown_override_is_rejected():
    with pytest.raises(KeyError):
        resolve_config("act", {"training.shell_command": "rm -rf /"})


def test_short_smoke_runs_scale_the_implicit_warmup_default():
    resolved, _ = resolve_config("act", {"training.steps": 100})

    assert resolved["optimizer"]["warmup_steps"] == 10

    with pytest.raises(ValueError, match="warmup_steps"):
        resolve_config(
            "act",
            {"training.steps": 100, "optimizer.warmup_steps": 100},
        )


def test_canonical_hash_is_order_independent():
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
