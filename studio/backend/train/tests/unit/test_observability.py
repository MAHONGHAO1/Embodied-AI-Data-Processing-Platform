from types import SimpleNamespace

from quictrain_scheduler.engine import parse_lerobot_metrics
from quictrain_scheduler.tracking import MLflowTrackingClient


def test_scheduler_parses_lerobot_metrics_tracker_output():
    assert parse_lerobot_metrics(
        "step:20 smpl:20 ep:1 epch:0.16 loss:0.421 grdn:1.250 lr:5.0e-05 updt_s:0.120 data_s:0.010"
    ) == {
        "train/epoch": 0.16,
        "train/loss": 0.421,
        "train/grad_norm": 1.25,
        "train/lr": 5.0e-05,
        "train/update_seconds": 0.12,
        "train/dataloading_seconds": 0.01,
    }


def test_mlflow_run_creation_is_idempotent_for_an_attempt():
    class MemoryClient:
        def __init__(self):
            self.tags = []

        def get_experiment_by_name(self, _name):
            return SimpleNamespace(experiment_id="exp_1")

        def search_runs(self, experiment_ids, filter_string, max_results):
            assert experiment_ids == ["exp_1"]
            assert filter_string == ("tags.`quictrain.idempotency_key` = 'job_1:attempt:1'")
            assert max_results == 1
            return [SimpleNamespace(info=SimpleNamespace(run_id="run_existing"))]

        def set_tag(self, run_id, name, value):
            self.tags.append((run_id, name, value))

        def create_run(self, **_kwargs):
            raise AssertionError("an existing idempotent run must be reused")

    tracking = MLflowTrackingClient.__new__(MLflowTrackingClient)
    tracking.client = MemoryClient()
    tracking.experiment_name = "QuicTrain"
    tags = {
        "quictrain.job_id": "job_1",
        "quictrain.attempt_id": "att_recovered",
        "quictrain.idempotency_key": "job_1:attempt:1",
    }

    assert tracking.create_run(tags) == ("run_existing", "exp_1")
    assert tracking.client.tags == [("run_existing", name, value) for name, value in tags.items()]
