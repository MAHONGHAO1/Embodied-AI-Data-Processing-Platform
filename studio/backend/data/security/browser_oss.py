"""Shared browser-to-OSS delivery policy.

The API signs individual OSS objects, while a browser additionally needs a
precise CSP source and a Bucket CORS rule.  Keep those three concerns derived
from the same small role registry so a new storage bucket cannot be enabled by
changing only one of them.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)
_OSS_BUCKET_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")


@dataclass(frozen=True)
class BrowserCorsRequirement:
    """One minimal CORS capability set for an OSS Bucket."""

    bucket: str
    roles: tuple[str, ...]
    methods: tuple[str, ...]
    allowed_headers: tuple[str, ...]
    expose_headers: tuple[str, ...]


@dataclass(frozen=True)
class _BrowserRolePolicy:
    csp_directives: tuple[str, ...]
    cors_methods: tuple[str, ...]
    cors_allowed_headers: tuple[str, ...]
    cors_expose_headers: tuple[str, ...]


# `official` and `export` open a signed document/navigation URL. They are not
# fetched by page JavaScript or used by an in-page media element, so granting
# their Bucket origins to CSP/CORS would only widen the browser surface.
_BROWSER_ROLE_POLICIES: dict[str, _BrowserRolePolicy] = {
    "raw": _BrowserRolePolicy(
        csp_directives=("connect-src",),
        cors_methods=("PUT",),
        cors_allowed_headers=("Content-Type",),
        cors_expose_headers=("ETag",),
    ),
    "process": _BrowserRolePolicy(
        csp_directives=("img-src", "media-src"),
        cors_methods=("GET", "HEAD"),
        cors_allowed_headers=("Range",),
        cors_expose_headers=("Accept-Ranges", "Content-Length", "Content-Range", "Content-Type"),
    ),
    "official": _BrowserRolePolicy((), (), (), ()),
    "export": _BrowserRolePolicy((), (), (), ()),
}

_CORS_METHOD_ORDER = ("GET", "HEAD", "PUT")
_CORS_HEADER_ORDER = ("Content-Type", "Range")
_CORS_EXPOSE_HEADER_ORDER = (
    "Accept-Ranges",
    "Content-Length",
    "Content-Range",
    "Content-Type",
    "ETag",
)


def public_oss_browser_endpoint_parts(value: str | None) -> tuple[str, int | None, bool] | None:
    """Return a normalized public HTTPS endpoint without credentials or paths."""
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        return None
    try:
        parsed_ip = ip_address(hostname)
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
        labels = hostname.split(".")
        if (
            len(hostname) > 253
            or any(not _DNS_LABEL_RE.fullmatch(label) for label in labels)
            or "-internal." in hostname
            or hostname.endswith(".internal.aliyuncs.com")
            or hostname == "localhost"
            or hostname.endswith(".localhost")
        ):
            return None
        return hostname, port, False
    if not parsed_ip.is_global:
        return None
    return parsed_ip.compressed, port, True


def exact_bucket_origin(endpoint: str | None, bucket: str | None) -> str | None:
    """Return the virtual-host Bucket origin used by V4 browser signing.

    IP endpoints cannot safely express a virtual-hosted Bucket origin, so page
    delivery fails closed for them even if the endpoint itself is public.
    """
    endpoint_parts = public_oss_browser_endpoint_parts(endpoint)
    normalized_bucket = str(bucket or "").strip().lower()
    if endpoint_parts is None or not _OSS_BUCKET_NAME_RE.fullmatch(normalized_bucket):
        return None
    host, port, is_ip = endpoint_parts
    if is_ip:
        return None
    authority_host = f"[{host}]" if ":" in host else host
    authority = f"{authority_host}:{port}" if port else authority_host
    bucket_authority = (
        authority
        if host == normalized_bucket or host.startswith(f"{normalized_bucket}.")
        else f"{normalized_bucket}.{authority}"
    )
    return f"https://{bucket_authority}"


def browser_csp_sources(
    endpoint: str | None,
    buckets: Mapping[str, str | None],
) -> dict[str, tuple[str, ...]]:
    """Return exact per-directive Origins required by page-internal OSS use."""
    grouped: dict[str, list[str]] = {}
    for role, policy in _BROWSER_ROLE_POLICIES.items():
        if not policy.csp_directives:
            continue
        origin = exact_bucket_origin(endpoint, buckets.get(role))
        if origin is None:
            continue
        for directive in policy.csp_directives:
            values = grouped.setdefault(directive, [])
            if origin not in values:
                values.append(origin)
    return {directive: tuple(values) for directive, values in grouped.items()}


def browser_cors_requirements(
    endpoint: str | None,
    buckets: Mapping[str, str | None],
) -> tuple[BrowserCorsRequirement, ...]:
    """Aggregate raw/process browser access by physical Bucket.

    OSS CORS cannot be restricted by object prefix.  When raw and process use
    one physical Bucket, their signed-object authorization remains separate but
    its CORS rule needs the union of the two minimal method/header sets.
    """
    grouped: OrderedDict[str, dict[str, list[str]]] = OrderedDict()
    for role, policy in _BROWSER_ROLE_POLICIES.items():
        if not policy.cors_methods:
            continue
        bucket = str(buckets.get(role) or "").strip().lower()
        if exact_bucket_origin(endpoint, bucket) is None:
            raise ValueError(f"browser OSS role {role} has no safe exact Bucket origin")
        aggregate = grouped.setdefault(
            bucket,
            {"roles": [], "methods": [], "headers": [], "exposed": []},
        )
        aggregate["roles"].append(role)
        aggregate["methods"].extend(policy.cors_methods)
        aggregate["headers"].extend(policy.cors_allowed_headers)
        aggregate["exposed"].extend(policy.cors_expose_headers)

    def ordered(values: list[str], allowed_order: tuple[str, ...]) -> tuple[str, ...]:
        present = set(values)
        return tuple(value for value in allowed_order if value in present)

    return tuple(
        BrowserCorsRequirement(
            bucket=bucket,
            roles=tuple(aggregate["roles"]),
            methods=ordered(aggregate["methods"], _CORS_METHOD_ORDER),
            allowed_headers=ordered(aggregate["headers"], _CORS_HEADER_ORDER),
            expose_headers=ordered(aggregate["exposed"], _CORS_EXPOSE_HEADER_ORDER),
        )
        for bucket, aggregate in grouped.items()
    )


def validate_browser_direct_content_security_policy(
    policy: str,
    *,
    endpoint: str | None,
    buckets: Mapping[str, str | None],
) -> None:
    """Validate a custom CSP against the exact raw/process browser contract."""
    required = browser_csp_sources(endpoint, buckets)
    directives: dict[str, tuple[str, ...]] = {}
    for raw_directive in str(policy or "").split(";"):
        tokens = raw_directive.strip().split()
        if not tokens:
            continue
        directive = tokens[0].lower()
        if directive in directives:
            raise ValueError(f"custom CSP repeats {directive}")
        directives[directive] = tuple(tokens[1:])

    for directive, expected_sources in required.items():
        actual_sources = directives.get(directive)
        if actual_sources is None:
            raise ValueError(f"custom CSP must declare {directive}")
        if any("*" in source for source in actual_sources):
            raise ValueError(f"custom CSP {directive} must not contain a wildcard source")
        allowed_sources = {
            "connect-src": {"'self'"},
            "img-src": {"'self'", "data:", "blob:"},
            "media-src": {"'self'", "blob:"},
        }[directive] | set(expected_sources)
        unexpected = set(actual_sources) - allowed_sources
        if unexpected:
            raise ValueError(f"custom CSP {directive} contains an unexpected browser OSS source")
        missing = set(expected_sources) - set(actual_sources)
        if missing:
            raise ValueError(f"custom CSP {directive} is missing its exact browser OSS source")
