from datetime import datetime, timedelta, timezone

from data.utils.formatting import format_api_datetime


def test_format_api_datetime_emits_explicit_utc_for_naive_database_values():
    assert format_api_datetime(datetime(2026, 8, 8, 16, 42, 0)) == "2026-08-08T16:42:00Z"


def test_format_api_datetime_normalizes_aware_values_to_utc():
    value = datetime(2026, 8, 9, 0, 42, 0, tzinfo=timezone(timedelta(hours=8)))

    assert format_api_datetime(value) == "2026-08-08T16:42:00Z"
