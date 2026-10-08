"""User registration policy (similar to registration config in GitLab / Jenkins / Keycloak).

Config is stored in runtime_config.registration, and admins can update it via PUT /config;
it can also be bootstrapped from REGISTRATION_CONFIG_FILE or a deploy/registration.example.json template.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from data.config import get_runtime_config, settings
from data.database import User
from data.utils.helpers import hash_password

logger = logging.getLogger("quicdata.registration")

VALID_MODES = frozenset({"disabled", "admin_only", "open"})
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def get_registration_config() -> dict[str, Any]:
    """Merge runtime configuration with environment variable override (REGISTRATION_MODE)."""
    cfg = dict(get_runtime_config().get("registration") or {})
    env_mode = (settings.registration_mode or "").strip().lower()
    if env_mode:
        cfg["mode"] = env_mode
    # Normalize
    mode = str(cfg.get("mode") or "admin_only").strip().lower()
    if mode not in VALID_MODES:
        mode = "admin_only"
    cfg["mode"] = mode
    cfg["default_role"] = str(cfg.get("default_role") or "viewer").strip() or "viewer"
    cfg["self_register_role"] = str(cfg.get("self_register_role") or cfg["default_role"]).strip()
    try:
        cfg["min_password_length"] = max(8, int(cfg.get("min_password_length") or 10))
    except (TypeError, ValueError):
        cfg["min_password_length"] = 10
    cfg["creator_roles"] = ["admin"]
    creatable = cfg.get("creatable_roles") or {}
    if not isinstance(creatable, dict):
        creatable = {}
    admin_roles = (
        creatable["admin"]
        if "admin" in creatable
        else list((get_runtime_config().get("rbac") or {}).get("roles") or {})
    )
    cfg["creatable_roles"] = {
        "admin": [str(value).strip() for value in admin_roles if str(value).strip()]
    }
    domains = cfg.get("allowed_email_domains") or []
    if not isinstance(domains, list):
        domains = []
    cfg["allowed_email_domains"] = [
        str(d).strip().lower().lstrip("@") for d in domains if str(d).strip()
    ]
    return cfg


def public_registration_policy() -> dict[str, Any]:
    """Anonymous read-only check: whether self-registration is open, etc. (excludes role matrix details)."""
    cfg = get_registration_config()
    return {
        "mode": cfg["mode"],
        "self_register_enabled": cfg["mode"] == "open",
        "min_password_length": cfg["min_password_length"],
        "self_register_role": cfg["self_register_role"] if cfg["mode"] == "open" else None,
    }


def actor_can_manage_users(actor_role: str | None) -> bool:
    """Only platform administrators may enumerate or manage identities."""
    return (actor_role or "").strip() == "admin"


def actor_can_create_users(actor_role: str | None) -> bool:
    """Whether the actor can create users on behalf of others (registration not disabled, and has administrative privileges)."""
    cfg = get_registration_config()
    if cfg["mode"] == "disabled":
        return False
    return actor_can_manage_users(actor_role)


def assignable_roles_for(actor_role: str | None) -> list[str]:
    """List of roles assignable by current actor (intersected with RBAC role definitions)."""
    cfg = get_registration_config()
    rbac_roles = set((get_runtime_config().get("rbac") or {}).get("roles") or {})
    role = (actor_role or "").strip()
    if not actor_can_create_users(role):
        return []
    creatable = cfg.get("creatable_roles") or {}
    if role == "admin" and "admin" not in creatable:
        # When admin has no explicit config, default to creating all RBAC roles
        return sorted(rbac_roles)
    allowed = list(creatable.get(role) or [])
    return [r for r in allowed if r in rbac_roles]


def creator_registration_view(actor: dict[str, Any]) -> dict[str, Any]:
    cfg = get_registration_config()
    role = actor.get("role")
    return {
        **public_registration_policy(),
        "default_role": cfg["default_role"],
        "self_register_role": cfg["self_register_role"],
        "creator_roles": cfg["creator_roles"],
        "creatable_roles": cfg["creatable_roles"],
        "allowed_email_domains": cfg["allowed_email_domains"],
        "can_manage_users": actor_can_manage_users(role),
        "can_create_users": actor_can_create_users(role),
        "assignable_roles": assignable_roles_for(role),
    }


def validate_registration_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Validation and normalization for PUT /config section=registration."""
    if not isinstance(data, dict):
        raise ValueError("registration 配置必须是对象")
    out = dict(data)
    if "mode" in out:
        mode = str(out["mode"] or "").strip().lower()
        if mode not in VALID_MODES:
            raise ValueError(
                f"无效 registration.mode: {out['mode']}（允许: disabled|admin_only|open）"
            )
        out["mode"] = mode
    if "min_password_length" in out:
        try:
            length = int(out["min_password_length"])
        except (TypeError, ValueError) as exc:
            raise ValueError("min_password_length 必须是整数") from exc
        if length < 8 or length > 128:
            raise ValueError("min_password_length 须在 8–128 之间")
        out["min_password_length"] = length
    out["creator_roles"] = ["admin"]
    creatable = out.get("creatable_roles")
    if creatable is not None and not isinstance(creatable, dict):
        raise ValueError("creatable_roles 必须是对象")
    if (
        isinstance(creatable, dict)
        and "admin" in creatable
        and not isinstance(creatable["admin"], list)
    ):
        raise ValueError("creatable_roles.admin 必须是数组")
    admin_roles = (
        creatable.get("admin", [])
        if isinstance(creatable, dict)
        else list((get_runtime_config().get("rbac") or {}).get("roles") or {})
    )
    out["creatable_roles"] = {"admin": admin_roles}
    if "allowed_email_domains" in out and not isinstance(out["allowed_email_domains"], list):
        raise ValueError("allowed_email_domains 必须是数组")
    return out


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email_format(email: str) -> str | None:
    e = normalize_email(email)
    if not e or len(e) > 128 or not _EMAIL_RE.match(e):
        return "邮箱格式无效"
    return None


