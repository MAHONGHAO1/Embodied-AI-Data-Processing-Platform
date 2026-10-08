"""Check or append minimal OSS CORS rules for browser-direct platform access."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from data.security.browser_oss import BrowserCorsRequirement, browser_cors_requirements


def normalize_browser_origin(value: str, *, allow_http: bool) -> str:
    """Return one exact origin; paths, credentials and wildcards are forbidden."""
    candidate = str(value or "").strip()
    if not candidate or candidate == "*":
        raise ValueError("browser origin must be an exact HTTP(S) origin")
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("browser origin is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("browser origin must be an exact HTTP(S) origin")
    if parsed.scheme != "https" and not allow_http:
        raise ValueError("browser origin must use HTTPS unless --allow-http is explicit")
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    if not hostname or "*" in hostname:
        raise ValueError("browser origin must be an exact HTTP(S) origin")
    port = parsed.port
    if port in {80, 443} and (
        (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    ):
        port = None
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


def normalize_browser_origins(values: Sequence[str], *, allow_http: bool) -> tuple[str, ...]:
    """Normalize every configured frontend Origin and reject duplicate rules."""
    normalized = tuple(normalize_browser_origin(value, allow_http=allow_http) for value in values)
    if not normalized:
        raise ValueError("at least one browser origin is required")
    if len(set(normalized)) != len(normalized):
        raise ValueError("browser origins must not contain duplicates")
    return normalized


def cors_rule_matches_requirement(
    rule: Any,
    origin: str,
    requirement: BrowserCorsRequirement,
) -> bool:
    """Check one exact origin against one role-derived Bucket CORS capability."""
    origins = {str(item) for item in (getattr(rule, "allowed_origins", None) or [])}
    methods = {str(item).upper() for item in (getattr(rule, "allowed_methods", None) or [])}
    headers = {str(item).lower() for item in (getattr(rule, "allowed_headers", None) or [])}
    exposed = {str(item).lower() for item in (getattr(rule, "expose_headers", None) or [])}
    return (
        origin in origins
        and not any("*" in value for value in origins)
        and not any("*" in value for value in methods)
        and not any("*" in value for value in headers)
        and {value.upper() for value in requirement.methods}.issubset(methods)
        and {value.lower() for value in requirement.allowed_headers}.issubset(headers)
        and {value.lower() for value in requirement.expose_headers}.issubset(exposed)
    )


def append_cors_requirement(
    rules: Sequence[Any],
    origin: str,
    requirement: BrowserCorsRequirement,
) -> tuple[list[Any], bool]:
    """Append one minimal role-derived rule without replacing unrelated rules."""
    existing = list(rules)
    if any(cors_rule_matches_requirement(rule, origin, requirement) for rule in existing):
        return existing, False
    if len(existing) >= 10:
        raise RuntimeError("OSS Bucket already has the maximum number of CORS rules")

    from oss2.models import CorsRule

    existing.append(
        CorsRule(
            allowed_origins=[origin],
            allowed_methods=list(requirement.methods),
            allowed_headers=list(requirement.allowed_headers),
            expose_headers=list(requirement.expose_headers),
            max_age_seconds=600,
        )
    )
    return existing, True


_RAW_UPLOAD_REQUIREMENT = BrowserCorsRequirement(
    bucket="",
    roles=("raw",),
    methods=("PUT",),
    allowed_headers=("Content-Type",),
    expose_headers=("ETag",),
)


def upload_cors_rule_matches(rule: Any, origin: str) -> bool:
    """Backward-compatible predicate for the raw multipart upload capability."""
    return cors_rule_matches_requirement(rule, origin, _RAW_UPLOAD_REQUIREMENT)


def append_upload_cors_rule(rules: Sequence[Any], origin: str) -> tuple[list[Any], bool]:
    """Backward-compatible helper for callers that only need raw multipart PUT."""
    return append_cors_requirement(rules, origin, _RAW_UPLOAD_REQUIREMENT)


def wait_for_upload_cors_rules(
    read_rules: Callable[[], Sequence[Any]],
    origins: Sequence[str],
    *,
    timeout_seconds: float = 90.0,
    poll_interval_seconds: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait for OSS to expose an accepted CORS update with a bounded retry."""
    if timeout_seconds < 0 or poll_interval_seconds <= 0:
        raise ValueError(
            "CORS verification timeout must be nonnegative and interval must be positive"
        )
    deadline = monotonic() + timeout_seconds
    while True:
        rules = read_rules()
        if all(any(upload_cors_rule_matches(rule, origin) for rule in rules) for origin in origins):
            return True
        remaining_seconds = deadline - monotonic()
        if remaining_seconds <= 0:
            return False
        sleep(min(poll_interval_seconds, remaining_seconds))


