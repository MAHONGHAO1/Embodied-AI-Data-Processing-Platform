from types import SimpleNamespace

from quictrain_scheduler.engine import Scheduler


def test_pi05_missing_processor_is_classified_for_product_display():
    job = SimpleNamespace(failure_category=None, failure_message=None)

    Scheduler._classify_failure_log(
        job,
        "FileNotFoundError: Could not find 'policy_preprocessor.json' at lerobot/pi05_base",
    )

    assert job.failure_category == "MODEL_ASSET_MISSING"
    assert "暂停提交" in job.failure_message


def test_cuda_oom_is_classified_with_actionable_guidance():
    job = SimpleNamespace(failure_category=None, failure_message=None)

    Scheduler._classify_failure_log(job, "torch.OutOfMemoryError: CUDA out of memory")

    assert job.failure_category == "RESOURCE_OOM"
    assert "batch size" in job.failure_message


def test_host_sigkill_minus_nine_is_classified_as_resource_oom():
    job = SimpleNamespace(failure_category=None, failure_message=None)

    Scheduler._classify_failure_log(job, "RuntimeError: LeRobot exited with status -9")

    assert job.failure_category == "RESOURCE_OOM"
    assert "SIGKILL" in job.failure_message


def test_pi05_cpfs_tokenizer_path_on_oss_is_classified():
    job = SimpleNamespace(failure_category=None, failure_message=None)

    Scheduler._classify_failure_log(
        job,
        "ValueError: Failed to instantiate processor step 'tokenizer_processor' with config: "
        "{'tokenizer_name': '/mnt/cpfs/quictrain/models/pi05/lerobot-pi05-base/tokenizer'}. "
        "Error: Repo id must be in the form 'repo_name' or 'namespace/repo_name'",
    )

    assert job.failure_category == "MODEL_ASSET_PATH"
    assert "tokenizer" in job.failure_message


def test_pi05_missing_gated_tokenizer_is_not_misclassified_as_oom():
    job = SimpleNamespace(failure_category=None, failure_message=None)

    Scheduler._classify_failure_log(
        job,
        "tokenizer_processor: couldn't connect to https://huggingface.co and no cached files "
        "were found for google/paligemma-3b-pt-224",
    )

    assert job.failure_category == "MODEL_ASSET_MISSING"
    assert "PaliGemma tokenizer" in job.failure_message
    assert "许可" in job.failure_message
