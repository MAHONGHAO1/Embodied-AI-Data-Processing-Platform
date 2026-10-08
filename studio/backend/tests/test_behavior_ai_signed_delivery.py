from __future__ import annotations

from pathlib import Path

import pytest

from data.services.platform_settings import BehaviorAiRuntimeConfig


def _runtime() -> BehaviorAiRuntimeConfig:
    return BehaviorAiRuntimeConfig(
        enabled=True,
        outbound_enabled=True,
        signed_url_delivery_enabled=True,
        public_endpoint="https://oss-cn-beijing.aliyuncs.com",
        signed_url_ttl_seconds=600,
        api_key="api-key",
        app_id="app-id",
        provider_user_id="quicdata",
        provider_device_uuid="worker",
        annotation_version="v1",
        sdk_dir="/not-used",
        checksums_file="/not-used/checksums.sha256",
    )


def test_behavior_provider_accepts_only_public_https_url():
    from data.integrations.embodied_vl.behavior_suggestion import (
        BehaviorAiProviderError,
        EmbodiedVlBehaviorSuggestionProvider,
    )

    captured = {}

    class Completions:
        def create(self, *, messages):
            captured["url"] = messages[0]["content"][0]["video_url"]["url"]
            message = type(
                "Message",
                (),
                {"content": '[{"start_frame":0,"end_frame":1,"description":"close box"}]'},
            )()
            choice = type("Choice", (), {"finish_reason": "stop", "message": message})()
            return type("Response", (), {"choices": [choice]})()

    client = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    provider = EmbodiedVlBehaviorSuggestionProvider(
        config=_runtime(), client_factory=lambda: client
    )

    result = provider.suggest(
        "https://quic-process-qrdf.oss-cn-beijing.aliyuncs.com/ai-inputs/v1/episodes/1/a.mp4?signature=secret",
        correlation_id="job-1",
    )

    assert captured["url"].startswith("https://")
    assert len(result.segments) == 1
    with pytest.raises(BehaviorAiProviderError, match="ai_provider_invalid_input"):
        provider.suggest(Path("/tmp/local-preview.mp4"), correlation_id="job-2")


def test_ai_input_delivery_uses_server_side_copy_and_never_returns_storage_uri(monkeypatch):
    from data.infra import oss_client
    from data.services.behavior_ai_delivery import issue_behavior_ai_input_url

    calls = []
    monkeypatch.setattr(oss_client, "is_oss_configured", lambda: True)
    monkeypatch.setattr(oss_client, "cloud_enabled", lambda: True)
    monkeypatch.setattr(oss_client, "bucket_name", lambda _role: "process-bucket")
    monkeypatch.setattr(oss_client, "browser_direct_object_allowed", lambda _bucket, _key: True)

    def matching_info(_bucket, key):
        if key.startswith("ai-inputs/") and not calls:
            return None
        return type(
            "Info",
            (),
            {"size": 1024, "etag": "etag-1", "version_id": None},
        )()

    monkeypatch.setattr(oss_client, "object_info", matching_info)
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "oss://process/ai-input.mp4",
    )
    monkeypatch.setattr(
        oss_client,
        "sign_ai_input_get_url",
        lambda bucket, key, **_kwargs: (
            f"https://{bucket}.oss-cn-beijing.aliyuncs.com/{key}?signature=secret"
        ),
    )

    url = issue_behavior_ai_input_url(
        storage_uri="oss://process-bucket/process/v1/episodes/7/preview.mp4",
        episode_id=7,
        preview_fingerprint=f"sha256:{'a' * 64}",
        runtime=_runtime(),
    )

    assert len(calls) == 1
    assert calls[0][0][:3] == (
        "process-bucket",
        "process/v1/episodes/7/preview.mp4",
        "process-bucket",
    )
    assert url.startswith("https://")
    assert "oss://" not in url


