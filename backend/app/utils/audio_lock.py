"""
audio_lock.py – Exclusive ALSA lock for NAP.

Rules:
  - Only one audio source may hold the lock at a time.
  - The lock is a POSIX advisory file lock (flock(2)) on /var/run/audio.lock.
  - The lock file also stores the current owner name so operators can inspect
    it with  cat /var/run/audio.lock  without needing root tools.
  - Acquiring the lock is always time-bounded; callers cannot block forever.
  - The lock is released automatically when the context-manager exits, even if
    the process crashes (the kernel releases flock locks on process death).

Usage (preferred – context manager):
    from backend.app.utils.audio_lock import AudioLock, AudioLockError

    try:
        with AudioLock(owner="mpd", timeout=5.0):
            subprocess.run(["systemctl", "isolate", "audio-mpd.target"], check=True)
    except AudioLockError as exc:
        logger.error("Could not acquire audio lock: %s", exc)

Usage (manual acquire/release – avoid unless you have a good reason):
    lock = AudioLock(owner="airplay", timeout=5.0)
    lock.acquire()
    try:
        ...
    finally:
        lock.release()
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import time
from pathlib import Path
from types import TracebackType
from typing import Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Public constants
# ──────────────────────────────────────────────────────────────────────────────

LOCK_FILE: Path = Path("/run/audio.lock")

# Maximum time (seconds) any caller will wait before giving up.
# Individual callers may pass a shorter timeout; they may never pass a longer
# one because that would risk starvation.
GLOBAL_MAX_TIMEOUT: float = 15.0

# Polling interval while waiting for the lock to become available.
_POLL_INTERVAL: float = 0.05  # 50 ms


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────


class AudioLockError(Exception):
    """Base class for all audio-lock errors."""


class AudioLockTimeout(AudioLockError):
    """Raised when the lock cannot be acquired within the allowed time."""


class AudioLockPermissionError(AudioLockError):
    """Raised when the process lacks permission to create or lock the file."""


# ──────────────────────────────────────────────────────────────────────────────
# Core implementation
# ──────────────────────────────────────────────────────────────────────────────


class AudioLock:
    """Exclusive, time-bounded advisory lock backed by flock(2).

    Parameters
    ----------
    owner:
        Human-readable name of the audio source requesting the lock
        (e.g. "mpd", "airplay"). Written to the lock file so the current
        holder is visible without external tools.
    timeout:
        Maximum seconds to wait for the lock.  Must be > 0 and <=
        GLOBAL_MAX_TIMEOUT.  Defaults to GLOBAL_MAX_TIMEOUT.
    lock_file:
        Override the lock-file path (useful in tests).
    """

    def __init__(
        self,
        owner: str,
        timeout: float = GLOBAL_MAX_TIMEOUT,
        lock_file: Path = LOCK_FILE,
    ) -> None:
        if not owner or not owner.strip():
            raise ValueError("AudioLock owner must be a non-empty string.")
        if not (0 < timeout <= GLOBAL_MAX_TIMEOUT):
            raise ValueError(
                f"timeout must be in (0, {GLOBAL_MAX_TIMEOUT}], got {timeout!r}."
            )

        self._owner = owner.strip()
        self._timeout = timeout
        self._lock_file = lock_file
        self._fd: Optional[int] = None  # file descriptor; None → not held

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def is_held(self) -> bool:
        """True if *this* instance currently holds the lock."""
        return self._fd is not None

    @property
    def owner(self) -> str:
        return self._owner

    # ── context-manager interface ──────────────────────────────────────────────

    def __enter__(self) -> "AudioLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.release()

    # ── public API ─────────────────────────────────────────────────────────────

    def acquire(self) -> None:
        """Acquire the exclusive lock.

        Blocks (with polling) up to *timeout* seconds.

        Raises
        ------
        AudioLockTimeout
            Lock not available within the timeout window.
        AudioLockPermissionError
            Insufficient OS permissions to create or lock the file.
        AudioLockError
            Any other unexpected OS error.
        RuntimeError
            Called on an instance that already holds the lock (reentrant
            acquire is not supported and almost certainly a bug).
        """
        if self.is_held:
            raise RuntimeError(
                f"AudioLock already held by this instance (owner={self._owner!r}). "
                "Release it before acquiring again."
            )

        fd = self._open_lock_file()
        deadline = time.monotonic() + self._timeout

        logger.debug(
            "audio_lock: %r waiting for lock (timeout=%.1fs)", self._owner, self._timeout
        )

        while True:
            try:
                # LOCK_EX | LOCK_NB → fail immediately if someone else holds it.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break  # acquired
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    os.close(fd)
                    raise AudioLockError(
                        f"Unexpected OS error while locking {self._lock_file}: {exc}"
                    ) from exc

                # Lock is held by another holder; check deadline.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    os.close(fd)
                    holder = _read_current_holder(self._lock_file)
                    raise AudioLockTimeout(
                        f"audio_lock: could not acquire within {self._timeout:.1f}s "
                        f"(current holder: {holder!r}, requested by: {self._owner!r})."
                    ) from None

                time.sleep(min(_POLL_INTERVAL, remaining))

        # Lock acquired – record ownership.
        self._fd = fd
        self._write_owner()
        elapsed = self._timeout - (deadline - time.monotonic())
        logger.info(
            "audio_lock: acquired by %r (waited %.2fs)", self._owner, max(elapsed, 0.0)
        )

    def release(self) -> None:
        """Release the lock.

        Safe to call even if the lock is not currently held (no-op).
        """
        if not self.is_held:
            return

        fd = self._fd
        self._fd = None  # Mark released *before* OS calls to stay consistent on error.

        try:
            # Clear the owner field so the file shows "idle" between switches.
            try:
                os.ftruncate(fd, 0)
                os.write(fd, b"idle\n")
            except OSError:
                pass  # Best-effort; lock release is more important.

            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        logger.info("audio_lock: released by %r", self._owner)

    # ── internal helpers ───────────────────────────────────────────────────────

    def _open_lock_file(self) -> int:
        """Open (creating if necessary) the lock file; return its fd."""
        try:
            # O_CREAT | O_WRONLY: create the file if it doesn't exist.
            # 0o644: world-readable so any user can inspect the current holder.
            fd = os.open(
                self._lock_file,
                os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC,
                0o644,
            )
        except PermissionError as exc:
            raise AudioLockPermissionError(
                f"Cannot open lock file {self._lock_file}: {exc}. "
                "Ensure the process runs as a user with write access to /var/run/."
            ) from exc
        except OSError as exc:
            raise AudioLockError(
                f"Cannot open lock file {self._lock_file}: {exc}"
            ) from exc
        return fd

    def _write_owner(self) -> None:
        """Overwrite the lock file with the current owner name."""
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, f"{self._owner}\n".encode())
        except OSError as exc:
            # Non-fatal: the lock is still held; only the metadata is missing.
            logger.warning("audio_lock: could not write owner to lock file: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _read_current_holder(lock_file: Path) -> str:
    """Return the current owner string from the lock file (best-effort)."""
    try:
        return lock_file.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def current_holder(lock_file: Path = LOCK_FILE) -> str:
    """Return the name of the service currently holding the audio lock.

    Returns ``"idle"`` if no service holds it, ``"unknown"`` if the file
    cannot be read.  Never raises.
    """
    return _read_current_holder(lock_file)
