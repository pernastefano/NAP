"""
ota_updater.py – Git-based OTA updater for NAP.

Strategy
--------
1. ``git fetch origin`` – download remote refs without touching the working tree.
2. Compare HEAD to origin/<branch>.  If already up-to-date, stop.
3. Create a rollback snapshot: record current commit hash.
4. ``git stash`` local uncommitted changes (idempotent on clean installs).
5. ``git pull --ff-only origin <branch>`` – fast-forward only; reject force-pushes.
6. Run the post-update hook (pip install -r requirements.txt, etc.).
7. Verify the application is still importable (``python3 -c "import backend.app.main"``).
8. Write a VERSION file and signal systemd to restart the service.
9. On *any* failure in steps 5-8: ``git reset --hard <rollback_hash>`` and re-run
   the post-update hook to restore the prior state.

Scheduled trigger
-----------------
``OTAScheduler`` parses a cron expression and runs the update in a background
thread.  Only hour/minute/weekday/day-of-month/month fields are supported
(seconds are not, matching standard cron).

Design constraints
------------------
* No network calls from Python (urllib / requests) – git speaks to GitHub.
* The updater must never leave the working tree in a broken half-applied state.
* subprocess is used with explicit timeouts so a hung git never stalls the API.
* All mutations are protected by a module-level lock so concurrent API calls
  and the scheduler cannot race each other.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants / paths
# ──────────────────────────────────────────────────────────────────────────────

# Root of the git repository (two levels up from this file:
#   backend/app/ota_updater.py  →  NAP/
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent

# Written after every successful update; read by the API for the version field.
_VERSION_FILE: Path = _REPO_ROOT / "VERSION"

# Post-update hook executed after every successful pull.
_POST_HOOK: list[str] = [
    "pip3", "install", "--quiet", "--break-system-packages",
    "-r", str(_REPO_ROOT / "backend" / "requirements.txt"),
]

# Timeouts (seconds)
_GIT_FETCH_TIMEOUT  = 60
_GIT_PULL_TIMEOUT   = 60
_GIT_RESET_TIMEOUT  = 30
_HOOK_TIMEOUT       = 120
_VERIFY_TIMEOUT     = 20

# Serialise all mutation operations.
_update_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Result type
# ──────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class UpdateResult:
    ok: bool
    message: str
    previous_version: str = ""
    new_version: str = ""
    rolled_back: bool = False
    timestamp: str = dataclasses.field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class OTAError(Exception):
    """Fatal error during an update; rollback should be attempted."""


class OTAAlreadyRunning(Exception):
    """Another update is already in progress."""


class OTAUpToDate(Exception):
    """Repository is already at the latest commit; nothing to do."""


# ──────────────────────────────────────────────────────────────────────────────
# Low-level git helpers
# ──────────────────────────────────────────────────────────────────────────────

def _git(*args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in the repository root."""
    cmd = ["/usr/bin/git", "-C", str(_REPO_ROOT)] + list(args)
    logger.debug("ota: %s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=check,
        )
    except subprocess.TimeoutExpired as exc:
        raise OTAError(f"git command timed out after {timeout}s: {' '.join(args)}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise OTAError(f"git {args[0]} failed (exit {exc.returncode}): {stderr}") from exc


def _current_commit() -> str:
    result = _git("rev-parse", "HEAD")
    return result.stdout.decode().strip()


def _remote_commit(branch: str) -> str:
    result = _git("rev-parse", f"origin/{branch}")
    return result.stdout.decode().strip()


def _current_branch() -> str:
    result = _git("rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.decode().strip()


def _short(commit: str) -> str:
    return commit[:8]


def _stash_if_dirty() -> bool:
    """Stash uncommitted changes; return True if a stash was created."""
    result = _git("status", "--porcelain")
    if result.stdout.strip():
        _git("stash", "push", "--include-untracked", "-m", "nap-ota-pre-update")
        logger.info("ota: local changes stashed.")
        return True
    return False


def _pop_stash() -> None:
    result = _git("stash", "list", check=False)
    if b"nap-ota-pre-update" in result.stdout:
        _git("stash", "pop", check=False)
        logger.info("ota: stash restored.")


# ──────────────────────────────────────────────────────────────────────────────
# Post-update hook
# ──────────────────────────────────────────────────────────────────────────────

def _run_post_hook() -> None:
    logger.info("ota: running post-update hook: %s", " ".join(_POST_HOOK))
    try:
        result = subprocess.run(
            _POST_HOOK,
            capture_output=True,
            timeout=_HOOK_TIMEOUT,
            cwd=str(_REPO_ROOT),
        )
        if result.returncode != 0:
            raise OTAError(
                f"Post-hook exited {result.returncode}: "
                f"{result.stderr.decode(errors='replace').strip()}"
            )
    except subprocess.TimeoutExpired as exc:
        raise OTAError(f"Post-hook timed out after {_HOOK_TIMEOUT}s.") from exc


# ──────────────────────────────────────────────────────────────────────────────
# Application import verification
# ──────────────────────────────────────────────────────────────────────────────

def _verify_import() -> None:
    """Spawn a fresh Python process to verify the app still imports cleanly."""
    logger.info("ota: verifying application imports.")
    try:
        result = subprocess.run(
            ["python3", "-c", "import backend.app.main"],
            capture_output=True,
            timeout=_VERIFY_TIMEOUT,
            cwd=str(_REPO_ROOT),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise OTAError(f"Application import check failed: {stderr}")
    except subprocess.TimeoutExpired as exc:
        raise OTAError("Application import check timed out.") from exc


# ──────────────────────────────────────────────────────────────────────────────
# Version file
# ──────────────────────────────────────────────────────────────────────────────

def read_version() -> str:
    """Return the version string from VERSION (or the current git commit hash)."""
    if _VERSION_FILE.exists():
        return _VERSION_FILE.read_text().strip()
    try:
        return _short(_current_commit())
    except Exception:
        return "unknown"


def _write_version(commit: str) -> None:
    short = _short(commit)
    _VERSION_FILE.write_text(f"{short}\n")
    logger.info("ota: VERSION written: %s", short)


# ──────────────────────────────────────────────────────────────────────────────
# Restart
# ──────────────────────────────────────────────────────────────────────────────

def _request_restart() -> None:
    """Ask systemd to restart the nap-backend service.

    Uses SIGTERM to the current process as a fallback when systemctl is not
    available (e.g. during development).
    """
    try:
        result = subprocess.run(
            ["/bin/systemctl", "restart", "nap-backend.service"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info("ota: systemd restart requested for nap-backend.service.")
            return
    except Exception:
        pass

    # Fallback: send SIGTERM to self; uvicorn/gunicorn will restart the worker.
    logger.warning("ota: systemctl not available – sending SIGTERM to self.")
    import signal
    os.kill(os.getpid(), signal.SIGTERM)


# ──────────────────────────────────────────────────────────────────────────────
# Rollback
# ──────────────────────────────────────────────────────────────────────────────

def _rollback(to_commit: str) -> None:
    """Hard-reset the working tree to *to_commit* and re-run the post hook."""
    logger.warning("ota: rolling back to %s.", _short(to_commit))
    try:
        subprocess.run(
            ["/usr/bin/git", "-C", str(_REPO_ROOT), "reset", "--hard", to_commit],
            capture_output=True,
            timeout=_GIT_RESET_TIMEOUT,
            check=True,
        )
        _run_post_hook()
        logger.info("ota: rollback to %s complete.", _short(to_commit))
    except Exception as rb_exc:
        logger.critical(
            "ota: ROLLBACK FAILED – system is in an unknown state.  "
            "Manual intervention required.  error=%s", rb_exc
        )
        raise


# ──────────────────────────────────────────────────────────────────────────────
# History log
# ──────────────────────────────────────────────────────────────────────────────

_HISTORY_FILE: Path = _REPO_ROOT / "ota_history.json"


def _append_history(result: UpdateResult) -> None:
    history: list[dict] = []
    if _HISTORY_FILE.exists():
        try:
            history = json.loads(_HISTORY_FILE.read_text())
        except Exception:
            history = []
    history.append(result.to_dict())
    # Keep the last 50 entries
    history = history[-50:]
    try:
        _HISTORY_FILE.write_text(json.dumps(history, indent=2))
    except OSError as exc:
        logger.warning("ota: could not write update history: %s", exc)


def get_history() -> list[dict]:
    """Return the update history log (newest last)."""
    if not _HISTORY_FILE.exists():
        return []
    try:
        return json.loads(_HISTORY_FILE.read_text())
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Main update routine
# ──────────────────────────────────────────────────────────────────────────────

def run_update(branch: str = "") -> UpdateResult:
    """Perform a full OTA update cycle.

    Parameters
    ----------
    branch:
        Git branch to pull from.  Defaults to the current branch.

    Returns
    -------
    UpdateResult
        Describes what happened (success, already-up-to-date, rollback, …).

    Raises
    ------
    OTAAlreadyRunning
        Another update is already in progress.
    """
    if not _update_lock.acquire(blocking=False):
        raise OTAAlreadyRunning("An OTA update is already running.")

    try:
        return _run_update_locked(branch)
    finally:
        _update_lock.release()


def _run_update_locked(branch: str) -> UpdateResult:
    effective_branch = branch or _current_branch()
    logger.info("ota: starting update  branch=%s", effective_branch)

    # ── 1. Fetch ──────────────────────────────────────────────────────────────
    try:
        _git("fetch", "origin", timeout=_GIT_FETCH_TIMEOUT)
    except OTAError as exc:
        result = UpdateResult(ok=False, message=f"Fetch failed: {exc}",
                              previous_version=read_version())
        _append_history(result)
        return result

    # ── 2. Compare HEAD to remote ─────────────────────────────────────────────
    try:
        local  = _current_commit()
        remote = _remote_commit(effective_branch)
    except OTAError as exc:
        result = UpdateResult(ok=False, message=f"Commit resolution failed: {exc}",
                              previous_version=read_version())
        _append_history(result)
        return result

    if local == remote:
        logger.info("ota: already up-to-date at %s.", _short(local))
        result = UpdateResult(
            ok=True,
            message=f"Already up-to-date ({_short(local)}).",
            previous_version=_short(local),
            new_version=_short(local),
        )
        _append_history(result)
        return result

    prev_version = read_version()
    logger.info("ota: update available  %s → %s", _short(local), _short(remote))

    # ── 3. Stash ──────────────────────────────────────────────────────────────
    stashed = _stash_if_dirty()

    # ── 4. Pull ───────────────────────────────────────────────────────────────
    try:
        _git("pull", "--ff-only", "origin", effective_branch,
             timeout=_GIT_PULL_TIMEOUT)
    except OTAError as exc:
        logger.error("ota: pull failed: %s", exc)
        if stashed:
            _pop_stash()
        result = UpdateResult(
            ok=False,
            message=f"Pull failed: {exc}",
            previous_version=prev_version,
            rolled_back=False,
        )
        _append_history(result)
        return result

    # ── 5. Post-hook ──────────────────────────────────────────────────────────
    try:
        _run_post_hook()
    except OTAError as exc:
        logger.error("ota: post-hook failed: %s – rolling back.", exc)
        try:
            _rollback(local)
            if stashed:
                _pop_stash()
        except Exception:
            pass
        result = UpdateResult(
            ok=False,
            message=f"Post-hook failed; rolled back to {_short(local)}: {exc}",
            previous_version=prev_version,
            new_version=_short(remote),
            rolled_back=True,
        )
        _append_history(result)
        return result

    # ── 6. Import verification ────────────────────────────────────────────────
    try:
        _verify_import()
    except OTAError as exc:
        logger.error("ota: import check failed: %s – rolling back.", exc)
        try:
            _rollback(local)
            if stashed:
                _pop_stash()
        except Exception:
            pass
        result = UpdateResult(
            ok=False,
            message=f"Import check failed; rolled back to {_short(local)}: {exc}",
            previous_version=prev_version,
            new_version=_short(remote),
            rolled_back=True,
        )
        _append_history(result)
        return result

    # ── 7. Commit ─────────────────────────────────────────────────────────────
    if stashed:
        _pop_stash()
    _write_version(remote)

    new_version = _short(remote)
    logger.info("ota: update successful  %s → %s", _short(local), new_version)

    result = UpdateResult(
        ok=True,
        message=f"Updated {_short(local)} → {new_version}.",
        previous_version=prev_version,
        new_version=new_version,
    )
    _append_history(result)

    # ── 8. Restart ────────────────────────────────────────────────────────────
    # Schedule restart after this function returns so the API has time to
    # send the JSON response before the process exits.
    threading.Timer(1.5, _request_restart).start()

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Scheduler
# ──────────────────────────────────────────────────────────────────────────────

class OTAScheduler:
    """Run ``run_update()`` on a cron-like schedule in a background thread.

    Supports standard 5-field cron expressions:
        ``minute  hour  day-of-month  month  day-of-week``

    Wildcards (``*``), single values, and comma-separated lists are supported.
    Ranges (``1-5``) and step values (``*/2``) are **not** supported.

    Examples
    --------
    ``"0 3 * * *"``   – every day at 03:00
    ``"30 2 * * 0"``  – every Sunday at 02:30
    """

    def __init__(self, cron_expr: str, branch: str = "") -> None:
        self._cron = _parse_cron(cron_expr)
        self._branch = branch
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ota-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("ota: scheduler started  cron=%r", " ".join(str(f) for f in self._cron))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("ota: scheduler stopped.")

    def _loop(self) -> None:
        """Wake every 30 seconds; fire update when cron field matches current time."""
        while not self._stop.is_set():
            now = datetime.now()
            if _cron_matches(self._cron, now):
                logger.info("ota: scheduled update triggered.")
                try:
                    result = run_update(self._branch)
                    logger.info("ota: scheduled update result: %s", result.message)
                except OTAAlreadyRunning:
                    logger.info("ota: scheduler skipped – update already running.")
                except Exception as exc:
                    logger.error("ota: scheduled update error: %s", exc)
                # Sleep past this minute so we don't fire twice in the same minute.
                time.sleep(60)
            else:
                self._stop.wait(30)


# ── Cron helpers ───────────────────────────────────────────────────────────────

def _parse_cron(expr: str) -> list:
    """Parse a 5-field cron expression into a list of sets of allowed values."""
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Expected 5 cron fields, got {len(fields)}: {expr!r}")

    ranges = [
        range(0, 60),  # minute
        range(0, 24),  # hour
        range(1, 32),  # day-of-month
        range(1, 13),  # month
        range(0, 7),   # day-of-week (0=Sunday)
    ]
    result = []
    for field, allowed in zip(fields, ranges):
        if field == "*":
            result.append(set(allowed))
        else:
            values = set()
            for part in field.split(","):
                val = int(part)
                if val not in allowed:
                    raise ValueError(f"Cron value {val} out of range {allowed} for field {field!r}")
                values.add(val)
            result.append(values)
    return result


def _cron_matches(cron: list, dt: datetime) -> bool:
    minute, hour, dom, month, dow = cron
    return (
        dt.minute    in minute and
        dt.hour      in hour   and
        dt.day       in dom    and
        dt.month     in month  and
        dt.weekday() in dow    # Mon=0…Sun=6; cron Sun=0 handled below
    ) or (
        # cron day-of-week 0 = Sunday; Python weekday 6 = Sunday
        dt.minute in minute and
        dt.hour   in hour   and
        dt.day    in dom    and
        dt.month  in month  and
        (dt.weekday() + 1) % 7 in dow
    )
