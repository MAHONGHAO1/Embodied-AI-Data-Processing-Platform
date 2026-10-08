from types import SimpleNamespace

import pytest
from scripts.configure_oss_browser_cors import (
    _contains_wildcard_rule,
    append_cors_requirement,
    cors_rule_matches_requirement,
    normalize_browser_origin,
    normalize_browser_origins,
    upload_cors_rule_matches,
    wait_for_upload_cors_rules,
)

from data.security.browser_oss import browser_cors_requirements
from data.security.headers import (
    default_content_security_policy,
    validate_browser_direct_content_security_policy,
)


def _rule(
    *,
    origins: list[str],
    methods: list[str],
    headers: list[str],
    exposed: list[str],
):
    return SimpleNamespace(
        allowed_origins=origins,
        allowed_methods=methods,
        allowed_headers=headers,
        expose_headers=exposed,
        max_age_seconds=60,
    )


def test_browser_upload_cors_requires_an_exact_safe_origin():
    assert normalize_browser_origin("https://data.example.com", allow_http=False) == (
        "https://data.example.com"
    )
    assert normalize_browser_origin("https://DATA.example.com:443", allow_http=False) == (
        "https://data.example.com"
    )
    assert normalize_browser_origin("http://data.example.com:80", allow_http=True) == (
        "http://data.example.com"
    )
    assert normalize_browser_origin("http://39.105.51.76:18080", allow_http=True) == (
        "http://39.105.51.76:18080"
    )

    for value in (
        "*",
        "https://data.example.com/path",
        "https://user@example.com",
        "https://data.example.com?next=other",
    ):
        with pytest.raises(ValueError, match="browser origin"):
            normalize_browser_origin(value, allow_http=True)
    with pytest.raises(ValueError, match="HTTPS"):
        normalize_browser_origin("http://39.105.51.76:18080", allow_http=False)


def test_browser_upload_cors_normalizes_each_origin_without_duplicates():
    assert normalize_browser_origins(
        ["https://data.example.com", "https://review.example.com"],
        allow_http=False,
    ) == ("https://data.example.com", "https://review.example.com")
    with pytest.raises(ValueError, match="duplicate"):
        normalize_browser_origins(
            ["https://data.example.com", "https://data.example.com"],
            allow_http=False,
        )


def test_browser_direct_csp_assigns_exact_origins_by_browser_role():
    settings = SimpleNamespace(
        oss_browser_direct_enabled=True,
        oss_browser_endpoint="https://oss-cn-beijing.aliyuncs.com",
        oss_bucket_raw="quic-data-platform",
        oss_bucket_process="quic-process-qrdf",
        oss_bucket_official="quic-qrdf",
        oss_bucket_export="quic-lerobot",
    )

    csp = default_content_security_policy(settings)

    raw_origin = "https://quic-data-platform.oss-cn-beijing.aliyuncs.com"
    process_origin = "https://quic-process-qrdf.oss-cn-beijing.aliyuncs.com"
    official_origin = "https://quic-qrdf.oss-cn-beijing.aliyuncs.com"
    export_origin = "https://quic-lerobot.oss-cn-beijing.aliyuncs.com"

    assert f"connect-src 'self' {raw_origin};" in csp
    assert process_origin not in csp.split("connect-src", 1)[1].split(";", 1)[0]
    assert f"img-src 'self' data: blob: {process_origin};" in csp
    assert f"media-src 'self' blob: {process_origin};" in csp
    assert raw_origin not in csp.split("media-src", 1)[1].split(";", 1)[0]
    assert official_origin not in csp
    assert export_origin not in csp
    assert "*.aliyuncs.com" not in csp

    disabled = default_content_security_policy(
        SimpleNamespace(
            oss_browser_direct_enabled=False,
            oss_browser_endpoint="https://oss-cn-beijing.aliyuncs.com",
            oss_bucket_raw="quic-data-platform",
            oss_bucket_process="quic-process-qrdf",
            oss_bucket_official="quic-qrdf",
            oss_bucket_export="quic-lerobot",
        )
    )
    assert raw_origin not in disabled
    assert process_origin not in disabled


def test_browser_cors_requirements_merge_shared_bucket_roles_without_official_or_export():
    requirements = browser_cors_requirements(
        "https://oss-cn-beijing.aliyuncs.com",
        {
            "raw": "shared-browser-bucket",
            "process": "shared-browser-bucket",
            "official": "official-bucket",
            "export": "export-bucket",
        },
    )

    assert len(requirements) == 1
    requirement = requirements[0]
    assert requirement.bucket == "shared-browser-bucket"
    assert requirement.roles == ("raw", "process")
    assert requirement.methods == ("GET", "HEAD", "PUT")
    assert requirement.allowed_headers == ("Content-Type", "Range")
    assert requirement.expose_headers == (
        "Accept-Ranges",
        "Content-Length",
        "Content-Range",
        "Content-Type",
        "ETag",
    )


