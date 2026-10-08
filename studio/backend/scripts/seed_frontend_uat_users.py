"""Create or deactivate isolated UI acceptance identities, never workflow results.

Creation requires a private password file; cleanup requires a separate admin actor.
The guard intentionally requires all three dedicated Studio UAT buckets.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from data.config import get_runtime_config, settings
from data.database import SessionLocal, User
from data.infra.redis_client import RedisUnavailableError
from data.security.audit import emit_audit_event
from data.security.refresh_store import revoke_all_for_user
from data.services.user_lifecycle import set_user_active
from data.utils.helpers import hash_password

UAT_ROLES = ("admin", "annotator", "auditor")
# Earlier acceptance runs created two unsupported roles. Cleanup must retain
# those exact identities even though fresh seeds must never create them.
CLEANUP_ROLES = (*UAT_ROLES, "operator", "viewer")


def _deactivate(namespace: str, actor_user_id: int) -> list[dict]:
    emails = [f"{namespace}.{role}@uat.quicrobot.xyz" for role in CLEANUP_ROLES]
    identities = []
    with SessionLocal() as db:
        actor = db.get(User, actor_user_id)
        if actor is None or not actor.is_active or actor.role != "admin" or actor.email in emails:
            raise SystemExit("Cleanup requires an active admin outside the acceptance namespace")
        actor_email = actor.email
        users = db.query(User).filter(User.email.in_(emails)).order_by(User.id).all()
        for user in users:
            user, cancelled_jobs = set_user_active(
                db,
                actor_user_id=actor.id,
                target_user_id=user.id,
                is_active=False,
            )
            identities.append(
                {
                    "id": user.id,
                    "email": user.email,
                    "role": user.role,
                    "is_active": False,
                    "cancelled_job_count": len(cancelled_jobs),
                }
            )
        db.commit()
    for identity in identities:
        try:
            identity["revoked_sessions"] = revoke_all_for_user(identity["id"])
            identity["session_cleanup_pending"] = False
        except RedisUnavailableError:
            # Database activation and session epochs are already authoritative.
            identity["revoked_sessions"] = 0
            identity["session_cleanup_pending"] = True
        emit_audit_event(
            "auth.user.status.change",
            actor=actor_email,
            resource=str(identity["id"]),
            detail={"source": "frontend_uat_cleanup", **identity},
            level="warning",
        )
    return identities


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--deactivate", action="store_true")
    parser.add_argument("--actor-user-id", type=int)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"fe-uat-[a-z0-9-]{1,40}", args.namespace):
        parser.error("namespace must start with fe-uat- and contain lowercase letters/digits")
    buckets = [settings.oss_bucket_raw, settings.oss_bucket_process, settings.oss_bucket_export]
    if buckets != [f"quicstudio-uat-{role}" for role in ("raw", "process", "export")]:
        raise SystemExit("Refusing to seed outside the dedicated Studio UAT environment")
    if args.deactivate:
        if not args.actor_user_id or args.actor_user_id < 1:
            parser.error("--deactivate requires --actor-user-id of a separate active admin")
        identities = _deactivate(args.namespace, args.actor_user_id)
        print(
            json.dumps({"namespace": args.namespace, "action": "deactivate", "users": identities})
        )
        return
    if args.password_file is None:
        parser.error("creation requires --password-file")
    configured_roles = (get_runtime_config().get("rbac") or {}).get("roles") or {}
    missing_roles = sorted(set(UAT_ROLES) - set(configured_roles))
    if missing_roles:
        raise SystemExit(f"Acceptance roles are not configured: {', '.join(missing_roles)}")
    password = args.password_file.read_text().strip()
    if len(password) < 20:
        parser.error("use a private password file with at least 20 characters")
    password_hash = hash_password(password)
    identities = []
    with SessionLocal() as db:
        for role in UAT_ROLES:
            email = f"{args.namespace}.{role}@uat.quicrobot.xyz"
            user = db.query(User).filter(User.email == email).one_or_none()
            created = user is None
            if user is None:
                user = User(
                    email=email,
                    role=role,
                    password_hash=password_hash,
                    is_active=True,
                    must_change_password=False,
                )
                db.add(user)
                db.flush()
            elif user.role != role or not user.is_active:
                raise SystemExit(
                    f"Existing acceptance identity changed; refusing to overwrite: {email}"
                )
            identities.append({"id": user.id, "email": email, "role": role, "created": created})
        db.commit()
    # Never print a password, token, environment or connection string.
    print(json.dumps({"namespace": args.namespace, "users": identities}))


if __name__ == "__main__":
    main()
