"""Start and mount the QuicTrain control plane under ``/api/train``."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any

_TRAIN_SRC = Path(__file__).resolve().parent / "src"
if _TRAIN_SRC.is_dir() and str(_TRAIN_SRC) not in sys.path:
    sys.path.insert(0, str(_TRAIN_SRC))

logger = logging.getLogger("quicdata.train")
_started = False
_scheduler_thread: threading.Thread | None = None


def train_importable() -> bool:
    try:
        import quictrain_api.db  # noqa: F401
    except Exception as exc:
        logger.warning("train control plane unavailable: %s", exc)
        return False
    return True


def startup_train() -> None:
    """Initialize the train database and the embedded scheduler once."""

    global _started, _scheduler_thread
    if _started:
        return
    from quictrain_api.db import SessionLocal, init_database
    from quictrain_api.main import _scheduler_loop, _scheduler_stop
    from quictrain_api.service import seed_catalog
    from quictrain_api.settings import get_settings

    init_database()
    with SessionLocal() as session:
        seed_catalog(session)
        session.commit()
    settings = get_settings()
    if settings.embedded_scheduler and (
        _scheduler_thread is None or not _scheduler_thread.is_alive()
    ):
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            daemon=True,
            name="quictrain-scheduler",
        )
        _scheduler_thread.start()
    _started = True


def shutdown_train() -> None:
    """Stop the embedded scheduler started by :func:`startup_train`."""

    global _started, _scheduler_thread
    from quictrain_api.main import _scheduler_stop

    _scheduler_stop.set()
    thread = _scheduler_thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2)
    _scheduler_thread = None
    _started = False


def mount_train(app: Any) -> bool:
    """Mount QuicTrain at ``/api/train`` without starting background work."""

    if not train_importable():
        return False
    from quictrain_api.main import app as train_app

    app.mount("/api/train", train_app)
    return True
