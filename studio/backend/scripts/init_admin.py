#!/usr/bin/env python3
"""One-time administrator account initialization (used in production when DEFAULT_USERS seed is disabled).

Usage (from backend/ directory):
  python -m scripts.init_admin --email admin@example.com --password '****'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data.bootstrap  # noqa: F401
from data.database import SessionLocal, User, init_db
from data.utils.helpers import hash_password


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or reset QuicData admin user")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--role", default="admin")
    args = parser.parse_args()

    if len(args.password) < 10:
        print("error: password must be at least 10 characters", file=sys.stderr)
        return 2

    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == args.email).first()
        if user:
            user.password_hash = hash_password(args.password)
            user.role = args.role
            user.must_change_password = True
            print(f"updated user {args.email} role={args.role}")
        else:
            db.add(
                User(
                    email=args.email,
                    password_hash=hash_password(args.password),
                    role=args.role,
                    must_change_password=True,
                )
            )
            print(f"created user {args.email} role={args.role}")
        db.commit()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
