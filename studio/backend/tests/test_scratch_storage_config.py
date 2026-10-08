from __future__ import annotations

from data.config import Settings


def test_scratch_root_is_the_only_local_runtime_root():
    settings = Settings(_env_file=None, scratch_root="/tmp/quicstudio-scratch")

    assert settings.scratch_root == "/tmp/quicstudio-scratch"
    assert settings.storage_root == settings.scratch_root

    # The compatibility setter must not create a second persistent storage
    # namespace; remaining callers are redirected to the worker scratch root.
    settings.storage_root = "/tmp/compat-scratch"
    assert settings.scratch_root == "/tmp/compat-scratch"
