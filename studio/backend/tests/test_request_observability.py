"""Request logs must expose the API boundary without leaking request secrets."""

from __future__ import annotations

from unittest.mock import patch


def test_api_request_log_records_route_and_status_without_query_or_bearer_token(client):
    with patch("data.main.http_logger.info") as info:
        response = client.get(
            "/api/v1/work-queue?workspace_id=99&access_token=do-not-log",
            headers={"Authorization": "Bearer do-not-log"},
        )

    assert response.status_code == 401
    assert info.call_count == 1
    format_string, method, route, status, elapsed_ms = info.call_args.args
    rendered = format_string % (method, route, status, elapsed_ms)
    assert "method=GET" in rendered
    assert "route=/api/v1/work-queue" in rendered
    assert "status=401" in rendered
    assert "workspace_id" not in rendered
    assert "do-not-log" not in rendered
