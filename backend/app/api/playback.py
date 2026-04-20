"""
playback.py – Thin adapter that translates REST playback actions into
systemd/MPD/ALSA commands for the currently active source.

Only MPD commands are implemented in full (via mpc).  For Plexamp and
AirPlay, the relevant CLIs / D-Bus interfaces are thin stubs ready to be
filled in once the hardware is confirmed.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# Path to mpc – the MPD command-line client.
_MPC = "/usr/bin/mpc"
_AMIXER = "/usr/bin/amixer"

# Valid action names, used for input validation.
VALID_ACTIONS = frozenset({
    "play", "pause", "stop", "next", "previous",
    "volume_up", "volume_down", "set_volume",
})


class PlaybackError(Exception):
    pass


def dispatch(source: str, action: str, value: Optional[int] = None) -> str:
    """Execute *action* on the currently active *source*.

    Returns a human-readable detail string on success.
    Raises PlaybackError on failure.
    """
    if action not in VALID_ACTIONS:
        raise PlaybackError(f"Unknown action {action!r}. Valid: {sorted(VALID_ACTIONS)}")

    if action == "set_volume":
        if value is None or not (0 <= value <= 100):
            raise PlaybackError("set_volume requires value in [0, 100].")
        return _set_volume(value)

    if source == "mpd":
        return _mpd_action(action)
    if source == "airplay":
        return _airplay_action(action)
    if source == "plexamp":
        return _plexamp_action(action)
    if source == "bluetooth":
        return _bluetooth_action(action)
    if source == "idle":
        raise PlaybackError("No active source (state is idle).")
    raise PlaybackError(f"Unknown source {source!r}.")


# ── per-source implementations ─────────────────────────────────────────────────

_MPD_COMMANDS: dict[str, list[str]] = {
    "play":     [_MPC, "play"],
    "pause":    [_MPC, "pause"],
    "stop":     [_MPC, "stop"],
    "next":     [_MPC, "next"],
    "previous": [_MPC, "prev"],
    "volume_up":   [_MPC, "volume", "+5"],
    "volume_down": [_MPC, "volume", "-5"],
}


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=5)
        if result.returncode != 0:
            raise PlaybackError(
                f"Command {cmd[0]!r} failed (exit {result.returncode}): "
                f"{result.stderr.decode(errors='replace').strip()}"
            )
        return result.stdout.decode(errors="replace").strip()
    except FileNotFoundError:
        raise PlaybackError(f"Command not found: {cmd[0]!r}") from None
    except subprocess.TimeoutExpired:
        raise PlaybackError(f"Command timed out: {' '.join(cmd)!r}") from None


def _mpd_action(action: str) -> str:
    cmd = _MPD_COMMANDS.get(action)
    if cmd is None:
        raise PlaybackError(f"MPD does not support action {action!r}.")
    return _run(cmd) or action


def _set_volume(level: int) -> str:
    return _run([_AMIXER, "set", "Master", f"{level}%"])


def _airplay_action(action: str) -> str:
    # AirPlay (shairport-sync) is receiver-only; playback is controlled on
    # the sending device.  Volume changes can be relayed via ALSA.
    if action in ("volume_up", "volume_down"):
        delta = "+5%" if action == "volume_up" else "-5%"
        return _run([_AMIXER, "set", "Master", delta])
    raise PlaybackError(f"Action {action!r} is not supported for AirPlay (sender controls playback).")


def _plexamp_action(action: str) -> str:
    # Plexamp headless exposes a local HTTP API on port 32500.
    # Placeholder: replace with actual HTTP call once port is confirmed.
    logger.warning("playback: Plexamp action %r is a stub – not yet implemented.", action)
    raise PlaybackError(f"Plexamp playback control not yet implemented (action={action!r}).")


def _bluetooth_action(action: str) -> str:
    # Bluetooth A2DP sink; playback is sender-controlled.
    # AVRCP target commands could be sent via bluetoothctl / D-Bus here.
    if action in ("volume_up", "volume_down"):
        delta = "+5%" if action == "volume_up" else "-5%"
        return _run([_AMIXER, "set", "Master", delta])
    raise PlaybackError(f"Action {action!r} is not supported for Bluetooth sink.")
