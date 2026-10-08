"""OSS mirror mapping for Quota4090 / ECS DLC paths."""

from quictrain_scheduler.engine import Scheduler


def _scheduler(**kwargs):
    defaults = {
        "session_factory": None,
        "provider": object(),
        "cpfs_root_uri": "bmcpfs://bmcpfs-03001wmodv0g6pynhun3u.cn-beijing",
        "cpfs_mount_path": "/mnt/cpfs",
        "cpfs_workspace_dir": "quictrain",
        "oss_cpfs_mirror_prefix": "oss://bucket/quictrain",
    }
    defaults.update(kwargs)
    return Scheduler(**defaults)


def test_oss_mirror_uri_from_bmcpfs():
    sched = _scheduler()
    uri = (
        "bmcpfs://bmcpfs-03001wmodv0g6pynhun3u.cn-beijing/"
        "quictrain/datasets/lerobot/BPX-L-S01-T001-1-6/v1"
    )
    assert (
        sched._oss_mirror_uri(uri)
        == "oss://bucket/quictrain/datasets/lerobot/BPX-L-S01-T001-1-6/v1"
    )


def test_oss_mirror_uri_requires_prefix():
    sched = _scheduler(oss_cpfs_mirror_prefix=None)
    assert sched._oss_mirror_uri("bmcpfs://fs/quictrain/datasets/x") is None
