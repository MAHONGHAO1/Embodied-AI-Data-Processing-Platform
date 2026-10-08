"""Retired job kinds are readable history, never new work."""

RETIRED_JOB_KINDS = frozenset(
    {
        "episode_publish",
        "native_lerobot_scan",
        "native_lerobot_copy",
        "native_lerobot_bundle",
    }
)
