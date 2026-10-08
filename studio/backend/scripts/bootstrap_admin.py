#!/usr/bin/env python3
"""Create the one-time production administrator from a password file."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data.bootstrap  # noqa: F401
from data.database import SessionLocal, User
from data.runtime import assert_schema_current
from data.security.audit import emit_audit_event
from data.utils.helpers import hash_password


@dataclass(frozen=True)
class BootstrapResult:
    created: bool
    user_id: int
    email: str


def _read_one_time_password(password_file: Path) -> str:
    try:
        first_line = password_file.read_text(encoding="utf-8").splitlines()[0]
    except (FileNotFoundError, IndexError) as exc:
        raise RuntimeError("bootstrap password file must contain one password line") from exc
    if len(first_line) < 10:
        raise RuntimeError("bootstrap password must be at least 10 characters")
    return first_line


def bootstrap_admin(
    session_factory: Callable[[], object],
    *,
    email: str,
    password_file: str | Path,
    consume_password_file: bool = True,
) -> BootstrapResult:
    """Consume a one-time password file after a new admin has been committed."""
    normalized_email = email.strip().lower()
    if not normalized_email or "@" not in normalized_email:
        raise RuntimeError("bootstrap admin email is invalid")
    password_path = Path(password_file)
    password = _read_one_time_password(password_path)

    session = session_factory()
    try:
        existing = session.query(User).filter(User.email == normalized_email).first()
        if existing is not None:
            raise RuntimeError("bootstrap admin already exists")
        user = User(
            email=normalized_email,
            password_hash=hash_password(password),
            role="admin",
            must_change_password=True,
            bootstrap_created_at=datetime.utcnow(),
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        result = BootstrapResult(created=True, user_id=int(user.id), email=user.email)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    if consume_password_file:
        password_path.unlink()
    emit_audit_event(
        "auth.bootstrap_admin",
        actor=result.email,
        resource=str(result.user_id),
        detail={"role": "admin"},
        level="warning",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the one-time QuicData production administrator"
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument(
        "--leave-password-file",
        action="store_true",
        help="leave a read-only Docker secret for the host to remove after a successful bootstrap",
    )
    args = parser.parse_args()

    assert_schema_current()
    result = bootstrap_admin(
        SessionLocal,
        email=args.email,
        password_file=args.password_file,
        consume_password_file=not args.leave_password_file,
    )
    print(f"bootstrap administrator created: {result.email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
