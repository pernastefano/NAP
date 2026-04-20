"""
test_audio_controller.py – Unit tests for AudioController.

All tests run without a real systemd: _isolate() and _verify_active() are
patched at the module level so no subprocess is ever spawned.
A temporary file is used for the ALSA lock so root is not required.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, call

from backend.app.audio_controller import (
    AudioController,
    AudioSource,
    RollbackFailed,
    SwitchFailed,
    SwitchTimeout,
    _TARGETS,
    _IDLE_TARGET,
)
from backend.app.utils.audio_lock import AudioLockTimeout

# ── helpers ────────────────────────────────────────────────────────────────────

_MODULE = "backend.app.audio_controller"


class _LockFileMixin(unittest.TestCase):
    """Creates a per-test temp lock file so tests never need /var/run/."""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".lock")
        self._tmp.close()
        self._lock_file = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._lock_file.unlink(missing_ok=True)

    def _make_controller(self, **kw) -> AudioController:
        kw.setdefault("lock_timeout", 1.0)
        kw.setdefault("systemd_verify_timeout", 1.0)
        kw["lock_file"] = self._lock_file
        return AudioController(**kw)


def _make_controller(**kw) -> AudioController:
    """For tests that patch AudioLock.acquire directly (no real file needed)."""
    kw.setdefault("lock_timeout", 1.0)
    kw.setdefault("systemd_verify_timeout", 1.0)
    return AudioController(**kw)


# ── tests ──────────────────────────────────────────────────────────────────────


class TestInitialState(_LockFileMixin):
    def test_starts_idle(self):
        c = self._make_controller()
        self.assertEqual(c.current_source, AudioSource.IDLE)

    def test_status_initial(self):
        c = self._make_controller()
        s = c.status()
        self.assertEqual(s.source, AudioSource.IDLE)
        self.assertEqual(s.systemd_target, _IDLE_TARGET)
        self.assertIsNone(s.switched_at)
        self.assertEqual(s.switch_count, 0)


class TestNoop(_LockFileMixin):
    """Switching to the already-active source must be a no-op."""

    def test_noop_when_already_idle(self):
        c = self._make_controller()
        with patch(f"{_MODULE}._isolate") as mock_iso:
            c.switch(AudioSource.IDLE)
        mock_iso.assert_not_called()

    def test_noop_when_already_active(self):
        c = self._make_controller()
        with patch(f"{_MODULE}._isolate"), patch(f"{_MODULE}._verify_active"):
            c.switch(AudioSource.MPD)

        with patch(f"{_MODULE}._isolate") as mock_iso:
            c.switch(AudioSource.MPD)
        mock_iso.assert_not_called()


class TestHappyPath(_LockFileMixin):
    def test_switch_to_mpd(self):
        c = self._make_controller()
        with patch(f"{_MODULE}._isolate") as mock_iso, \
             patch(f"{_MODULE}._verify_active"):
            info = c.switch(AudioSource.MPD)

        mock_iso.assert_called_once_with(_TARGETS[AudioSource.MPD])
        self.assertEqual(info.source, AudioSource.MPD)
        self.assertEqual(info.switch_count, 1)
        self.assertIsNotNone(info.switched_at)

    def test_switch_all_sources(self):
        c = self._make_controller()
        with patch(f"{_MODULE}._isolate"), patch(f"{_MODULE}._verify_active"):
            for src in (AudioSource.MPD, AudioSource.AIRPLAY,
                        AudioSource.PLEXAMP, AudioSource.BLUETOOTH):
                info = c.switch(src)
                self.assertEqual(info.source, src)

        self.assertEqual(c.status().switch_count, 4)

    def test_switch_back_to_idle(self):
        c = self._make_controller()
        with patch(f"{_MODULE}._isolate"), patch(f"{_MODULE}._verify_active"):
            c.switch(AudioSource.MPD)
            info = c.switch(AudioSource.IDLE)

        self.assertEqual(info.source, AudioSource.IDLE)
        self.assertEqual(info.systemd_target, _IDLE_TARGET)

    def test_correct_targets_used(self):
        c = self._make_controller()
        with patch(f"{_MODULE}._isolate") as mock_iso, \
             patch(f"{_MODULE}._verify_active"):
            for src, expected_target in _TARGETS.items():
                c._current = AudioSource.IDLE  # reset without lock for speed
                mock_iso.reset_mock()
                c.switch(src)
                mock_iso.assert_called_once_with(expected_target)


class TestRollbackOnSwitchFailed(_LockFileMixin):
    """If isolate fails, the controller must roll back to previous source."""

    def test_rollback_invoked_on_switch_failed(self):
        c = self._make_controller()
        # Pre-condition: system is on MPD
        with patch(f"{_MODULE}._isolate"), patch(f"{_MODULE}._verify_active"):
            c.switch(AudioSource.MPD)

        call_log: list[str] = []

        def fake_isolate(target: str) -> None:
            call_log.append(target)
            if target == _TARGETS[AudioSource.AIRPLAY]:
                raise SwitchFailed("unit test: isolate failed")
            # rollback call passes through

        with patch(f"{_MODULE}._isolate", side_effect=fake_isolate), \
             patch(f"{_MODULE}._verify_active"):
            with self.assertRaises(SwitchFailed):
                c.switch(AudioSource.AIRPLAY)

        # First call: forward switch to airplay (fails)
        # Second call: rollback to mpd (succeeds)
        self.assertEqual(call_log, [
            _TARGETS[AudioSource.AIRPLAY],
            _TARGETS[AudioSource.MPD],
        ])
        self.assertEqual(c.current_source, AudioSource.MPD)

    def test_rollback_invoked_on_verify_timeout(self):
        c = self._make_controller()
        with patch(f"{_MODULE}._isolate"), patch(f"{_MODULE}._verify_active"):
            c.switch(AudioSource.MPD)

        verify_calls: list[str] = []

        def fake_verify(target: str, timeout: float) -> None:
            verify_calls.append(target)
            if target == _TARGETS[AudioSource.PLEXAMP]:
                raise SwitchTimeout("unit test: verify timed out")

        with patch(f"{_MODULE}._isolate"), \
             patch(f"{_MODULE}._verify_active", side_effect=fake_verify):
            with self.assertRaises(SwitchTimeout):
                c.switch(AudioSource.PLEXAMP)

        # Verify was called for plexamp (fails), then for mpd (rollback succeeds)
        self.assertIn(_TARGETS[AudioSource.PLEXAMP], verify_calls)
        self.assertIn(_TARGETS[AudioSource.MPD], verify_calls)
        self.assertEqual(c.current_source, AudioSource.MPD)


class TestRollbackFailed(_LockFileMixin):
    """If both the forward switch and the rollback fail, raise RollbackFailed."""

    def test_raises_rollback_failed(self):
        c = self._make_controller()
        with patch(f"{_MODULE}._isolate"), patch(f"{_MODULE}._verify_active"):
            c.switch(AudioSource.MPD)

        def always_fail(target: str) -> None:
            raise SwitchFailed(f"unit test: always fails ({target})")

        with patch(f"{_MODULE}._isolate", side_effect=always_fail), \
             patch(f"{_MODULE}._verify_active"):
            with self.assertRaises(RollbackFailed) as ctx:
                c.switch(AudioSource.AIRPLAY)

        self.assertIsInstance(ctx.exception.original_error, SwitchFailed)
        self.assertIsInstance(ctx.exception.rollback_error, SwitchFailed)
        # State must NOT be updated because neither forward nor rollback worked.
        self.assertEqual(c.current_source, AudioSource.MPD)


class TestLockTimeout(unittest.TestCase):
    """A concurrent switch in progress must be rejected cleanly."""

    def test_lock_timeout_propagated(self):
        c = _make_controller(lock_timeout=0.1)

        # Simulate the lock being held by patching AudioLock.acquire to raise.
        with patch(
            "backend.app.utils.audio_lock.AudioLock.acquire",
            side_effect=AudioLockTimeout("held by mpd"),
        ):
            with self.assertRaises(AudioLockTimeout):
                c.switch(AudioSource.AIRPLAY)

        # State must be unchanged.
        self.assertEqual(c.current_source, AudioSource.IDLE)


class TestStatusSnapshot(_LockFileMixin):
    def test_switch_count_increments(self):
        c = self._make_controller()
        with patch(f"{_MODULE}._isolate"), patch(f"{_MODULE}._verify_active"):
            for src in (AudioSource.MPD, AudioSource.IDLE,
                        AudioSource.BLUETOOTH, AudioSource.IDLE):
                c.switch(src)

        self.assertEqual(c.status().switch_count, 4)

    def test_switched_at_monotonically_increases(self):
        c = self._make_controller()
        times: list[float] = []
        with patch(f"{_MODULE}._isolate"), patch(f"{_MODULE}._verify_active"):
            for src in (AudioSource.MPD, AudioSource.IDLE, AudioSource.AIRPLAY):
                info = c.switch(src)
                times.append(info.switched_at)  # type: ignore[arg-type]

        self.assertEqual(times, sorted(times))


if __name__ == "__main__":
    unittest.main()
