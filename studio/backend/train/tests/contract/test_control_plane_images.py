from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_control_plane_images_include_aliyun_runtime_dependencies():
    for relative_path in ("apps/api/Dockerfile", "services/scheduler/Dockerfile"):
        dockerfile = (ROOT / relative_path).read_text()
        assert ".[postgres,aliyun]" in dockerfile


def test_production_compose_uses_quota_safe_dlc_defaults():
    compose = (ROOT / "infra/compose/docker-compose.production.example.yml").read_text()
    assert "ALIYUN_DLC_GPU_TYPE: ${ALIYUN_DLC_GPU_TYPE:-}" in compose
    assert "ALIYUN_DLC_ACCESSIBILITY: ${ALIYUN_DLC_ACCESSIBILITY:-PUBLIC}" in compose
    assert "pai-dlc-vpc.cn-beijing.aliyuncs.com" in compose
    assert compose.count("QUICTRAIN_ARTIFACT_ROOT: ${QUICTRAIN_ARTIFACT_ROOT}") == 2
    assert compose.count("ALIYUN_OSS_ENDPOINT: ${ALIYUN_OSS_ENDPOINT}") == 2
