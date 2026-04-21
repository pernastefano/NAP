"""
config_manager.py – Application configuration backed by a JSON file.

Reads/writes config.json.  All settings are also overridable via environment
variables (prefixed NAP_) so the service can be configured without touching
the file (useful during OTA updates and containerised testing).

Usage
-----
    from backend.app.config_manager import get_config, update_config

    cfg = get_config()
    print(cfg.default_source)

    update_config({"default_source": "airplay"})
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_CONFIG_FILE = Path("/etc/nap/config.json")
_CONFIG_FILE_FALLBACK = Path(__file__).resolve().parent.parent.parent / "config" / "config.json"
_write_lock = threading.Lock()


class NAPConfig(BaseSettings):
    """All NAP settings.  Values are resolved in priority order:
    1. Environment variables  (NAP_<FIELD_NAME_UPPER>)
    2. config.json on disk
    3. Default values below
    """

    model_config = SettingsConfigDict(
        env_prefix="NAP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── audio ──────────────────────────────────────────────────────────────────
    default_source: str = Field("idle", description="Source activated on boot.")
    audio_output_device: str = Field("default", description="ALSA output device (e.g. 'default', 'hw:0,0', 'hw:1,0').")
    lock_timeout: float = Field(8.0, ge=0.1, le=15.0, description="ALSA lock timeout (s).")
    systemd_verify_timeout: float = Field(10.0, ge=1.0, le=60.0)

    # ── LCD ───────────────────────────────────────────────────────────────────
    lcd_enabled: bool = Field(True)
    lcd_backlight_timeout: int = Field(30, ge=0, description="Seconds; 0 = always on.")

    # ── OTA ───────────────────────────────────────────────────────────────────
    ota_enabled: bool = Field(True)
    ota_github_repo: str = Field("your-org/nap")
    ota_schedule_cron: str = Field("0 3 * * *", description="Cron expression for auto-update.")

    # ── API ───────────────────────────────────────────────────────────────────
    api_host: str = Field("0.0.0.0")
    api_port: int = Field(8000, ge=1, le=65535)
    log_level: str = Field("INFO")
    log_max_lines: int = Field(500, ge=10, le=5000, description="Lines kept in the in-memory log buffer.")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return upper


def _config_file_path() -> Path:
    if _CONFIG_FILE.exists():
        return _CONFIG_FILE
    _CONFIG_FILE_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
    return _CONFIG_FILE_FALLBACK


def _load_from_file() -> dict[str, Any]:
    path = _config_file_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("config_manager: could not read %s: %s – using defaults.", path, exc)
        return {}


def get_config() -> NAPConfig:
    """Return the current configuration (reads file + env every call)."""
    file_values = _load_from_file()
    return NAPConfig(**file_values)


def update_config(changes: dict[str, Any]) -> NAPConfig:
    """Merge *changes* into the persistent config file and return the new config.

    Raises
    ------
    ValueError
        If any changed value fails Pydantic validation.
    """
    with _write_lock:
        current = _load_from_file()
        merged = {**current, **changes}
        # Validate by constructing a full NAPConfig from the merged dict.
        new_cfg = NAPConfig(**merged)
        path = _config_file_path()
        try:
            path.write_text(json.dumps(merged, indent=2))
        except OSError as exc:
            raise OSError(f"config_manager: cannot write {path}: {exc}") from exc
        logger.info("config_manager: configuration updated: %s", list(changes.keys()))
        return new_cfg
