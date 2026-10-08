"""Pool → ResourceId mapping, DLC capacities, and idle-stop defaults."""

from quictrain_model_specs import MODEL_REGISTRY

from quictrain_api.dsw_ops import parse_known_dsw_instances
from quictrain_api.ops import idle_resource_stop_policy, may_stop_idle_resource
from quictrain_api.pools import (
    match_resource_profile,
    pool_capacity_map,
    resolve_pool_resource_id,
    scale_worker_resources,
)


def test_quota4090_and_h20_bind_dlc_quotas(monkeypatch):
    monkeypatch.delenv("ALIYUN_DLC_RESOURCE_ID", raising=False)
    monkeypatch.setenv("QUICTRAIN_POOL_RESOURCE_IDS", "")
    monkeypatch.setenv("QUICTRAIN_POOL_CAPACITIES", "")
    from quictrain_api.settings import get_settings

    get_settings.cache_clear()
    assert resolve_pool_resource_id("cn-beijing-4090") == "quota87wrnuka7bx"
    assert resolve_pool_resource_id("cn-beijing-h20-8") == "quota1lnd98qodrh"
    assert pool_capacity_map()["cn-beijing-4090"] == 8
    assert pool_capacity_map()["cn-beijing-h20-8"] == 40
    matched = match_resource_profile(
        profile_id="act-4090-x4",
        provider_id="aliyun_dlc",
        pool_id="cn-beijing-4090",
        gpu_count=4,
        selectable=True,
        gpu_models=["RTX 4090"],
    )
    assert matched["ok"] is True
    assert matched["resource_id"] == "quota87wrnuka7bx"


def test_scale_worker_resources_for_4090_and_h20():
    scaled_4090 = scale_worker_resources(8, gpu_model="RTX 4090")
    assert scaled_4090["gpu_count"] == 8
    assert scaled_4090["cpu"] == 64
    assert scaled_4090["memory"] == "512Gi"
    assert scaled_4090["shared_memory"] == "128Gi"
    scaled_h20 = scale_worker_resources(2, gpu_model="H20")
    assert scaled_h20["cpu"] == 16
    assert scaled_h20["memory"] == "128Gi"
    scaled_pi05_4090 = scale_worker_resources(1, gpu_model="RTX 4090", model_id="pi05")
    assert scaled_pi05_4090["memory"] == "128Gi"
    assert scaled_pi05_4090["shared_memory"] == "32Gi"


def test_act_profiles_prefer_dlc_4090_and_h20_open_x4():
    act_profiles = {
        profile.id: profile
        for recipe in MODEL_REGISTRY["act"].recipes
        for profile in recipe.resource_profiles
    }
    assert act_profiles["act-4090-standard"].selectable is True
    assert act_profiles["act-4090-x4"].selectable is True
    assert act_profiles["act-4090-x8"].selectable is False
    assert act_profiles["act-h20-standard"].selectable is True
    assert act_profiles["act-h20-x4"].selectable is True
    assert act_profiles["act-h20-x8"].selectable is False
    assert act_profiles["act-4090-standard"].provider_id == "aliyun_dlc"
    assert act_profiles["act-h20-standard"].provider_id == "aliyun_dlc"


def test_known_dsw_catalog_includes_4090_pair(monkeypatch):
    monkeypatch.delenv("QUICTRAIN_DSW_KNOWN_INSTANCES", raising=False)
    from quictrain_api.settings import get_settings

    get_settings.cache_clear()
    known = {item["instance_id"]: item for item in parse_known_dsw_instances()}
    assert known["dsw-fr1y76cr1n67t9vx7n"]["family"] == "4090"
    assert known["dsw-if7gmtyy8qf0vep4jt"]["family"] == "4090"
    assert known["dsw-n1n8o1usk465z51gam"]["family"] == "h20"


def test_idle_resource_stop_defaults_to_never(monkeypatch):
    monkeypatch.delenv("QUICTRAIN_AUTO_STOP_IDLE_RESOURCES", raising=False)
    monkeypatch.delenv("QUICTRAIN_AUTO_STOP_RESOURCE_GROUPS", raising=False)
    monkeypatch.delenv("QUICTRAIN_PROTECT_H20_DSW", raising=False)
    monkeypatch.delenv("QUICTRAIN_PROTECT_H20_RESOURCE_GROUPS", raising=False)
    from quictrain_api.settings import get_settings

    get_settings.cache_clear()
    policy = idle_resource_stop_policy()
    assert policy["auto_stop_idle_resources"] is False
    assert policy["auto_stop_resource_groups"] is False
    assert policy["protect_h20_dsw"] is True
    assert policy["protect_h20_resource_groups"] is True
    ok, reason = may_stop_idle_resource(idle_minutes=120)
    assert ok is False
    assert "disabled" in reason
    ok_h20, reason_h20 = may_stop_idle_resource(
        idle_minutes=999, resource_group=True, family="h20", resource_id="quota1lnd98qodrh"
    )
    assert ok_h20 is False
    assert "H20" in reason_h20
