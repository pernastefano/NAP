"""
websocket.py – WebSocket endpoint for live state-change pushes.

Clients connect to  ws://<host>/ws  and receive JSON messages whenever the
active audio source changes.  The connection is also used to send a
"connected" handshake and periodic keepalive pings so the client can detect
a stale connection quickly.

Message format
--------------
Server → client (state_changed):
    {
        "event": "state_changed",
        "source": "mpd",
        "systemd_target": "audio-mpd.target",
        "switch_count": 3,
        "switched_at": 12345.678
    }

Server → client (connected):
    {"event": "connected", "source": "mpd"}

Server → client (ping):
    {"event": "ping"}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from backend.app.state_manager import get_state_manager

logger = logging.getLogger(__name__)

ws_router = APIRouter()

_PING_INTERVAL = 20  # seconds between server-initiated pings


class ConnectionManager:
    """Tracks all live WebSocket connections and fans out broadcast messages."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    def register(self, ws: WebSocket) -> None:
        self._connections.add(ws)
        logger.debug("ws: client connected  total=%d", len(self._connections))

    def deregister(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        logger.debug("ws: client disconnected  total=%d", len(self._connections))

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Send *payload* to all connected clients; drop unresponsive ones."""
        if not self._connections:
            return
        message = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(message)
            except Exception as exc:  # noqa: BLE001
                logger.debug("ws: send failed (%s) – dropping client.", exc)
                dead.append(ws)
        for ws in dead:
            self.deregister(ws)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


# Module-level singleton – shared across the lifespan of the process.
manager = ConnectionManager()


@ws_router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    manager.register(ws)

    # Inject a synchronous broadcast shim into the StateManager so every
    # source switch reaches WebSocket clients without them having to poll.
    sm = get_state_manager()

    def _on_state_change(event: dict[str, Any]) -> None:
        """Called synchronously from the switch thread; schedule a coroutine."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(manager.broadcast(event), loop=loop)
        except RuntimeError:
            pass  # No running event loop in this thread – broadcast skipped.

    sm.subscribe(_on_state_change)

    try:
        # Send initial state so the client knows the current source immediately.
        info = sm.status()
        await ws.send_text(json.dumps({
            "event": "connected",
            "source": info.source.value,
            "systemd_target": info.systemd_target,
            "switch_count": info.switch_count,
        }))

        # Keep the connection alive with periodic pings; also drain any
        # client messages (we don't act on them, but we must read to detect
        # disconnection and prevent buffer exhaustion).
        while True:
            try:
                # Wait for a client message or timeout after PING_INTERVAL.
                text = await asyncio.wait_for(ws.receive_text(), timeout=_PING_INTERVAL)
                logger.debug("ws: received from client: %r (ignored)", text[:120])
            except asyncio.TimeoutError:
                # No message arrived; send a keepalive ping.
                try:
                    await ws.send_text(json.dumps({"event": "ping"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        logger.debug("ws: client disconnected cleanly.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ws: unexpected error: %s", exc)
    finally:
        sm.unsubscribe(_on_state_change)
        manager.deregister(ws)
