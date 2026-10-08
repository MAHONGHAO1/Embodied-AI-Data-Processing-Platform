"""Socket.IO server assembly for the same-origin QuicData ASGI application."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import socketio
from sqlalchemy.orm import Session

from data.config import settings
from data.database import SessionLocal
from data.infra.redis_client import RedisUnavailableError
from data.realtime.dispatcher import dispatch_realtime_events
from data.realtime.namespace import RealtimeNamespace

_socket_server: socketio.AsyncServer | None = None
_realtime_loop: asyncio.AbstractEventLoop | None = None
_outbox_dispatch_task: asyncio.Task[None] | None = None
_OUTBOX_DISPATCH_INTERVAL_SECONDS = 5
logger = logging.getLogger("quicdata.realtime")


def create_socket_server() -> socketio.AsyncServer:
    redis_url = str(settings.redis_url or "").strip()
    if not redis_url:
        raise RedisUnavailableError()
    manager = socketio.AsyncRedisManager(redis_url)
    return socketio.AsyncServer(
        async_mode="asgi",
        client_manager=manager,
        cors_allowed_origins=settings.cors_origin_list,
        transports=["websocket"],
        logger=False,
        engineio_logger=False,
    )


def create_socket_application(
    http_app: Callable[..., Any],
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> tuple[Callable[..., Any], socketio.AsyncServer]:
    global _socket_server
    server = create_socket_server()
    server.register_namespace(RealtimeNamespace(session_factory=session_factory))
    _socket_server = server
    return (
        socketio.ASGIApp(server, other_asgi_app=http_app, socketio_path="socket.io"),
        server,
    )


def bind_realtime_event_loop() -> None:
    """Record the ASGI loop so synchronous FastAPI routes can dispatch safely."""
    global _realtime_loop
    _realtime_loop = asyncio.get_running_loop()


def start_realtime_outbox_dispatcher() -> None:
    """Retry pending outbox rows even when no subsequent HTTP request arrives."""
    global _outbox_dispatch_task
    if _socket_server is None:
        raise RuntimeError("Socket.IO server is not initialized")
    if _outbox_dispatch_task is None or _outbox_dispatch_task.done():
        _outbox_dispatch_task = _socket_server.start_background_task(_outbox_dispatch_loop)


async def stop_realtime_outbox_dispatcher() -> None:
    """Cancel this process's dispatcher without changing committed outbox state."""
    global _outbox_dispatch_task
    task = _outbox_dispatch_task
    _outbox_dispatch_task = None
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _outbox_dispatch_loop() -> None:
    while True:
        try:
            if _socket_server is not None:
                await dispatch_realtime_events(_socket_server)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("realtime outbox background dispatch failed")
        await asyncio.sleep(_OUTBOX_DISPATCH_INTERVAL_SECONDS)


def schedule_realtime_dispatch() -> None:
    """Queue post-commit delivery from a sync route onto the ASGI event loop."""
    if not settings.realtime_dispatcher_in_api:
        return
    if _socket_server is None or _realtime_loop is None:
        raise RuntimeError("Socket.IO realtime loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(
        dispatch_realtime_events(_socket_server),
        _realtime_loop,
    )
    future.add_done_callback(_log_dispatch_failure)


def schedule_realtime_user_revocation(user_id: int) -> None:
    """Disconnect this process's sockets for a user after a committed revoke."""
    _schedule_realtime_control(lambda: _disconnect_local_user(int(user_id)))


def schedule_realtime_workspace_revocation(user_id: int, workspace_id: int) -> None:
    """Remove this process's subscriptions to a workspace after membership removal."""
    _schedule_realtime_control(
        lambda: _remove_local_workspace_subscriptions(int(user_id), int(workspace_id))
    )


def _schedule_realtime_control(coroutine_factory: Callable[[], Any]) -> None:
    if _socket_server is None or _realtime_loop is None:
        logger.warning("realtime control was unavailable after a committed authorization change")
        return
    future = asyncio.run_coroutine_threadsafe(coroutine_factory(), _realtime_loop)
    future.add_done_callback(_log_dispatch_failure)


async def _disconnect_local_user(user_id: int) -> None:
    if _socket_server is None:
        return
    participants = list(_socket_server.manager.get_participants("/", f"user:{user_id}"))
    for sid, _ in participants:
        await _socket_server.disconnect(sid, namespace="/")


async def _remove_local_workspace_subscriptions(user_id: int, workspace_id: int) -> None:
    if _socket_server is None:
        return
    participants = list(_socket_server.manager.get_participants("/", f"user:{user_id}"))
    for sid, _ in participants:
        session = await _socket_server.get_session(sid, namespace="/")
        subscriptions = session.get("subscriptions", [])
        retained = []
        for subscription in subscriptions:
            if subscription.get("workspace_id") == workspace_id:
                room = subscription.get("room")
                if isinstance(room, str):
                    await _socket_server.leave_room(sid, room, namespace="/")
            else:
                retained.append(subscription)
        session["subscriptions"] = retained
        await _socket_server.save_session(sid, session, namespace="/")


def _log_dispatch_failure(future) -> None:
    try:
        future.result()
    except Exception:
        logger.exception("realtime outbox dispatch failed")
