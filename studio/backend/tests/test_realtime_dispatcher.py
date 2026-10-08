import asyncio

import pytest

import data.realtime.socketio as realtime_socketio
from data.config import settings
from data.realtime.dispatcher import run_realtime_dispatcher


class RecordingSocketServer:
    def start_background_task(self, target):
        return asyncio.create_task(target())


@pytest.mark.anyio
async def test_background_dispatcher_retries_without_a_follow_up_http_request(monkeypatch):
    calls = []

    async def dispatch(_server):
        calls.append(True)

    monkeypatch.setattr(realtime_socketio, "_socket_server", RecordingSocketServer())
    monkeypatch.setattr(realtime_socketio, "_outbox_dispatch_task", None)
    monkeypatch.setattr(realtime_socketio, "dispatch_realtime_events", dispatch)
    monkeypatch.setattr(realtime_socketio, "_OUTBOX_DISPATCH_INTERVAL_SECONDS", 3600)

    realtime_socketio.start_realtime_outbox_dispatcher()
    await asyncio.sleep(0)
    await realtime_socketio.stop_realtime_outbox_dispatcher()

    assert calls


@pytest.mark.anyio
async def test_standalone_dispatcher_consumes_until_stopped():
    calls = []
    stop_event = asyncio.Event()

    async def dispatch(_server, *, session_factory):
        calls.append(session_factory)
        stop_event.set()
        return 1

    session_factory = object()
    await run_realtime_dispatcher(
        object(),
        session_factory=session_factory,
        stop_event=stop_event,
        poll_interval_seconds=0.01,
        dispatch=dispatch,
    )

    assert calls == [session_factory]


def test_api_post_commit_dispatch_is_disabled_without_explicit_dev_switch(monkeypatch):
    calls = []

    monkeypatch.setattr(settings, "realtime_dispatcher_in_api", False)
    monkeypatch.setattr(
        realtime_socketio.asyncio,
        "run_coroutine_threadsafe",
        lambda *_args, **_kwargs: calls.append(True),
    )

    realtime_socketio.schedule_realtime_dispatch()

    assert calls == []
