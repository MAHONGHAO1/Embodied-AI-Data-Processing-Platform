"""Regression coverage for collector and device attribution precedence."""

from sqlalchemy import literal, select


def test_machine_reported_attribution_outranks_manual_default_everywhere(db_session):
    """A recognized QRDF operator ID must win over an import-form default."""
    from data.services import (
        dashboard_warehouse,
        episode_asset_projection,
        episode_workbench,
        episodes,
    )

    priority_maps = (
        episode_asset_projection._ATTRIBUTION_PRIORITY,
        dashboard_warehouse._ATTRIBUTION_PRIORITY,
    )
    for priorities in priority_maps:
        assert priorities["machine_reported"] > priorities["offline_declared"]

    for priority in (episodes._attribution_priority, episode_workbench._attribution_priority):
        machine_reported, offline_declared = db_session.execute(
            select(
                priority(literal("machine_reported")),
                priority(literal("offline_declared")),
            )
        ).one()
        assert machine_reported > offline_declared
