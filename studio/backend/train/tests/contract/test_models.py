import pytest
from quictrain_model_specs import MODEL_REGISTRY, DatasetVersion, compatibility_issues


@pytest.mark.parametrize("model_id", ["act", "pi05"])
def test_model_manifest_contract(model_id: str):
    model = MODEL_REGISTRY[model_id]
    assert model.backend == "lerobot"
    assert model.upstream_ref and model.upstream_ref != "main"
    assert model.image_digest.startswith("sha256:")
    assert model.schema_hash.startswith("sha256:")
    assert model.recipes
    assert model.standard_metrics


def test_control_plane_does_not_import_runtime_dependencies():
    import sys

    forbidden = {"torch", "transformers", "lerobot"}
    assert forbidden.isdisjoint(sys.modules)


def test_lerobot_models_share_the_verified_immutable_runtime():
    act = MODEL_REGISTRY["act"]
    pi05 = MODEL_REGISTRY["pi05"]

    assert act.image_uri == pi05.image_uri
    assert act.image_uri == (
        "quic-robot-registry-vpc.cn-beijing.cr.aliyuncs.com/quicrobot/quictrain-lerobot-runtime"
    )
    assert act.image_digest == pi05.image_digest
    assert act.image_digest == (
        "sha256:385f4e8b44269ebb01b71408087beb4709a09c47988859ffce2089e80b922b08"
    )
    assert act.adapter_version == pi05.adapter_version == "0.3.0"
    assert "pending" not in act.image_digest


def test_pi05_h20_profile_is_enabled_after_acceptance():
    pi05 = MODEL_REGISTRY["pi05"]
    act = MODEL_REGISTRY["act"]
    assert pi05.version_id == "mv_pi05_20260717_cpfs"
    assert pi05.selectable is True
    assert pi05.version == act.version == "1.0.0"
    assert pi05.availability_message is None
    profiles = {
        profile.id: profile for recipe in pi05.recipes for profile in recipe.resource_profiles
    }
    assert profiles["pi05-h20-standard"].selectable is True
    assert profiles["pi05-h20-standard"].calibration_status == "BENCHMARKED"
    assert profiles["pi05-4090-standard"].selectable is True
    assert profiles["pi05-4090-debug"].selectable is False


def test_act_and_pi05_prefer_dlc_open_x4_close_x8():
    act_profiles = {
        profile.id: profile
        for recipe in MODEL_REGISTRY["act"].recipes
        for profile in recipe.resource_profiles
    }
    pi05_profiles = {
        profile.id: profile
        for recipe in MODEL_REGISTRY["pi05"].recipes
        for profile in recipe.resource_profiles
    }
    assert act_profiles["act-4090-standard"].selectable is True
    assert act_profiles["act-4090-x4"].selectable is True
    assert act_profiles["act-4090-x8"].selectable is False
    assert act_profiles["act-h20-standard"].selectable is True
    assert act_profiles["act-h20-x4"].selectable is True
    assert act_profiles["act-h20-x8"].selectable is False
    assert pi05_profiles["pi05-4090-standard"].selectable is True
    assert pi05_profiles["pi05-h20-x4"].selectable is True
    assert pi05_profiles["pi05-4090-x8"].selectable is False

    selectable_act = [
        profile.id
        for profile in MODEL_REGISTRY["act"].recipes[0].resource_profiles
        if profile.selectable and "LOCAL" not in profile.gpu_models
    ]
    assert "act-4090-standard" in selectable_act
    assert "act-h20-standard" in selectable_act
    assert "act-4090-x8" not in selectable_act

    selectable_pi05 = [
        profile.id
        for profile in MODEL_REGISTRY["pi05"].recipes[0].resource_profiles
        if profile.selectable and "LOCAL" not in profile.gpu_models
    ]
    assert "pi05-4090-standard" in selectable_pi05
    assert "pi05-h20-x4" in selectable_pi05
    assert "pi05-4090-x8" not in selectable_pi05


def test_dataset_blocker_is_structured():
    dataset = DatasetVersion(
        id="dsv_bad",
        dataset_id="bad",
        name="Bad fixture",
        version="1",
        format="qrdf",
        format_version="1",
        uri="oss://fixture/bad",
        checksum="sha256:bad",
        episodes=1,
        frames=1,
        duration_hours=0.01,
        fps=30,
        robot_type="x",
        camera_keys=["cam"],
        action_dim=7,
        state_dim=7,
    )
    issues = compatibility_issues(dataset, MODEL_REGISTRY["act"])
    assert issues and issues[0]["code"]
