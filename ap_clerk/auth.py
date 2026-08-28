"""Read KIMCO credentials from the environment. Never log secret values."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_PROTOTYPE_URL = "https://prototype.kimcoerp.com"
DEFAULT_LIVE_URL = "https://live.kimcoerp.com"
LIVE_HOST = "live.kimcoerp.com"
PROTOTYPE_HOST = "prototype.kimcoerp.com"

ENV_NAMES = (
    "KIMCO_PROTOTYPE_API_KEY",
    "KIMCO_PROTOTYPE_API_PASSWORD",
    "KIMCO_PROTOTYPE_INSTANCE_URL",
    "KIMCO_API_KEY",
    "KIMCO_API_PASSWORD",
    "KIMCO_LIVE_API_KEY",
    "KIMCO_LIVE_API_PASSWORD",
    "KIMCO_LIVE_INSTANCE_URL",
    "KIMCO_TARGET",
)


@dataclass(frozen=True)
class CredentialStatus:
    presence: dict[str, bool]
    key: str | None
    password: str | None
    instance_url: str
    key_source: str | None
    ready: bool
    error: str | None
    target: str


def env_presence() -> dict[str, bool]:
    return {name: bool(os.environ.get(name)) for name in ENV_NAMES}


def format_presence(presence: dict[str, bool]) -> str:
    lines = ["Environment variable presence (names only, values never printed):"]
    for name, present in presence.items():
        lines.append(f"  {name}: {'present' if present else 'absent'}")
    return "\n".join(lines)


def resolve_target(*, live_flag: bool = False) -> str:
    """Explicit --live or KIMCO_TARGET=live. Default remains prototype."""
    env_target = (os.environ.get("KIMCO_TARGET") or "").strip().lower()
    if live_flag or env_target == "live":
        return "live"
    return "prototype"


def load_credentials(*, target: str = "prototype") -> CredentialStatus:
    presence = env_presence()
    if target == "live":
        return _load_live(presence)
    return _load_prototype(presence)


def _load_live(presence: dict[str, bool]) -> CredentialStatus:
    key = os.environ.get("KIMCO_LIVE_API_KEY")
    password = os.environ.get("KIMCO_LIVE_API_PASSWORD")
    instance = (os.environ.get("KIMCO_LIVE_INSTANCE_URL") or DEFAULT_LIVE_URL).rstrip("/")
    source = "KIMCO_LIVE_API_KEY/KIMCO_LIVE_API_PASSWORD" if key and password else None
    error = None
    if not key or not password:
        error = "Refusing live: KIMCO_LIVE_API_KEY and KIMCO_LIVE_API_PASSWORD must both be present."
        key = None
        password = None
        source = None
    elif LIVE_HOST not in instance.lower():
        error = "Refusing live: instance URL is not live.kimcoerp.com"
        key = None
        password = None
        source = None
    return CredentialStatus(
        presence=presence,
        key=key,
        password=password,
        instance_url=instance,
        key_source=source,
        ready=bool(key and password and error is None),
        error=error,
        target="live",
    )


def _load_prototype(presence: dict[str, bool]) -> CredentialStatus:
    proto_key = os.environ.get("KIMCO_PROTOTYPE_API_KEY")
    proto_password = os.environ.get("KIMCO_PROTOTYPE_API_PASSWORD")
    alias_key = os.environ.get("KIMCO_API_KEY")
    alias_password = os.environ.get("KIMCO_API_PASSWORD")
    instance = (os.environ.get("KIMCO_PROTOTYPE_INSTANCE_URL") or DEFAULT_PROTOTYPE_URL).rstrip("/")

    key = None
    password = None
    source = None
    if proto_key and proto_password:
        key, password, source = proto_key, proto_password, "KIMCO_PROTOTYPE_API_KEY/KIMCO_PROTOTYPE_API_PASSWORD"
    elif alias_key and alias_password:
        key, password, source = alias_key, alias_password, "KIMCO_API_KEY/KIMCO_API_PASSWORD"

    error = None
    if LIVE_HOST in instance.lower():
        error = "Refusing live.kimcoerp.com on the default prototype target. Pass --live (off until Kyle says go)."
        key = None
        password = None
        source = None
    elif not key or not password:
        error = "Prototype credentials missing. First populated pair was empty."

    return CredentialStatus(
        presence=presence,
        key=key,
        password=password,
        instance_url=instance,
        key_source=source,
        ready=bool(key and password and error is None),
        error=error,
        target="prototype",
    )
