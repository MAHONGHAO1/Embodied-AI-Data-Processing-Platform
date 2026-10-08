from __future__ import annotations


class MLflowTrackingClient:
    """Small Scheduler-side MLflow client; training pods never need ECS credentials."""

    def __init__(self, tracking_uri: str, experiment_name: str = "QuicTrain") -> None:
        from mlflow.tracking import MlflowClient

        self.client = MlflowClient(tracking_uri=tracking_uri)
        self.experiment_name = experiment_name

    def create_run(self, tags: dict[str, str]) -> tuple[str, str]:
        experiment = self.client.get_experiment_by_name(self.experiment_name)
        if experiment is None:
            try:
                experiment_id = self.client.create_experiment(self.experiment_name)
            except Exception:
                experiment = self.client.get_experiment_by_name(self.experiment_name)
                if experiment is None:
                    raise
                experiment_id = experiment.experiment_id
        else:
            experiment_id = experiment.experiment_id
        idempotency_key = tags.get("quictrain.idempotency_key")
        if idempotency_key:
            existing = self.client.search_runs(
                experiment_ids=[experiment_id],
                filter_string=(f"tags.`quictrain.idempotency_key` = '{idempotency_key}'"),
                max_results=1,
            )
            if existing:
                run_id = existing[0].info.run_id
                for name, value in tags.items():
                    self.client.set_tag(run_id, name, value)
                return run_id, experiment_id
        run = self.client.create_run(experiment_id=experiment_id, tags=tags)
        return run.info.run_id, experiment_id

    def log_metrics(self, run_id: str, metrics: dict[str, float], step: int) -> None:
        for name, value in metrics.items():
            self.client.log_metric(run_id, name, value, step=step)

    def finish(self, run_id: str, status: str) -> None:
        self.client.set_terminated(run_id, status=status)
