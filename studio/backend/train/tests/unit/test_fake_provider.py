from quictrain_core import LaunchSpec, ProviderState
from quictrain_provider_local import FakeProvider


def _spec() -> LaunchSpec:
    return LaunchSpec(
        job_id="job_test",
        attempt_id="att_test",
        idempotency_key="job_test:attempt:1",
        image_uri="registry.invalid/runtime",
        image_digest="sha256:test",
        command=("python", "-m", "quictrain_runner"),
        environment={},
        mounts=(),
        resource={"gpu_count": 1},
    )


def test_fake_provider_missing_external_id_is_unknown_not_keyerror():
    provider = FakeProvider()
    lost = provider.get("fake-att_stale_after_reload")
    assert lost.state == ProviderState.UNKNOWN
    assert lost.reason_code == "PROVIDER_STATE_LOST"
    cancelled = provider.cancel("fake-att_stale_after_reload")
    assert cancelled.state == ProviderState.CANCELLED
    lines, cursor = provider.get_logs("fake-att_stale_after_reload")
    assert lines == []
    assert cursor == 0


def test_fake_provider_submit_still_tracks_known_ids():
    provider = FakeProvider()
    submitted = provider.submit(_spec())
    assert provider.get(submitted.external_id).state == ProviderState.PROVISIONING
