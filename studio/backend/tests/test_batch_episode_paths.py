import pytest

from data.services.batch_paths import (
    official_episode_publication_prefix,
    process_episode_run_prefix,
    raw_episode_source_prefix,
    raw_import_original_prefix,
)


def test_raw_episode_source_prefix_is_namespace_scoped():
    prefix = raw_episode_source_prefix(
        workspace_id=1,
        task_set_id=2,
        batch_id=3,
        episode_id=4,
        artifact_id=5,
    )

    assert prefix == "raw/v2/workspaces/1/task-sets/2/batches/3/episodes/4/source/5"


def test_import_original_prefix_is_cleanup_scoped():
    prefix = raw_import_original_prefix(
        workspace_id=1,
        task_set_id=2,
        batch_id=3,
        import_session_id="imp_1",
        artifact_id=5,
    )

    assert prefix == "raw/v2/workspaces/1/task-sets/2/batches/3/imports/imp_1/original/5"


def test_process_and_official_prefixes_use_task_set_v2_namespace():
    assert (
        process_episode_run_prefix(
            workspace_id=1,
            task_set_id=2,
            batch_id=3,
            episode_id=4,
            job_id="job_5",
        )
        == "process/v2/workspaces/1/task-sets/2/batches/3/episodes/4/runs/job_5"
    )
    assert (
        official_episode_publication_prefix(
            workspace_id=1,
            task_set_id=2,
            episode_id=4,
            publication_id="pub_5",
        )
        == "official/v2/workspaces/1/task-sets/2/episodes/4/publications/pub_5"
    )


@pytest.mark.parametrize("import_session_id", ["../escape", "imp/child", "", "imp space"])
def test_import_prefix_rejects_untrusted_session_token(import_session_id):
    with pytest.raises(ValueError):
        raw_import_original_prefix(
            workspace_id=1,
            task_set_id=2,
            batch_id=3,
            import_session_id=import_session_id,
            artifact_id=5,
        )
