"""Prepare a bounded AI preview and issue a short-lived provider URL."""

from __future__ import annotations

from data.infra import oss_client
from data.services.platform_settings import BehaviorAiRuntimeConfig
from data.utils.storage_uri import is_cloud_uri, parse_storage_uri


class BehaviorAiDeliveryError(RuntimeError):
    pass


_MAX_AI_PREVIEW_BYTES = 100 * 1024 * 1024


def _checksum(value: str) -> str:
    candidate = str(value or "").removeprefix("sha256:").lower()
    if len(candidate) != 64 or any(char not in "0123456789abcdef" for char in candidate):
        raise BehaviorAiDeliveryError("ai_preview_fingerprint_invalid")
    return candidate


def issue_behavior_ai_input_url(
    *,
    storage_uri: str,
    episode_id: int,
    preview_fingerprint: str,
    runtime: BehaviorAiRuntimeConfig,
) -> str:
    """Return an in-memory signed URL; never expose a local path to the SDK."""
    if (
        episode_id <= 0
        or not runtime.signed_url_delivery_enabled
        or not oss_client.cloud_enabled()
        or not oss_client.is_oss_configured()
    ):
        raise BehaviorAiDeliveryError("ai_signed_url_unavailable")
    digest = _checksum(preview_fingerprint)
    destination_bucket = oss_client.bucket_name("process")
    destination_key = f"ai-inputs/v1/episodes/{episode_id}/{digest}.mp4"
    expected_size = 0
    expected_etag = ""

    if not is_cloud_uri(storage_uri):
        raise BehaviorAiDeliveryError("ai_preview_unavailable")
    source_bucket, source_key = parse_storage_uri(storage_uri)
    if source_bucket != destination_bucket or not oss_client.browser_direct_object_allowed(
        source_bucket, source_key
    ):
        raise BehaviorAiDeliveryError("ai_preview_unavailable")
    source = oss_client.object_info(source_bucket, source_key)
    if source is None:
        raise BehaviorAiDeliveryError("ai_preview_unavailable")
    expected_size = int(source.size)
    expected_etag = str(source.etag or "")
    if expected_size <= 0:
        raise BehaviorAiDeliveryError("ai_preview_unavailable")
    if expected_size > _MAX_AI_PREVIEW_BYTES:
        raise BehaviorAiDeliveryError("ai_preview_too_large")
    if (
        source_key != destination_key
        and oss_client.object_info(destination_bucket, destination_key) is None
    ):
        try:
            oss_client.copy_object(
                source_bucket,
                source_key,
                destination_bucket,
                destination_key,
                source_etag=source.etag,
                source_version_id=source.version_id,
                forbid_overwrite=True,
            )
        except FileExistsError:
            # Another worker may have published the same content-addressed object.
            pass

    destination = oss_client.object_info(destination_bucket, destination_key)
    if destination is None:
        raise BehaviorAiDeliveryError("ai_signed_url_unavailable")
    if int(destination.size) != expected_size or (
        expected_etag and str(destination.etag or "") != expected_etag
    ):
        raise BehaviorAiDeliveryError("ai_preview_identity_mismatch")
    try:
        return oss_client.sign_ai_input_get_url(
            destination_bucket,
            destination_key,
            endpoint=runtime.public_endpoint,
            expires=runtime.signed_url_ttl_seconds,
        )
    except (RuntimeError, ValueError) as exc:
        raise BehaviorAiDeliveryError("ai_signed_url_unavailable") from exc
