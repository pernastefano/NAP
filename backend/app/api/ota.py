"""
ota.py – OTA update REST endpoint.

The update logic lives in ``ota_updater.py``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from backend.app.ota_updater import (
    OTAAlreadyRunning,
    OTAError,
    get_history,
    read_version,
    run_update,
)

logger = logging.getLogger(__name__)

ota_router = APIRouter(tags=["system"])


class OTAResponse(BaseModel):
    ok: bool
    message: str
    version: str = ""
    previous_version: str = ""
    rolled_back: bool = False


class OTAHistoryResponse(BaseModel):
    history: list[dict]


@ota_router.post("/ota/update", response_model=OTAResponse)
async def trigger_ota_update() -> OTAResponse:
    """Trigger an OTA update from GitHub.

    The update runs in a thread-pool executor so the event loop is not
    blocked during the git fetch / pull.
    """
    logger.info("ota: update requested via API.")
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, run_update)
    except OTAAlreadyRunning as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except OTAError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    return OTAResponse(
        ok=result.ok,
        message=result.message,
        version=result.new_version,
        previous_version=result.previous_version,
        rolled_back=result.rolled_back,
    )


@ota_router.get("/ota/version")
async def get_version() -> dict:
    """Return the current application version string."""
    return {"version": read_version()}


@ota_router.get("/ota/history", response_model=OTAHistoryResponse)
async def get_update_history() -> OTAHistoryResponse:
    """Return the last 50 OTA update history entries."""
    return OTAHistoryResponse(history=get_history())
