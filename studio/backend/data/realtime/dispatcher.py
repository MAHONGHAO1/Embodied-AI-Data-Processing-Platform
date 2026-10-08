"""Post-commit dispatch entry point for the durable realtime outbox."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import socketio
from sqlalchemy.orm import Session

from data.database import SessionLocal
from data.realtime.publisher import publish_realtime_events

logger = logging.getLogger("quicdata.realtime.dispatcher")


async def dispatch_realtime_events(
    server: socketio.AsyncServer,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> int:
    """Open a fresh session only after the request transaction has committed."""
    db = session_factory()
    try:
        return await publish_realtime_events(
            db,
            server=server,
            now=datetime.now(timezone.utc),
        )
    finally:
        db.close()


async def run_realtime_dispatcher(
    server: socketio.AsyncServer,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    stop_event: asyncio.Event | None = None,
    poll_interval_seconds: float = 5,
    dispatch: Callable[..., Awaitable[int]] = dispatch_realtime_events,
) -> None:
    """Continuously publish committed outbox rows from one isolated process."""
    if poll_interval_seconds <= 0:
        raise ValueError("realtime dispatcher poll interval must be positive")
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            await dispatch(server, session_factory=session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("realtime outbox dispatch cycle failed")
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval_seconds)
        except TimeoutError:
            continue
