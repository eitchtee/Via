"""Server configuration, read from ``VIA_*`` environment variables."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SIZE_RE = re.compile(r"^\s*(\d+)\s*([a-zA-Z]*)\s*$")
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gib": 1024**3,
}
_DURATION_RE = re.compile(r"(\d+)\s*([smhdw])")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_size(value: object) -> int:
    """Parse a byte count: ``1048576``, ``100MiB``, ``1GB``."""
    if isinstance(value, int):
        return value
    m = _SIZE_RE.match(str(value))
    if not m or m[2].lower() not in _SIZE_UNITS:
        raise ValueError(f"invalid size {value!r} (examples: 1048576, 100MiB, 1GB)")
    return int(m[1]) * _SIZE_UNITS[m[2].lower()]


def parse_duration(value: object) -> int:
    """Parse a duration in seconds: ``3600``, ``7d``, ``1h30m``."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text)
    parts = _DURATION_RE.findall(text)
    if not parts or _DURATION_RE.sub("", text).strip():
        raise ValueError(f"invalid duration {value!r} (examples: 3600, 30m, 7d, 1h30m)")
    return sum(int(n) * _DURATION_UNITS[unit] for n, unit in parts)


Size = Annotated[int, BeforeValidator(parse_size)]
Duration = Annotated[int, BeforeValidator(parse_duration)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIA_")

    data_dir: Path = Path("data")
    master_key: str | None = None
    master_key_file: Path | None = None
    base_url: str | None = None

    host: str = "0.0.0.0"
    port: int = 8080
    forwarded_allow_ips: str = "127.0.0.1"
    log_level: str = "info"
    docs_enabled: bool = True
    web_ui: bool = True

    signup: Literal["closed", "invite", "open"] = "invite"
    admin_username: str | None = None
    admin_password: str | None = None
    session_ttl: Duration = 30 * 86400
    invite_ttl: Duration = 7 * 86400

    default_ttl: Duration = 7 * 86400
    max_ttl: Duration = 30 * 86400
    tombstone_days: int = 7
    history_days: int = 0
    history_keep_files: bool = False

    max_file_size: Size = 100 * 1024**2
    user_quota: Size = 1024**3
    max_text_length: int = 256 * 1024

    push_relay_url: str = ""
    push_relay_key: str | None = None

    login_rate_per_minute: int = 10
    push_rate_per_minute: int = 120
    janitor_interval: Duration = 60
    sse_heartbeat: float = 25.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "via.db"
