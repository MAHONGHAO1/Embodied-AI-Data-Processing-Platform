from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "runtimes" / "lerobot" / "Dockerfile"
PATCH_DOCKERFILE = ROOT / "runtimes" / "lerobot" / "Dockerfile.patch"
BUILD_SCRIPT = ROOT / "runtimes" / "lerobot" / "build-image.sh"
PREPARE_SCRIPT = ROOT / "runtimes" / "lerobot" / "prepare-build-context.sh"


def test_lerobot_image_pins_gpu_base_and_upstream() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "docker.io/pytorch/pytorch:2.10.0-cuda12.8-cudnn9-runtime@sha256:" in dockerfile
    assert "1396b9fab7aecddd10006c33c47a487ffdcb54b4" in dockerfile
    assert "fe0b3079a517f0c60bdf325afc7832d81ae384bd1d022f4e802e7da8f73606dd" in dockerfile
    assert '"/opt/lerobot[pi]"' in dockerfile
    assert '"click==8.3.1"' in dockerfile
    assert "pip install --break-system-packages" in dockerfile
    assert "ARG PIP_INDEX_URL=https://pypi.org/simple" in dockerfile
    assert "pip check" in dockerfile
    assert "COPY runtimes/lerobot/.build/lerobot.tar.gz" in dockerfile


def test_lerobot_publish_target_is_immutable_versioned_tag() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "quicrobot/quictrain-lerobot-runtime" in script
    assert "v0.5.1-pytorch2.10.0-cu128-r3" in script
    assert ":latest" not in script
    assert "prepare-build-context.sh" in script
    assert "Dockerfile.patch" in script
    assert 'grep -q "runtimes/lerobot/.build/lerobot.tar.gz"' in script


def test_runtime_patch_inherits_the_published_r1_digest() -> None:
    dockerfile = PATCH_DOCKERFILE.read_text(encoding="utf-8")

    assert "sha256:7581c74724c61ae056f14d6b84f7277df97b6955a637e603e518457ac3e2dc57" in dockerfile
    assert "COPY src/quictrain_runner" in dockerfile
    assert "COPY runtimes/lerobot/quictrain_runtime_lerobot" in dockerfile
    assert "QUICTRAIN_WORK_ROOT=/tmp/quictrain" in dockerfile


def test_lerobot_build_context_verifies_source_checksum() -> None:
    script = PREPARE_SCRIPT.read_text(encoding="utf-8")

    assert "source_sha256" in script
    assert "checksum mismatch" in script
