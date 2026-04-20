"""
main.py – FastAPI application entry point.

Start the server with:
    uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
or via the systemd nap-backend.service unit.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.app.api.routes import router
from backend.app.api.websocket import ws_router
from backend.app.api.ota import ota_router

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
from backend.app.config_manager import get_config
from backend.app.state_manager import create_state_manager, init_state_manager
from backend.app.utils.logger import configure_logging
from backend.app.ota_updater import OTAScheduler

logger = logging.getLogger(__name__)

_ota_scheduler: OTAScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle."""
    global _ota_scheduler

    cfg = get_config()
    configure_logging(level=cfg.log_level, max_lines=cfg.log_max_lines)

    logger.info("nap: starting up  log_level=%s", cfg.log_level)

    sm = create_state_manager()
    init_state_manager(sm)

    logger.info("nap: StateManager ready  default_source=%s", cfg.default_source)

    if cfg.ota_enabled:
        try:
            _ota_scheduler = OTAScheduler(cfg.ota_schedule_cron)
            _ota_scheduler.start()
        except Exception as exc:
            logger.warning("nap: OTA scheduler failed to start: %s", exc)

    yield  # ── application running ─────────────────────────────────────────────

    if _ota_scheduler is not None:
        _ota_scheduler.stop()
        _ota_scheduler = None

    logger.info("nap: shutting down.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Network Audio Player API",
        description=(
            "REST + WebSocket API for the NAP Raspberry Pi audio player.\n\n"
            "All source switches go through `systemctl isolate`; "
            "the ALSA lock prevents concurrent access."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # CORS – allow local Web UI served from the same host; tighten for
    # production by restricting allow_origins to the Pi's own address.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix="/api/v1")
    app.include_router(ota_router, prefix="/api/v1")
    app.include_router(ws_router)

    # Serve the static Web UI from /web/
    if _WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_WEB_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        async def serve_ui() -> FileResponse:
            return FileResponse(str(_WEB_DIR / "index.html"))

    return app


app = create_app()
