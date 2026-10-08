"""Authenticated Socket.IO namespace for resource subscriptions."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import socketio
from sqlalchemy.orm import Session

from data.realtime.auth import RealtimeAuthenticationError, authenticate_socket
from data.realtime.subscriptions import SubscriptionDenied, resolve_resource_subscription


class RealtimeNamespace(socketio.AsyncNamespace):
    """Keep per-connection state in Socket.IO sessions, never a process-global SID map."""

    def __init__(self, *, session_factory: Callable[[], Session], namespace: str = "/") -> None:
        super().__init__(namespace)
        self._session_factory = session_factory

    async def on_connect(self, sid: str, environ: dict[str, Any], auth: object) -> bool:
        db = self._session_factory()
        try:
            identity = authenticate_socket(
                db,
                auth=auth,
                origin=environ.get("HTTP_ORIGIN"),
            )
        except RealtimeAuthenticationError:
            return False
        finally:
            db.close()

        session = {
            "user_id": identity.user_id,
            "realtime_session_epoch": identity.session_epoch,
            "expires_at": identity.expires_at,
            "subscriptions": [],
        }
        await self.server.save_session(sid, session, namespace=self.namespace)
        await self.server.enter_room(sid, f"user:{identity.user_id}", namespace=self.namespace)
        await self.server.enter_room(
            sid,
            f"user:{identity.user_id}:epoch:{identity.session_epoch}",
            namespace=self.namespace,
        )
        self.server.start_background_task(
            self._disconnect_at_expiry,
            sid,
            identity.expires_at,
        )
        return True

    async def on_subscribe(self, sid: str, data: object) -> dict[str, object]:
        if not isinstance(data, dict) or set(data) != {"resource_type", "resource_id"}:
            raise ConnectionRefusedError("invalid realtime subscription")
        resource_type = data["resource_type"]
        resource_id = data["resource_id"]
        if not isinstance(resource_type, str) or not isinstance(resource_id, str):
            raise ConnectionRefusedError("invalid realtime subscription")

        session = await self.server.get_session(sid, namespace=self.namespace)
        user_id = session.get("user_id")
        realtime_session_epoch = session.get("realtime_session_epoch")
        if not isinstance(user_id, int) or not isinstance(realtime_session_epoch, int):
            raise ConnectionRefusedError("socket session is unavailable")

        db = self._session_factory()
        try:
            subscription = resolve_resource_subscription(
                db,
                actor_id=user_id,
                realtime_session_epoch=realtime_session_epoch,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        except SubscriptionDenied as exc:
            raise ConnectionRefusedError("realtime access denied") from exc
        finally:
            db.close()

        await self.server.enter_room(sid, subscription.room, namespace=self.namespace)
        subscriptions = [
            entry
            for entry in session.get("subscriptions", [])
            if entry.get("room") != subscription.room
        ]
        subscriptions.append(
            {
                "room": subscription.room,
                "workspace_id": subscription.workspace_id,
            }
        )
        session["subscriptions"] = subscriptions
        await self.server.save_session(sid, session, namespace=self.namespace)
        return {
            "resource_type": subscription.resource_type,
            "resource_id": subscription.resource_id,
        }

    async def _disconnect_at_expiry(self, sid: str, expires_at: int) -> None:
        await asyncio.sleep(max(0, expires_at - int(time.time())))
        await self.server.disconnect(sid, namespace=self.namespace)