def wait_for_browser_cors_requirements(
    read_rules: Callable[[str], Sequence[Any]],
    origins: Sequence[str],
    requirements: Sequence[BrowserCorsRequirement],
    *,
    timeout_seconds: float = 90.0,
    poll_interval_seconds: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Boundedly wait until every physical Bucket exposes its exact rule."""
    if timeout_seconds < 0 or poll_interval_seconds <= 0:
        raise ValueError(
            "CORS verification timeout must be nonnegative and interval must be positive"
        )
    deadline = monotonic() + timeout_seconds
    while True:
        if all(
            all(
                any(
                    cors_rule_matches_requirement(rule, origin, requirement)
                    for rule in read_rules(requirement.bucket)
                )
                for origin in origins
            )
            for requirement in requirements
        ):
            return True
        remaining_seconds = deadline - monotonic()
        if remaining_seconds <= 0:
            return False
        sleep(min(poll_interval_seconds, remaining_seconds))


def _read_bucket_rules(bucket: Any) -> list[Any]:
    from oss2.exceptions import NoSuchCors

    try:
        return list(getattr(bucket.get_bucket_cors(), "rules", None) or [])
    except NoSuchCors:
        return []


def _contains_wildcard_rule(rules: Sequence[Any]) -> bool:
    """Refuse to bless a browser-delivery Bucket with wildcard CORS rules.

    The apply path deliberately appends one minimal rule and preserves unrelated
    configuration.  It must therefore stop rather than give a false "ready"
    result when an existing wildcard rule still permits a wider browser origin
    or request header surface.
    """
    for rule in rules:
        values = (
            getattr(rule, "allowed_origins", None) or [],
            getattr(rule, "allowed_methods", None) or [],
            getattr(rule, "allowed_headers", None) or [],
        )
        if any("*" in str(item) for group in values for item in group):
            return True
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--origin",
        action="append",
        required=True,
        help="Exact browser origin, without a path. Repeat for every trusted frontend Origin.",
    )
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="Allow an explicit HTTP origin for an isolated UAT environment",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Append missing raw/process rules; the default mode only checks",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        origins = normalize_browser_origins(args.origin, allow_http=bool(args.allow_http))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    from oss2.models import BucketCors

    from data.infra import oss_client

    endpoint = str(oss_client._browser_direct_config().get("endpoint") or "").strip()
    try:
        requirements = browser_cors_requirements(
            endpoint,
            {
                role: oss_client.bucket_name(role)
                for role in ("raw", "process", "official", "export")
            },
        )
    except ValueError as exc:
        print(f"browser OSS CORS configuration is invalid: {exc}", file=sys.stderr)
        return 2
    if not requirements:
        print("browser OSS CORS has no raw/process requirements", file=sys.stderr)
        return 2

    buckets: dict[str, Any] = {}
    for requirement in requirements:
        bucket = oss_client._get_bucket(requirement.bucket)
        if bucket is None:
            print(f"OSS Bucket is unavailable: {requirement.bucket}", file=sys.stderr)
            return 2
        buckets[requirement.bucket] = bucket

    try:
        rules_by_bucket = {
            bucket_name: _read_bucket_rules(bucket) for bucket_name, bucket in buckets.items()
        }
        wildcard_buckets = [
            bucket_name
            for bucket_name, rules in rules_by_bucket.items()
            if _contains_wildcard_rule(rules)
        ]
        if wildcard_buckets:
            print(
                "OSS browser-delivery Bucket has a wildcard CORS rule; remove it before enabling direct access: "
                + ", ".join(wildcard_buckets),
                file=sys.stderr,
            )
            return 2
        missing = [
            (requirement, origin)
            for requirement in requirements
            for origin in origins
            if not any(
                cors_rule_matches_requirement(rule, origin, requirement)
                for rule in rules_by_bucket[requirement.bucket]
            )
        ]
        if not missing:
            print(f"OSS browser CORS is ready for {', '.join(origins)}")
            return 0
        if not args.apply:
            details = ", ".join(
                f"{requirement.bucket} ({'/'.join(requirement.roles)}) for {origin}"
                for requirement, origin in missing
            )
            print(f"OSS browser CORS is missing: {details}", file=sys.stderr)
            return 1
        changed_buckets: set[str] = set()
        for requirement, origin in missing:
            updated, appended = append_cors_requirement(
                rules_by_bucket[requirement.bucket], origin, requirement
            )
            rules_by_bucket[requirement.bucket] = updated
            if appended:
                changed_buckets.add(requirement.bucket)
        for bucket_name in changed_buckets:
            buckets[bucket_name].put_bucket_cors(
                BucketCors(rules_by_bucket[bucket_name], response_vary=True)
            )
        if not wait_for_browser_cors_requirements(
            lambda bucket_name: _read_bucket_rules(buckets[bucket_name]),
            origins,
            requirements,
        ):
            print(
                "OSS browser CORS verification did not converge within 90 seconds",
                file=sys.stderr,
            )
            return 1
    except Exception as exc:
        print(f"OSS browser CORS operation failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    print("OSS browser CORS was added for raw/process browser delivery")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
