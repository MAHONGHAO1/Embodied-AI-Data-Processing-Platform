"""Run the durable realtime outbox dispatcher outside API processes."""

from __future__ import annotations

import asyncio
import logging

from data import runtime
from data.config import settings
from data.infra.redis_client import redis_service
from data.realtime.dispatcher import run_realtime_dispatcher
from data.realtime.socketio import create_socket_server
from data.security.logging_setup import attach_secret_log_filter


def main() -> None:
    attach_secret_log_filter()
    logging.basicConfig(level=str(settings.log_level or "INFO").upper())
    settings.validate_security_baseline()
    settings.validate_supply_chain_config()
    settings.validate_storage_deployment_config()
    runtime.assert_schema_current()
    redis_service.connect_required()
    server = create_socket_server()
    try:
        asyncio.run(
            run_realtime_dispatcher(
                server,
                poll_interval_seconds=settings.realtime_dispatch_interval_seconds,
            )
        )
    except KeyboardInterrupt:
        return
    finally:
        redis_service.disconnect()


if __name__ == "__main__":
    main()
