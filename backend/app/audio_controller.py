"""
audio_controller.py – Source-switching state machine for NAP.

Responsibilities
----------------
* Own the authoritative AudioSource state machine (IDLE → source → IDLE …).
* Guard every switch with the ALSA file lock so two concurrent callers cannot
  both issue systemctl isolate simultaneously.
* Invoke **systemctl isolate <target>** – never manage processes directly.
* Roll back to the previous source if the isolate command or its post-switch
  verification fails.
* Emit structured log events at every state transition so external log
  aggregators can build dashboards without parsing free-form text.

Public API
----------
    controller = AudioController()

    # Switch sources (blocking, thread-safe)
    controller.switch(AudioSource.MPD)

    # Read the current state at any time (lock-free)
    src: AudioSource = controller.current_source
    info: SourceInfo = controller.status()

Thread safety
-------------
`switch()` serialises itself via the ALSA lock.  `current_source` and
`status()` are read-only and safe to call from any thread without locking.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import subprocess
import time
from threading import RLock
from typing import Optional

from pathlib import Path

from backend.app.utils.audio_lock import (
    AudioLock,
    AudioLockError,
    AudioLockTimeout,
    GLOBAL_MAX_TIMEOUT,
    LOCK_FILE,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────


class AudioSource(enum.Enum):
    """All valid audio source states, including the neutral IDLE state."""

    IDLE = "idle"
    MPD = "mpd"
    AIRPLAY = "airplay"
    PLEXAMP = "plexamp"
    BLUETOOTH = "bluetooth"


# Maps every non-IDLE source to its systemd target.
_TARGETS: dict[AudioSource, str] = {
    AudioSource.MPD:       "audio-mpd.target",
    AudioSource.AIRPLAY:   "audio-airplay.target",
    AudioSource.PLEXAMP:   "audio-plexamp.target",
    AudioSource.BLUETOOTH: "audio-bluetooth.target",
}

# The synthetic "no active audio" target used to park the system.
_IDLE_TARGET = "multi-user.target"

# How long we wait for systemd to confirm the target is active after isolate.
_SYSTEMD_VERIFY_TIMEOUT: float = 10.0

# How long we wait to acquire the ALSA lock before rejecting a switch request.
_LOCK_TIMEOUT: float = 8.0

# Absolute paths – explicit so PATH manipulation cannot hijack them.
_SUDO      = "/usr/bin/sudo"
_SYSTEMCTL = "/bin/systemctl"


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────


class AudioControllerError(Exception):
    """Base class for all AudioController errors."""


class SwitchTimeout(AudioControllerError):
    """Raised when systemd does not confirm the target is active in time."""


class SwitchFailed(AudioControllerError):
    """Raised when systemctl isolate exits non-zero."""


class RollbackFailed(AudioControllerError):
    """Raised when roll-back to the previous source also fails.

    Attributes
    ----------
    original_error:
        The exception that triggered the rollback.
    rollback_error:
        The exception raised during the rollback attempt.
    """

    def __init__(
        self,
        original_error: Exception,
        rollback_error: Exception,
        *args: object,
    ) -> None:
        self.original_error = original_error
        self.rollback_error = rollback_error
        super().__init__(
            f"Switch failed ({original_error!r}) AND rollback also failed ({rollback_error!r}).",
            *args,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Status snapshot
# ──────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class SourceInfo:
    """Point-in-time snapshot of the controller state.

    Attributes
    ----------
    source:
        The currently active audio source (or IDLE).
    systemd_target:
        The systemd target that corresponds to *source*.
    switched_at:
        ``time.monotonic()`` value recorded when the last switch completed.
        ``None`` means no switch has happened since the controller was created.
    switch_count:
        Total number of successful switches since startup.
    """

    source: AudioSource
    systemd_target: str
    switched_at: Optional[float]
    switch_count: int


# ──────────────────────────────────────────────────────────────────────────────
# Controller
# ──────────────────────────────────────────────────────────────────────────────


class AudioController:
    """Thread-safe audio-source state machine.

    Parameters
    ----------
    lock_timeout:
        Seconds to wait for the ALSA lock before aborting a switch request.
        Defaults to ``_LOCK_TIMEOUT``.
    systemd_verify_timeout:
        Seconds to wait for systemd to report the new target as active.
        Defaults to ``_SYSTEMD_VERIFY_TIMEOUT``.
    lock_file:
        Path to the ALSA lock file.  Override in tests to avoid needing root.
        Defaults to ``LOCK_FILE`` (``/var/run/audio.lock``).
    """

    def __init__(
        self,
        lock_timeout: float = _LOCK_TIMEOUT,
        systemd_verify_timeout: float = _SYSTEMD_VERIFY_TIMEOUT,
        lock_file: Path = LOCK_FILE,
    ) -> None:
        if not (0 < lock_timeout <= GLOBAL_MAX_TIMEOUT):
            raise ValueError(
                f"lock_timeout must be in (0, {GLOBAL_MAX_TIMEOUT}], got {lock_timeout!r}."
            )

        self._lock_timeout = lock_timeout
        self._verify_timeout = systemd_verify_timeout
        self._lock_file = lock_file

        # Authoritative state – mutated only inside _do_switch(), which is
        # serialised by the ALSA lock.
        self._current: AudioSource = AudioSource.IDLE
        self._switched_at: Optional[float] = None
        self._switch_count: int = 0

        # A lightweight Python-level reentrant lock guards the state attributes
        # against torn reads on 32-bit platforms.  It does *not* replace the
        # ALSA file lock.
        self._state_lock = RLock()

    # ── read-only properties ──────────────────────────────────────────────────

    @property
    def current_source(self) -> AudioSource:
        """The currently active audio source (lock-free read)."""
        with self._state_lock:
            return self._current

    def status(self) -> SourceInfo:
        """Return a frozen snapshot of the current controller state."""
        with self._state_lock:
            return SourceInfo(
                source=self._current,
                systemd_target=_target_for(self._current),
                switched_at=self._switched_at,
                switch_count=self._switch_count,
            )

    # ── public switch API ─────────────────────────────────────────────────────

    def switch(self, source: AudioSource) -> SourceInfo:
        """Switch the active audio source.

        The call is synchronous: it returns only after systemd confirms the new
        target is active (or raises on failure/timeout).

        Switching to the current source is a no-op (returns immediately).
        Switching to ``AudioSource.IDLE`` stops all managed audio targets.

        Parameters
        ----------
        source:
            The desired audio source.

        Returns
        -------
        SourceInfo
            Snapshot reflecting the *new* state.

        Raises
        ------
        AudioLockTimeout
            Another switch is already in progress; try again later.
        SwitchFailed
            systemctl isolate exited non-zero.
        SwitchTimeout
            systemd did not confirm the target within the verify timeout.
        RollbackFailed
            The switch failed *and* the automatic rollback to the previous
            source also failed — the system is in an unknown state.
        """
        with self._state_lock:
            previous = self._current

        if previous == source:
            logger.info(
                "audio_controller: switch to %r requested but already active – no-op.",
                source.value,
            )
            return self.status()

        logger.info(
            "audio_controller: switch requested  %r → %r",
            previous.value,
            source.value,
        )

        try:
            with AudioLock(owner=source.value, timeout=self._lock_timeout, lock_file=self._lock_file):
                self._do_switch(source, previous)
        except AudioLockTimeout:
            logger.warning(
                "audio_controller: switch to %r rejected – lock not available within %.1fs.",
                source.value,
                self._lock_timeout,
            )
            raise

        return self.status()

    # ── internal state-machine core ────────────────────────────────────────────

    def _do_switch(self, target: AudioSource, previous: AudioSource) -> None:
        """Execute the switch; roll back on any failure.

        Called only from inside an active AudioLock context.
        """
        target_name = _target_for(target)
        t_start = time.monotonic()

        # ── 1. Isolate the new target ─────────────────────────────────────────
        try:
            _isolate(target_name)
        except SwitchFailed as exc:
            logger.error(
                "audio_controller: isolate %r failed – initiating rollback to %r. error=%s",
                target_name,
                previous.value,
                exc,
            )
            self._rollback(previous, caused_by=exc)
            raise  # re-raise the original error after a successful rollback

        # ── 2. Verify the target is actually active ────────────────────────────
        try:
            _verify_active(target_name, timeout=self._verify_timeout)
        except SwitchTimeout as exc:
            logger.error(
                "audio_controller: target %r did not become active within %.1fs – rolling back.",
                target_name,
                self._verify_timeout,
            )
            self._rollback(previous, caused_by=exc)
            raise

        # ── 3. Commit state ────────────────────────────────────────────────────
        elapsed = time.monotonic() - t_start
        with self._state_lock:
            self._current = target
            self._switched_at = time.monotonic()
            self._switch_count += 1

        logger.info(
            "audio_controller: ✓ switched to %r  target=%r  elapsed=%.2fs  total_switches=%d",
            target.value,
            target_name,
            elapsed,
            self._switch_count,
        )

    def _rollback(self, previous: AudioSource, caused_by: Exception) -> None:
        """Attempt to return to *previous*; raise RollbackFailed if that also fails."""
        prev_target = _target_for(previous)
        logger.warning(
            "audio_controller: rolling back to %r  target=%r",
            previous.value,
            prev_target,
        )

        try:
            _isolate(prev_target)
            _verify_active(prev_target, timeout=self._verify_timeout)
        except (SwitchFailed, SwitchTimeout) as rb_exc:
            # Both the forward switch and the rollback failed.
            # State remains as it was; log a critical event so the watchdog
            # can pick it up.
            logger.critical(
                "audio_controller: ROLLBACK FAILED  previous=%r  target=%r  "
                "original_error=%r  rollback_error=%r  – system audio state is UNKNOWN.",
                previous.value,
                prev_target,
                caused_by,
                rb_exc,
            )
            raise RollbackFailed(caused_by, rb_exc) from rb_exc

        # Rollback succeeded – restore state.
        with self._state_lock:
            self._current = previous

        logger.info(
            "audio_controller: rollback successful – restored to %r.", previous.value
        )


# ──────────────────────────────────────────────────────────────────────────────
# systemd helpers  (module-level so they can be patched in unit tests)
# ──────────────────────────────────────────────────────────────────────────────


def _target_for(source: AudioSource) -> str:
    """Return the systemd target name for *source*."""
    return _TARGETS.get(source, _IDLE_TARGET)


def _isolate(target: str) -> None:
    """Run ``systemctl isolate <target>``.

    Raises
    ------
    SwitchFailed
        systemctl exited non-zero.
    """
    logger.debug("audio_controller: systemctl isolate %r", target)
    try:
        result = subprocess.run(
            [_SUDO, _SYSTEMCTL, "isolate", target],
            capture_output=True,
            timeout=30,  # systemd should never take longer than this
        )
    except subprocess.TimeoutExpired as exc:
        raise SwitchFailed(
            f"systemctl isolate {target!r} timed out after 30 s."
        ) from exc
    except FileNotFoundError as exc:
        raise SwitchFailed(
            f"systemctl not found at {_SYSTEMCTL!r}. Is this a systemd host?"
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise SwitchFailed(
            f"systemctl isolate {target!r} exited {result.returncode}: {stderr}"
        )


def _verify_active(target: str, timeout: float) -> None:
    """Poll ``systemctl is-active <target>`` until it reports *active*.

    Raises
    ------
    SwitchTimeout
        Target not active within *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    poll = 0.2  # seconds between polls

    while True:
        try:
            result = subprocess.run(
                [_SYSTEMCTL, "is-active", target],
                capture_output=True,
                timeout=5,
            )
            state = result.stdout.decode().strip()
        except subprocess.TimeoutExpired:
            state = "timeout"

        if state == "active":
            logger.debug("audio_controller: verified %r is active.", target)
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SwitchTimeout(
                f"Target {target!r} not active after {timeout:.1f}s "
                f"(last systemd state: {state!r})."
            )

        # Back off slightly on repeated polls to reduce systemd pressure,
        # but never sleep past the deadline.
        time.sleep(min(poll, remaining))
        poll = min(poll * 1.5, 1.0)  # exponential back-off, capped at 1 s