def validate_email_domain(email: str, cfg: dict[str, Any] | None = None) -> str | None:
    cfg = cfg or get_registration_config()
    domains = cfg.get("allowed_email_domains") or []
    if not domains:
        return None
    e = normalize_email(email)
    domain = e.rsplit("@", 1)[-1]
    if domain not in domains:
        return f"邮箱域名不在允许列表: {', '.join(domains)}"
    return None


def validate_password(password: str, cfg: dict[str, Any] | None = None) -> str | None:
    cfg = cfg or get_registration_config()
    min_len = int(cfg.get("min_password_length") or 10)
    if not password or len(password) < min_len:
        return f"密码长度至少 {min_len} 位"
    if len(password) > 256:
        return "密码过长"
    return None


def create_user(
    db: Session,
    *,
    email: str,
    password: str,
    role: str,
    must_change_password: bool = False,
) -> tuple[User | None, str | None]:
    """Create user. Returns (user, None) on success, or (None, message) on failure."""
    err = validate_email_format(email)
    if err:
        return None, err
    err = validate_email_domain(email)
    if err:
        return None, err
    err = validate_password(password)
    if err:
        return None, err

    email_n = normalize_email(email)
    rbac_roles = (get_runtime_config().get("rbac") or {}).get("roles") or {}
    if role not in rbac_roles:
        return None, f"未知角色: {role}"

    if db.query(User).filter(User.email == email_n).first():
        return None, "该邮箱已注册"

    user = User(
        email=email_n,
        password_hash=hash_password(password),
        role=role,
        must_change_password=must_change_password,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, None


def load_registration_file_into_runtime(path: str | Path) -> None:
    """Merge registration section from JSON file into runtime_config (optional on startup)."""
    p = Path(path)
    if not p.is_file():
        logger.warning("registration config file not found: %s", p)
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("failed to read registration config file: %s", p)
        return
    section = (
        data.get("registration") if isinstance(data, dict) and "registration" in data else data
    )
    if not isinstance(section, dict):
        logger.error("registration config file must be a JSON object: %s", p)
        return
    try:
        normalized = validate_registration_payload(section)
    except ValueError as exc:
        logger.error("invalid registration config file %s: %s", p, exc)
        return
    runtime = get_runtime_config()
    runtime.setdefault("registration", {}).update(normalized)
    logger.info("loaded registration config from %s", p)
