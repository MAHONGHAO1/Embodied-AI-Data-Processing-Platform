"""Embodied VL SDK adapter for behavior suggestion inference."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

from data.config import _is_public_oss_browser_endpoint
from data.integrations.embodied_vl.client_loader import load_embodied_vl_client

_MAX_RESPONSE_TEXT_CHARS = 512 * 1024
_MAX_BEHAVIOR_SEGMENTS = 256
_MAX_DESCRIPTION_CHARS = 2048
_JSON_CODE_FENCE_RE = re.compile(
    r"\A\s*```(?:json)?[ \t]*\r?\n(?P<payload>.*?)\r?\n?```\s*\Z",
    flags=re.IGNORECASE | re.DOTALL,
)
_DEFAULT_CLIENT_USER_ID = "quicdata"
_DEFAULT_CLIENT_DEVICE_UUID = "quicdata-ai-worker"
_DEFAULT_VLA_ANNO_VERSION = "vla-anno#A2FM#AT9J"
_SDK_ENVIRONMENT_NAMES = (
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_APP_ID",
    "DASHSCOPE_USER_ID",
    "DASHSCOPE_DEVICE_UUID",
    "VLA_ANNO_VERSION",
)
_SDK_ENVIRONMENT_LOCK = threading.RLock()


class BehaviorAiProviderError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, repr=False)
class ProviderSuggestionSegment:
    start_frame: int
    end_frame: int
    description: str
    confidence: float | None


@dataclass(frozen=True, repr=False)
class ProviderSuggestionResult:
    segments: tuple[ProviderSuggestionSegment, ...]


class EmbodiedVlBehaviorSuggestionProvider:
    """Send a short-lived public HTTPS preview URL through the native SDK."""

    def __init__(
        self,
        *,
        config: Any,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or self._build_sdk_client

    def suggest(
        self,
        preview_url: str,
        *,
        correlation_id: str,
    ) -> ProviderSuggestionResult:
        _ = correlation_id
        _require_signed_preview_url(preview_url)

        try:
            with _sdk_environment(self._config):
                client = self._client_factory()
                response = client.chat.completions.create(
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "video_url",
                                    "video_url": {"url": preview_url},
                                }
                            ],
                        }
                    ]
                )
        except BehaviorAiProviderError:
            raise
        except Exception:
            raise BehaviorAiProviderError("ai_provider_unavailable") from None

        try:
            return parse_behavior_segments(_response_content(response))
        except BehaviorAiProviderError:
            raise
        except Exception:
            raise BehaviorAiProviderError("ai_provider_invalid_response") from None

    def _build_sdk_client(self) -> Any:
        module = load_embodied_vl_client(
            self._config.embodied_vl_sdk_dir,
            self._config.embodied_vl_checksums_file,
        )
        return module.EmbodiedVLClient()


@contextmanager
def _sdk_environment(config: Any) -> Iterator[None]:
    values = {
        "DASHSCOPE_API_KEY": _required_config_value(config, "dashscope_api_key"),
        "DASHSCOPE_APP_ID": _required_config_value(config, "dashscope_app_id"),
        "DASHSCOPE_USER_ID": _optional_config_value(
            config,
            "dashscope_user_id",
            _DEFAULT_CLIENT_USER_ID,
        ),
        "DASHSCOPE_DEVICE_UUID": _optional_config_value(
            config,
            "dashscope_device_uuid",
            _DEFAULT_CLIENT_DEVICE_UUID,
        ),
        "VLA_ANNO_VERSION": _optional_config_value(
            config,
            "vla_anno_version",
            _DEFAULT_VLA_ANNO_VERSION,
        ),
    }
    with _SDK_ENVIRONMENT_LOCK:
        previous = {name: os.environ.get(name) for name in _SDK_ENVIRONMENT_NAMES}
        try:
            os.environ.update(values)
            yield
        finally:
            for name, previous_value in previous.items():
                if previous_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous_value


def _required_config_value(config: Any, name: str) -> str:
    value = getattr(config, name, "")
    if not isinstance(value, str) or not value.strip():
        raise BehaviorAiProviderError("ai_provider_unavailable")
    return value.strip()


def _optional_config_value(config: Any, name: str, fallback: str) -> str:
    value = getattr(config, name, "")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _require_signed_preview_url(preview_url: str) -> None:
    if not isinstance(preview_url, str) or not 1 <= len(preview_url) <= 8192:
        raise BehaviorAiProviderError("ai_provider_invalid_input")
    try:
        parsed = urlsplit(preview_url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        raise BehaviorAiProviderError("ai_provider_invalid_input") from None
    if (
        not parsed.query
        or parsed.fragment
        or not parsed.path
        or parsed.path == "/"
        or parsed.username is not None
        or parsed.password is not None
        or not _is_public_oss_browser_endpoint(endpoint)
    ):
        raise BehaviorAiProviderError("ai_provider_invalid_input")


def _response_content(response: Any) -> str:
    try:
        choices = response.choices
        if not isinstance(choices, (list, tuple)) or not choices:
            raise ValueError("missing choices")
        first_choice = choices[0]
        finish_reason = first_choice.finish_reason
        if finish_reason == "error":
            raise BehaviorAiProviderError("ai_provider_unavailable")
        if not isinstance(finish_reason, str) or not finish_reason:
            raise ValueError("invalid finish reason")
        content = first_choice.message.content
    except BehaviorAiProviderError:
        raise
    except Exception:
        raise BehaviorAiProviderError("ai_provider_invalid_response") from None
    if not isinstance(content, str) or len(content) > _MAX_RESPONSE_TEXT_CHARS:
        raise BehaviorAiProviderError("ai_provider_invalid_response")
    return content


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _load_json_strict(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_json_constant)


def _unwrap_complete_json_code_fence(text: str) -> str:
    match = _JSON_CODE_FENCE_RE.fullmatch(text)
    return match.group("payload") if match else text


def parse_behavior_segments(text: str) -> ProviderSuggestionResult:
    if not isinstance(text, str) or len(text) > _MAX_RESPONSE_TEXT_CHARS:
        raise BehaviorAiProviderError("ai_provider_invalid_response")
    try:
        payload = _load_json_strict(_unwrap_complete_json_code_fence(text))
    except (TypeError, ValueError, RecursionError):
        raise BehaviorAiProviderError("ai_provider_invalid_response") from None
    if not isinstance(payload, list) or len(payload) > _MAX_BEHAVIOR_SEGMENTS:
        raise BehaviorAiProviderError("ai_provider_invalid_response")

    segments: list[ProviderSuggestionSegment] = []
    for item in payload:
        if not isinstance(item, dict):
            raise BehaviorAiProviderError("ai_provider_invalid_response")
        start_frame = item.get("start_frame")
        end_frame = item.get("end_frame")
        description = item.get("description")
        confidence = item.get("confidence")
        confidence_value: float | None = None
        if confidence is not None and not isinstance(confidence, bool):
            try:
                confidence_value = float(confidence)
            except (OverflowError, TypeError, ValueError):
                raise BehaviorAiProviderError("ai_provider_invalid_response") from None
        if (
            isinstance(start_frame, bool)
            or not isinstance(start_frame, int)
            or isinstance(end_frame, bool)
            or not isinstance(end_frame, int)
            or start_frame < 0
            or end_frame < start_frame
            or not isinstance(description, str)
            or not description.strip()
            or len(description) > _MAX_DESCRIPTION_CHARS
            or isinstance(confidence, bool)
            or (confidence is not None and not isinstance(confidence, (int, float)))
            or (
                confidence_value is not None
                and (not isfinite(confidence_value) or not 0.0 <= confidence_value <= 1.0)
            )
        ):
            raise BehaviorAiProviderError("ai_provider_invalid_response")
        segments.append(
            ProviderSuggestionSegment(
                start_frame=start_frame,
                end_frame=end_frame,
                description=description.strip(),
                confidence=confidence_value,
            )
        )
    return ProviderSuggestionResult(segments=tuple(segments))