def test_ai_input_delivery_rejects_a_mismatched_existing_object(monkeypatch):
    from data.infra import oss_client
    from data.services.behavior_ai_delivery import (
        BehaviorAiDeliveryError,
        issue_behavior_ai_input_url,
    )

    destination_key = f"ai-inputs/v1/episodes/7/{'a' * 64}.mp4"
    monkeypatch.setattr(oss_client, "is_oss_configured", lambda: True)
    monkeypatch.setattr(oss_client, "cloud_enabled", lambda: True)
    monkeypatch.setattr(oss_client, "bucket_name", lambda _role: "process-bucket")
    monkeypatch.setattr(oss_client, "browser_direct_object_allowed", lambda _bucket, _key: True)

    def object_info(_bucket, key):
        if key == destination_key:
            return type("Info", (), {"size": 7, "etag": "wrong-etag", "version_id": None})()
        return type("Info", (), {"size": 1024, "etag": "source-etag", "version_id": None})()

    monkeypatch.setattr(oss_client, "object_info", object_info)
    monkeypatch.setattr(oss_client, "copy_object", lambda *_args, **_kwargs: None)

    with pytest.raises(BehaviorAiDeliveryError, match="ai_preview_identity_mismatch"):
        issue_behavior_ai_input_url(
            storage_uri="oss://process-bucket/process/v1/episodes/7/preview.mp4",
            episode_id=7,
            preview_fingerprint=f"sha256:{'a' * 64}",
            runtime=_runtime(),
        )


def test_ai_input_delivery_rejects_preview_over_100_mib_before_copy(monkeypatch):
    from data.infra import oss_client
    from data.services.behavior_ai_delivery import (
        BehaviorAiDeliveryError,
        issue_behavior_ai_input_url,
    )

    monkeypatch.setattr(oss_client, "is_oss_configured", lambda: True)
    monkeypatch.setattr(oss_client, "cloud_enabled", lambda: True)
    monkeypatch.setattr(oss_client, "bucket_name", lambda _role: "process-bucket")
    monkeypatch.setattr(oss_client, "browser_direct_object_allowed", lambda _bucket, _key: True)
    monkeypatch.setattr(
        oss_client,
        "object_info",
        lambda _bucket, _key: type(
            "Info",
            (),
            {"size": 100 * 1024 * 1024 + 1, "etag": "source-etag", "version_id": None},
        )(),
    )
    monkeypatch.setattr(
        oss_client,
        "copy_object",
        lambda *_args, **_kwargs: pytest.fail("oversized input must not be copied"),
    )
    monkeypatch.setattr(
        oss_client,
        "sign_ai_input_get_url",
        lambda *_args, **_kwargs: pytest.fail("oversized input must not be signed"),
    )

    with pytest.raises(BehaviorAiDeliveryError, match="ai_preview_too_large"):
        issue_behavior_ai_input_url(
            storage_uri="oss://process-bucket/process/v1/episodes/7/preview.mp4",
            episode_id=7,
            preview_fingerprint=f"sha256:{'a' * 64}",
            runtime=_runtime(),
        )


def test_ai_input_delivery_rejects_local_preview_without_uploading_from_ecs(monkeypatch):
    from data.infra import oss_client
    from data.services import cloud_storage
    from data.services.behavior_ai_delivery import (
        BehaviorAiDeliveryError,
        issue_behavior_ai_input_url,
    )

    monkeypatch.setattr(oss_client, "is_oss_configured", lambda: True)
    monkeypatch.setattr(oss_client, "cloud_enabled", lambda: True)
    monkeypatch.setattr(
        cloud_storage,
        "materialize_for_processing",
        lambda *_args, **_kwargs: pytest.fail("AI delivery must not materialize a local preview"),
    )
    monkeypatch.setattr(
        oss_client,
        "upload_file",
        lambda *_args, **_kwargs: pytest.fail("AI delivery must not upload preview bytes from ECS"),
    )

    with pytest.raises(BehaviorAiDeliveryError, match="ai_preview_unavailable"):
        issue_behavior_ai_input_url(
            storage_uri="nas://process/process/v1/episodes/7/preview.mp4",
            episode_id=7,
            preview_fingerprint=f"sha256:{'a' * 64}",
            runtime=_runtime(),
        )
