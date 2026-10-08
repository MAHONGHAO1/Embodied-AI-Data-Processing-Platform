from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from quictrain_core import LaunchSpec, ProviderJob, ProviderState
from quictrain_core.provider import LogLine


@dataclass
class _FakeRecord:
    spec: LaunchSpec
    external_id: str
    state: ProviderState = ProviderState.PROVISIONING
    polls: int = 0
    cancelled: bool = False


class FakeProvider:
    """Deterministic provider used by local development and contract tests."""

    def __init__(self) -> None:
        self._by_external_id: dict[str, _FakeRecord] = {}
        self._by_key: dict[str, str] = {}
        self._lock = threading.RLock()
        self.fail_next_submit: bool = False
        self.submit_timeout: bool = False
        self.crash_after_submit: bool = False
        self.last_submit_spec: LaunchSpec | None = None

    def submit(self, spec: LaunchSpec) -> ProviderJob:
        with self._lock:
            self.last_submit_spec = spec
            if self.submit_timeout:
                raise TimeoutError("simulated provider timeout")
            if self.fail_next_submit:
                self.fail_next_submit = False
                raise RuntimeError("simulated provider submit failure")
            if spec.idempotency_key in self._by_key:
                return self.get(self._by_key[spec.idempotency_key])
            external_id = f"fake-{spec.attempt_id}"
            record = _FakeRecord(spec=spec, external_id=external_id)
            self._by_external_id[external_id] = record
            self._by_key[spec.idempotency_key] = external_id
            if self.crash_after_submit:
                self.crash_after_submit = False
                # Record exists; caller simulates process crash before persisting external_id.
            return self._as_job(record)

    def get(self, external_id: str) -> ProviderJob:
        with self._lock:
            record = self._by_external_id.get(external_id)
            if record is None:
                # In-memory only: uvicorn --reload / process restart drops records while
                # SQLite still holds active attempts. Surface UNKNOWN so the scheduler
                # can orphan + recover via idempotency re-submit instead of KeyError spam.
                return self._lost_job(external_id)
            if record.cancelled:
                record.state = ProviderState.CANCELLED
            else:
                record.polls += 1
                if record.polls >= 5:
                    record.state = ProviderState.SUCCEEDED
                elif record.polls >= 2:
                    record.state = ProviderState.RUNNING
            return self._as_job(record)

    def find_by_idempotency_key(self, key: str) -> ProviderJob | None:
        external_id = self._by_key.get(key)
        return self.get(external_id) if external_id else None

    def cancel(self, external_id: str) -> ProviderJob:
        with self._lock:
            record = self._by_external_id.get(external_id)
            if record is None:
                return ProviderJob(
                    external_id=external_id,
                    state=ProviderState.CANCELLED,
                    raw_status="Cancelled",
                    message="FakeProvider record already gone",
                    reason_code="PROVIDER_STATE_LOST",
                )
            record.cancelled = True
            record.state = ProviderState.CANCELLED
            return self._as_job(record)

    def get_logs(self, external_id: str, cursor: int = 0) -> tuple[list[LogLine], int]:
        record = self._by_external_id.get(external_id)
        if record is None:
            return [], cursor
        all_lines = [
            "stage=preflight dataset manifest validated",
            "stage=initialize model runtime ready",
            "stage=train step=100 loss=0.842 lr=0.00005",
            "stage=train step=500 loss=0.421 throughput=18.6",
            "stage=checkpoint artifact manifest written",
            "stage=finalize _SUCCESS verified",
        ]
        visible = min(max(record.polls + 1, 1), len(all_lines))
        lines = [
            LogLine(
                sequence=index + 1,
                timestamp=datetime.now(UTC).isoformat(),
                level="INFO",
                source="runner",
                message=message,
            )
            for index, message in enumerate(all_lines[cursor:visible], start=cursor)
        ]
        return lines, visible

    def get_dashboard_url(self, external_id: str) -> str | None:
        return f"https://fake-provider.invalid/jobs/{external_id}"

    @staticmethod
    def _lost_job(external_id: str) -> ProviderJob:
        return ProviderJob(
            external_id=external_id,
            state=ProviderState.UNKNOWN,
            raw_status="Lost",
            message=(
                "FakeProvider has no in-memory record for this external_id "
                "(likely after process restart or uvicorn --reload)"
            ),
            reason_code="PROVIDER_STATE_LOST",
        )

    @staticmethod
    def _as_job(record: _FakeRecord) -> ProviderJob:
        return ProviderJob(
            external_id=record.external_id,
            state=record.state,
            raw_status=record.state.value,
        )