def test_custom_csp_requires_exact_role_sources_and_rejects_wildcards():
    buckets = {
        "raw": "quic-data-platform",
        "process": "quic-process-qrdf",
        "official": "quic-qrdf",
        "export": "quic-lerobot",
    }
    endpoint = "https://oss-cn-beijing.aliyuncs.com"
    raw = "https://quic-data-platform.oss-cn-beijing.aliyuncs.com"
    process = "https://quic-process-qrdf.oss-cn-beijing.aliyuncs.com"
    valid = (
        "default-src 'self'; "
        "connect-src 'self' " + raw + "; "
        "img-src 'self' data: " + process + "; "
        "media-src 'self' " + process
    )

    validate_browser_direct_content_security_policy(valid, endpoint=endpoint, buckets=buckets)

    with pytest.raises(ValueError, match="media-src"):
        validate_browser_direct_content_security_policy(
            valid.replace("media-src 'self' " + process, "media-src 'self'"),
            endpoint=endpoint,
            buckets=buckets,
        )
    with pytest.raises(ValueError, match="wildcard"):
        validate_browser_direct_content_security_policy(
            valid.replace("connect-src 'self' " + raw, "connect-src *"),
            endpoint=endpoint,
            buckets=buckets,
        )


def test_browser_upload_cors_requires_put_content_type_and_exposed_etag():
    origin = "https://data.example.com"
    valid = _rule(
        origins=[origin],
        methods=["PUT"],
        headers=["Content-Type"],
        exposed=["ETag"],
    )

    assert upload_cors_rule_matches(valid, origin) is True
    assert (
        upload_cors_rule_matches(
            _rule(origins=["*"], methods=["PUT"], headers=["*"], exposed=["ETag"]),
            origin,
        )
        is False
    )
    assert (
        upload_cors_rule_matches(
            _rule(
                origins=[origin, "*"],
                methods=["PUT"],
                headers=["Content-Type"],
                exposed=["ETag"],
            ),
            origin,
        )
        is False
    )
    assert (
        upload_cors_rule_matches(
            _rule(
                origins=[origin],
                methods=["GET"],
                headers=["Content-Type"],
                exposed=["ETag"],
            ),
            origin,
        )
        is False
    )
    assert (
        upload_cors_rule_matches(
            _rule(
                origins=[origin],
                methods=["PUT"],
                headers=["Content-Type"],
                exposed=[],
            ),
            origin,
        )
        is False
    )


def test_browser_upload_cors_rejects_existing_wildcard_rules_without_overwriting_them():
    assert _contains_wildcard_rule(
        [_rule(origins=["*"], methods=["GET"], headers=["Range"], exposed=[])]
    )
    assert _contains_wildcard_rule(
        [_rule(origins=["https://*.example.com"], methods=["GET"], headers=["Range"], exposed=[])]
    )
    assert _contains_wildcard_rule(
        [_rule(origins=["https://data.example.com"], methods=["PUT"], headers=["*"], exposed=[])]
    )
    assert _contains_wildcard_rule(
        [
            _rule(
                origins=["https://data.example.com"],
                methods=["*"],
                headers=["Content-Type"],
                exposed=[],
            )
        ]
    )
    assert not _contains_wildcard_rule(
        [
            _rule(
                origins=["https://data.example.com"],
                methods=["PUT"],
                headers=["Content-Type"],
                exposed=["ETag"],
            )
        ]
    )


def test_browser_upload_cors_append_preserves_existing_rules():
    existing = _rule(
        origins=["https://download.example.com"],
        methods=["GET"],
        headers=["Range"],
        exposed=["Content-Length"],
    )

    raw_requirement = browser_cors_requirements(
        "https://oss-cn-beijing.aliyuncs.com",
        {"raw": "raw-bucket", "process": "process-bucket"},
    )[0]
    rules, changed = append_cors_requirement(
        [existing],
        "https://data.example.com",
        raw_requirement,
    )

    assert changed is True
    assert rules[0] is existing
    assert (
        cors_rule_matches_requirement(rules[1], "https://data.example.com", raw_requirement) is True
    )
    same_rules, changed_again = append_cors_requirement(
        rules,
        "https://data.example.com",
        raw_requirement,
    )
    assert changed_again is False
    assert same_rules == rules


def test_browser_upload_cors_waits_for_provider_read_after_apply():
    origin = "https://data.example.com"
    existing = _rule(
        origins=["https://download.example.com"],
        methods=["GET"],
        headers=["Range"],
        exposed=["Content-Length"],
    )
    expected = _rule(
        origins=[origin],
        methods=["PUT"],
        headers=["Content-Type"],
        exposed=["ETag"],
    )
    snapshots = iter(([existing], [existing, expected]))
    elapsed = [0.0]
    waits: list[float] = []

    def read_rules():
        return next(snapshots)

    def monotonic():
        return elapsed[0]

    def sleep(seconds: float):
        waits.append(seconds)
        elapsed[0] += seconds

    assert wait_for_upload_cors_rules(
        read_rules,
        [origin],
        timeout_seconds=3.0,
        poll_interval_seconds=1.0,
        monotonic=monotonic,
        sleep=sleep,
    )
    assert waits == [1.0]


def test_browser_upload_cors_wait_is_bounded_when_provider_never_converges():
    origin = "https://data.example.com"
    elapsed = [0.0]
    waits: list[float] = []

    def read_rules():
        return []

    def monotonic():
        return elapsed[0]

    def sleep(seconds: float):
        waits.append(seconds)
        elapsed[0] += seconds

    assert not wait_for_upload_cors_rules(
        read_rules,
        [origin],
        timeout_seconds=2.0,
        poll_interval_seconds=1.0,
        monotonic=monotonic,
        sleep=sleep,
    )
    assert waits == [1.0, 1.0]
