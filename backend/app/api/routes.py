"""
routes.py – All REST API routes for NAP.

Routers are registered in main.py via app.include_router().
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from backend.app.api.playback import PlaybackError, dispatch
from backend.app.api.schemas import (
    ConfigResponse,
    ConfigUpdateRequest,
    HealthResponse,
    LogEntry,
    LogsResponse,
    PlaybackAction,
    PlaybackResponse,
    SourceResponse,
    SwitchRequest,
    SwitchResponse,
)
from backend.app.audio_controller import AudioSource, RollbackFailed, SwitchFailed, SwitchTimeout
from backend.app.config_manager import get_config, update_config
from backend.app.state_manager import get_state_manager
from backend.app.utils.audio_lock import AudioLockTimeout
from backend.app.utils.logger import get_recent_logs

logger = logging.getLogger(__name__)

router = APIRouter()


# ── health ─────────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    """Liveness probe.  Returns the current audio source and process health."""
    sm = get_state_manager()
    return HealthResponse(status="ok", source=sm.current_source.value)


# ── source / state ─────────────────────────────────────────────────────────────

@router.get("/source", response_model=SourceResponse, tags=["audio"])
def get_source() -> SourceResponse:
    """Return the currently active audio source."""
    info = get_state_manager().status()
    return SourceResponse(
        source=info.source.value,
        systemd_target=info.systemd_target,
        switch_count=info.switch_count,
        switched_at=info.switched_at,
    )


@router.post("/source", response_model=SwitchResponse, tags=["audio"])
def switch_source(body: SwitchRequest) -> SwitchResponse:
    """Switch the active audio source.

    Blocks until systemd confirms the new target is active (or returns an
    error).  The previous source is automatically rolled back on failure.
    """
    try:
        src = AudioSource(body.source.lower())
    except ValueError:
        valid = [s.value for s in AudioSource]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid source {body.source!r}. Valid values: {valid}",
        )

    try:
        info = get_state_manager().switch(src)
    except AudioLockTimeout as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Switch rejected: another switch is in progress. {exc}",
        )
    except (SwitchFailed, SwitchTimeout) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"systemd switch failed: {exc}",
        )
    except RollbackFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Switch AND rollback both failed – system state unknown. {exc}",
        )

    return SwitchResponse(
        ok=True,
        source=info.source.value,
        systemd_target=info.systemd_target,
        switch_count=info.switch_count,
    )


# ── playback controls ─────────────────────────────────────────────────────────

@router.post("/playback", response_model=PlaybackResponse, tags=["audio"])
def playback_action(body: PlaybackAction) -> PlaybackResponse:
    """Send a playback command to the currently active source.

    Supported actions depend on the active source:
    - **mpd**: play, pause, stop, next, previous, volume_up, volume_down, set_volume
    - **airplay** / **bluetooth**: volume_up, volume_down, set_volume only
    - **plexamp**: not yet implemented
    - **idle**: always returns 409
    """
    source = get_state_manager().current_source.value
    try:
        detail = dispatch(source=source, action=body.action, value=body.value)
    except PlaybackError as exc:
        if "not supported" in str(exc) or "not yet implemented" in str(exc):
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc))
        if source == "idle":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    return PlaybackResponse(ok=True, action=body.action, detail=detail or None)


# ── config ────────────────────────────────────────────────────────────────────

@router.get("/config", response_model=ConfigResponse, tags=["system"])
def read_config() -> ConfigResponse:
    """Return the current configuration (sensitive fields redacted)."""
    cfg = get_config()
    return ConfigResponse(config=cfg.model_dump())


@router.patch("/config", response_model=ConfigResponse, tags=["system"])
def patch_config(body: ConfigUpdateRequest) -> ConfigResponse:
    """Merge *changes* into the persistent configuration.

    Only keys that are defined in NAPConfig are accepted.  Unknown keys are
    rejected.  Validation (value ranges, types) is enforced by Pydantic.
    """
    from pydantic import ValidationError
    from backend.app.config_manager import NAPConfig

    # Reject keys that don't exist in the schema.
    known_keys = set(NAPConfig.model_fields.keys())
    unknown = set(body.changes.keys()) - known_keys
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown config keys: {sorted(unknown)}. Valid keys: {sorted(known_keys)}",
        )

    try:
        new_cfg = update_config(body.changes)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        )
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not write config file: {exc}",
        )

    return ConfigResponse(config=new_cfg.model_dump())


# ── logs ──────────────────────────────────────────────────────────────────────

@router.get("/logs", response_model=LogsResponse, tags=["system"])
def get_logs(
    n: int = Query(100, ge=1, le=2000, description="Number of most-recent log lines to return."),
    level: str = Query("DEBUG", description="Minimum log level filter."),
) -> LogsResponse:
    """Return the most-recent *n* structured log entries from the in-memory buffer."""
    level_upper = level.upper()
    level_rank = {
        "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50,
    }
    min_rank = level_rank.get(level_upper, 0)

    raw = get_recent_logs(n=None)
    filtered = [e for e in raw if level_rank.get(e["level"], 0) >= min_rank]
    recent = filtered[-n:]

    entries = [
        LogEntry(ts=e["ts"], level=e["level"], logger=e["logger"], msg=e["msg"])
        for e in recent
    ]
    return LogsResponse(count=len(entries), entries=entries)
