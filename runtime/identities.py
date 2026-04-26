from __future__ import annotations

import re


_RUNTIME_IDENTITY_SANITIZE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def service_identity(role: str) -> str:
    return str(role or "").strip() or "unknown"


def local_identity(owner_role: str, target_role: str) -> str:
    owner = service_identity(owner_role)
    target = service_identity(target_role)
    return f"{owner}.local.{target}"


def sanitize_identity(identity: str) -> str:
    normalized = _RUNTIME_IDENTITY_SANITIZE_PATTERN.sub("_", str(identity or "").strip())
    normalized = normalized.strip("._-")
    return normalized or "unknown"


def service_runtime_identity(role: str) -> str:
    return service_identity(role)


def local_runtime_identity(owner_role: str, target_role: str) -> str:
    return local_identity(owner_role, target_role)


def sanitize_runtime_identity(identity: str) -> str:
    return sanitize_identity(identity)


__all__ = [
    "local_identity",
    "local_runtime_identity",
    "sanitize_identity",
    "sanitize_runtime_identity",
    "service_identity",
    "service_runtime_identity",
]
