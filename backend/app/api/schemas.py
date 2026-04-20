"""
schemas.py – Pydantic request / response models for the REST API.
"""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


# ── source / state ─────────────────────────────────────────────────────────────

class SourceResponse(BaseModel):
    source: str
    systemd_target: str
    switch_count: int
    switched_at: Optional[float] = None


class SwitchRequest(BaseModel):
    source: str = Field(..., description="Target audio source: mpd | airplay | plexamp | bluetooth | idle")


class SwitchResponse(BaseModel):
    ok: bool
    source: str
    systemd_target: str
    switch_count: int


# ── playback controls ─────────────────────────────────────────────────────────

class PlaybackAction(BaseModel):
    action: str = Field(
        ...,
        description="One of: play, pause, stop, next, previous, volume_up, volume_down, set_volume",
    )
    value: Optional[int] = Field(None, description="Required for set_volume (0–100).")


class PlaybackResponse(BaseModel):
    ok: bool
    action: str
    detail: Optional[str] = None


# ── config ────────────────────────────────────────────────────────────────────

class ConfigResponse(BaseModel):
    config: dict[str, Any]


class ConfigUpdateRequest(BaseModel):
    changes: dict[str, Any] = Field(..., description="Key/value pairs to update in config.json.")


# ── logs ──────────────────────────────────────────────────────────────────────

class LogEntry(BaseModel):
    ts: str
    level: str
    logger: str
    msg: str


class LogsResponse(BaseModel):
    count: int
    entries: list[LogEntry]


# ── health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    source: str
    version: str = "0.1.0"
