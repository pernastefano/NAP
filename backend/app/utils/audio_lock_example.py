"""
audio_lock_example.py – Shows how audio_controller.py should use AudioLock.

This file is not part of the production runtime; it documents the call patterns
that every source-switch path must follow.
"""

from __future__ import annotations

import logging
import subprocess

from backend.app.utils.audio_lock import AudioLock, AudioLockError, AudioLockTimeout, current_holder

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Pattern 1 – preferred: context manager (auto-release even on exception)
# ──────────────────────────────────────────────────────────────────────────────


def switch_to_mpd() -> None:
    """Switch the active audio source to MPD."""
    try:
        with AudioLock(owner="mpd", timeout=5.0):
            # systemd atomically stops conflicting targets and starts this one.
            subprocess.run(
                ["systemctl", "isolate", "audio-mpd.target"],
                check=True,
                capture_output=True,
            )
            logger.info("Switched to MPD.")
    except AudioLockTimeout as exc:
        # Another switch is in progress; surface this to the caller / Web UI.
        logger.warning("Switch to MPD rejected: %s", exc)
        raise
    except AudioLockError as exc:
        logger.error("Lock error during switch to MPD: %s", exc)
        raise
    except subprocess.CalledProcessError as exc:
        # systemd command failed; lock is still released by context manager.
        logger.error("systemctl isolate failed: %s", exc.stderr.decode().strip())
        raise


# ──────────────────────────────────────────────────────────────────────────────
# Pattern 2 – generic helper used by audio_controller.py for all sources
# ──────────────────────────────────────────────────────────────────────────────

_SOURCE_TARGETS: dict[str, str] = {
    "mpd":       "audio-mpd.target",
    "airplay":   "audio-airplay.target",
    "plexamp":   "audio-plexamp.target",
    "bluetooth": "audio-bluetooth.target",
}


def switch_source(source: str, timeout: float = 5.0) -> None:
    """Switch the active audio source.

    Parameters
    ----------
    source:
        One of "mpd", "airplay", "plexamp", "bluetooth".
    timeout:
        Seconds to wait for the lock before aborting.

    Raises
    ------
    ValueError
        Unknown source name.
    AudioLockTimeout
        Another switch is already in progress.
    subprocess.CalledProcessError
        systemctl isolate failed.
    """
    target = _SOURCE_TARGETS.get(source)
    if target is None:
        raise ValueError(f"Unknown audio source: {source!r}. Choose from {list(_SOURCE_TARGETS)}")

    logger.debug("Requested switch to source=%r target=%r", source, target)

    with AudioLock(owner=source, timeout=timeout):
        subprocess.run(
            ["systemctl", "isolate", target],
            check=True,
            capture_output=True,
        )
        logger.info("Active audio source is now %r.", source)


# ──────────────────────────────────────────────────────────────────────────────
# Pattern 3 – status inspection (no lock required)
# ──────────────────────────────────────────────────────────────────────────────


def get_current_source() -> str:
    """Return the name of the current audio source without acquiring the lock."""
    return current_holder()


# ──────────────────────────────────────────────────────────────────────────────
# Quick self-test (run directly: python -m backend.app.utils.audio_lock_example)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    from backend.app.utils.audio_lock import AudioLock, GLOBAL_MAX_TIMEOUT

    # Use a temp file so the example runs without root.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".lock") as tmp:
        tmp_path = Path(tmp.name)

    print("── Test 1: basic acquire / release ──")
    lock = AudioLock(owner="mpd", timeout=2.0, lock_file=tmp_path)
    lock.acquire()
    print(f"  held={lock.is_held}, owner={lock.owner}")
    lock.release()
    print(f"  held={lock.is_held}")

    print("\n── Test 2: context manager ──")
    with AudioLock(owner="airplay", timeout=2.0, lock_file=tmp_path) as lk:
        print(f"  inside context: held={lk.is_held}")
    print(f"  after context:  held={lk.is_held}")

    print("\n── Test 3: timeout when already locked ──")
    outer = AudioLock(owner="plexamp", timeout=2.0, lock_file=tmp_path)
    outer.acquire()
    try:
        inner = AudioLock(owner="bluetooth", timeout=0.2, lock_file=tmp_path)
        inner.acquire()
    except AudioLockTimeout as e:
        print(f"  Correctly timed out: {e}")
    finally:
        outer.release()

    print("\n── Test 4: double-acquire guard ──")
    dbl = AudioLock(owner="mpd", timeout=1.0, lock_file=tmp_path)
    dbl.acquire()
    try:
        dbl.acquire()
    except RuntimeError as e:
        print(f"  Correctly raised RuntimeError: {e}")
    finally:
        dbl.release()

    tmp_path.unlink(missing_ok=True)
    print("\nAll tests passed.")
