"""
state_manager.py – Thin wrapper that couples AudioController to the event bus.

Every successful source switch publishes a StateEvent so WebSocket clients
receive a push update without polling.

Usage
-----
    from backend.app.state_manager import get_state_manager
    sm = get_state_manager()

    # blocking switch + broadcast
    sm.switch(AudioSource.MPD)

    # read-only
    info = sm.status()
"""

from __future__ import annotations

import logging
from typing import Callable, Any

from backend.app.audio_controller import AudioController, AudioSource, SourceInfo
from backend.app.config_manager import get_config

logger = logging.getLogger(__name__)

# Module-level singleton – created once in main.py startup.
_state_manager: "StateManager | None" = None


class StateManager:
    """Wraps AudioController and notifies the event bus on every state change."""

    def __init__(self, controller: AudioController) -> None:
        self._controller = controller
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    # ── public API ─────────────────────────────────────────────────────────────

    def switch(self, source: AudioSource) -> SourceInfo:
        """Switch source and broadcast the new state to all registered listeners."""
        info = self._controller.switch(source)
        self._broadcast(_info_to_event(info))
        return info

    def status(self) -> SourceInfo:
        return self._controller.status()

    @property
    def current_source(self) -> AudioSource:
        return self._controller.current_source

    # ── event-bus ──────────────────────────────────────────────────────────────

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Register a listener that is called synchronously on every state change."""
        self._listeners.append(fn)

    def unsubscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass

    def _broadcast(self, event: dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning("state_manager: listener %r raised: %s", fn, exc)


# ── module-level helpers ───────────────────────────────────────────────────────


def _info_to_event(info: SourceInfo) -> dict[str, Any]:
    return {
        "event": "state_changed",
        "source": info.source.value,
        "systemd_target": info.systemd_target,
        "switch_count": info.switch_count,
        "switched_at": info.switched_at,
    }


def create_state_manager() -> "StateManager":
    """Build a StateManager from the current configuration."""
    cfg = get_config()
    controller = AudioController(
        lock_timeout=cfg.lock_timeout,
        systemd_verify_timeout=cfg.systemd_verify_timeout,
    )
    return StateManager(controller)


def init_state_manager(sm: "StateManager") -> None:
    global _state_manager
    _state_manager = sm


def get_state_manager() -> "StateManager":
    if _state_manager is None:
        raise RuntimeError("StateManager has not been initialised. Call init_state_manager() first.")
    return _state_manager
