"""
hardware_input.py – GPIO input handler for NAP.

Devices managed
---------------
* Rotary encoder  – A/B quadrature + push button
* Action buttons  – arbitrary number of momentary buttons (play/pause, next, …)
* Power button    – short press → toggle audio ON/OFF, long press → shutdown
* IR receiver     – LIRC/evdev virtual key events via /dev/input

All GPIO operations go through RPi.GPIO.  When the library is absent (dev /
CI), a _MockGPIO shim is used so the rest of the code is fully exercisable.

Design
------
* Interrupt-driven: RPi.GPIO edge callbacks fire on the GPIO ISR thread.
  Callbacks are kept minimal (push to queue) to avoid holding the ISR long.
* All debouncing is done in software with time-gated acceptance windows,
  not with RPi.GPIO's built-in bouncetime parameter (which only delays
  registration, not detection).
* A single worker thread drains the event queue and dispatches to registered
  callbacks.  This decouples ISRs from application logic and allows callbacks
  to block without affecting input responsiveness.
* IR events are read from the device as evdev EV_KEY events.  The IR receiver
  itself is handled by the kernel / LIRC; we only read key codes.

Callback wiring (after construction)
-------------------------------------
    hw = HardwareInput(cfg)
    hw.on_encoder_rotate  = lcd.on_encoder_rotate
    hw.on_encoder_press   = lcd.on_encoder_press
    hw.on_button          = my_button_handler      # (button_id: str, long: bool)
    hw.on_power_short     = lambda: state_mgr.switch(AudioSource.IDLE) ...
    hw.on_power_long      = lambda: os.system('shutdown -h now')
    hw.on_ir_key          = my_ir_handler          # (key_code: int)
    hw.start()
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Tunable constants
# ──────────────────────────────────────────────────────────────────────────────

_DEBOUNCE_MS: int = 30          # ms: minimum gap between accepted edges
_LONG_PRESS_MS: int = 600       # ms: hold duration that counts as "long press"
_ENCODER_DEBOUNCE_MS: int = 5   # ms: tighter window for encoder quadrature
_IR_DEVICE: str = "/dev/input/ir-keys"  # symlink set up by install.sh
_WORKER_TIMEOUT: float = 0.05   # seconds: worker thread poll interval


# ──────────────────────────────────────────────────────────────────────────────
# GPIO abstraction
# ──────────────────────────────────────────────────────────────────────────────

class _MockGPIO:
    """Drop-in replacement used when RPi.GPIO is not available."""

    BCM = "BCM"
    IN = "IN"
    PUD_UP = "PUD_UP"
    FALLING = "FALLING"
    RISING = "RISING"
    BOTH = "BOTH"

    def setmode(self, mode: Any) -> None: pass
    def setwarnings(self, flag: bool) -> None: pass
    def setup(self, pin: int, mode: Any, pull_up_down: Any = None) -> None: pass
    def input(self, pin: int) -> int: return 1  # pulled high = not pressed
    def add_event_detect(self, pin: int, edge: Any, callback: Any = None, bouncetime: int = 0) -> None: pass
    def remove_event_detect(self, pin: int) -> None: pass
    def cleanup(self) -> None: pass


def _get_gpio() -> Any:
    try:
        import RPi.GPIO as GPIO  # type: ignore[import]
        return GPIO
    except (ImportError, RuntimeError):
        logger.warning("hardware_input: RPi.GPIO not available – using MockGPIO.")
        return _MockGPIO()


# ──────────────────────────────────────────────────────────────────────────────
# Configuration dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PinConfig:
    """BCM GPIO pin assignments.  Override defaults to match your wiring."""

    # Rotary encoder
    encoder_a: int = 17
    encoder_b: int = 18
    encoder_btn: int = 27

    # Power button (dedicated, separate from encoder)
    power_btn: int = 22

    # Extra action buttons: list of (pin, button_id) tuples
    # e.g. [(24, "play_pause"), (25, "next"), (26, "prev")]
    action_buttons: list[tuple[int, str]] = field(default_factory=list)

    # IR receiver: set to None to disable
    ir_device: Optional[str] = _IR_DEVICE

    # Debounce / timing
    debounce_ms: int = _DEBOUNCE_MS
    long_press_ms: int = _LONG_PRESS_MS
    encoder_debounce_ms: int = _ENCODER_DEBOUNCE_MS


# ──────────────────────────────────────────────────────────────────────────────
# Internal event types (ISR → queue → worker)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _EdgeEvent:
    kind: str      # "encoder_a", "encoder_b", "encoder_btn", "power", "action", "ir"
    pin: int
    level: int     # GPIO level at time of ISR
    ts: float      # time.monotonic()
    meta: Any = None  # e.g. button_id for action buttons


# ──────────────────────────────────────────────────────────────────────────────
# Button tracker (debounce + long-press detection)
# ──────────────────────────────────────────────────────────────────────────────

class _ButtonTracker:
    """Tracks press/release edges for one button and classifies them."""

    def __init__(self, long_press_ms: int, debounce_ms: int) -> None:
        self._long_ms = long_press_ms
        self._debounce_s = debounce_ms / 1000.0
        self._pressed_at: Optional[float] = None
        self._last_edge: float = 0.0

    def on_edge(self, level: int, ts: float) -> Optional[str]:
        """Feed an edge; return "short", "long", or None (still held / debounced)."""
        if ts - self._last_edge < self._debounce_s:
            return None
        self._last_edge = ts

        if level == 0:  # falling = press
            self._pressed_at = ts
            return None
        else:           # rising = release
            if self._pressed_at is None:
                return None
            held_ms = (ts - self._pressed_at) * 1000
            self._pressed_at = None
            if held_ms >= self._long_ms:
                return "long"
            return "short"


# ──────────────────────────────────────────────────────────────────────────────
# Encoder tracker (quadrature decoding with debounce)
# ──────────────────────────────────────────────────────────────────────────────

# Gray-code quadrature state machine.
# State = (A_level, B_level) → index into transition table.
_ENCODER_TABLE: dict[tuple[int, int, int, int], int] = {
    # (prev_A, prev_B, cur_A, cur_B) → delta (+1 CW, -1 CCW, 0 invalid)
    (0, 0, 0, 1): +1,
    (0, 1, 1, 1): +1,
    (1, 1, 1, 0): +1,
    (1, 0, 0, 0): +1,
    (0, 0, 1, 0): -1,
    (1, 0, 1, 1): -1,
    (1, 1, 0, 1): -1,
    (0, 1, 0, 0): -1,
}


class _EncoderTracker:
    def __init__(self, debounce_ms: int) -> None:
        self._debounce_s = debounce_ms / 1000.0
        self._last_a: int = 1
        self._last_b: int = 1
        self._last_emit: float = 0.0

    def on_edge(self, a: int, b: int, ts: float) -> int:
        """Feed current A/B levels; return delta (+1, -1, or 0)."""
        delta = _ENCODER_TABLE.get((self._last_a, self._last_b, a, b), 0)
        if delta and (ts - self._last_emit) >= self._debounce_s:
            self._last_a = a
            self._last_b = b
            self._last_emit = ts
            return delta
        self._last_a = a
        self._last_b = b
        return 0


# ──────────────────────────────────────────────────────────────────────────────
# IR reader (evdev EV_KEY events from /dev/input)
# ──────────────────────────────────────────────────────────────────────────────

# evdev EV_KEY event struct: struct input_event { timeval (8 or 16 bytes),
# __u16 type, __u16 code, __s32 value }
# We read only type=1 (EV_KEY), value=1 (KEY_DOWN).

import struct as _struct

_EV_KEY = 1
_KEY_DOWN = 1

# input_event layout differs by architecture; use the 'standard' 64-bit layout.
_EV_FMT = "llHHi"   # long, long, ushort, ushort, int (24 bytes on 64-bit)
_EV_SIZE = _struct.calcsize(_EV_FMT)

# Common LIRC key codes mapped to action names.
IR_KEY_MAP: dict[int, str] = {
    0x0C: "play_pause",
    0x40: "next",
    0x19: "prev",
    0x15: "vol_up",
    0x07: "vol_down",
    0x45: "mute",
    0x16: "source_mpd",
    0x09: "source_airplay",
    0x1C: "source_plexamp",
    0x42: "source_bluetooth",
}


class _IRReader(threading.Thread):
    """Reads evdev key events from the IR input device in a dedicated thread."""

    def __init__(self, device: str, event_q: "queue.Queue[_EdgeEvent]") -> None:
        super().__init__(name="ir-reader", daemon=True)
        self._device = device
        self._q = event_q
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                fd = os.open(self._device, os.O_RDONLY | os.O_NONBLOCK)
                logger.info("hardware_input: IR device opened: %s", self._device)
                self._read_loop(fd)
            except FileNotFoundError:
                logger.debug("hardware_input: IR device not found: %s  (retrying in 5 s)", self._device)
                time.sleep(5)
            except OSError as exc:
                logger.warning("hardware_input: IR device error: %s  (retrying in 5 s)", exc)
                time.sleep(5)

    def _read_loop(self, fd: int) -> None:
        import select
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue
                raw = os.read(fd, _EV_SIZE)
                if len(raw) < _EV_SIZE:
                    continue
                _, _, ev_type, ev_code, ev_value = _struct.unpack(_EV_FMT, raw)
                if ev_type == _EV_KEY and ev_value == _KEY_DOWN:
                    self._q.put_nowait(
                        _EdgeEvent(kind="ir", pin=-1, level=1,
                                   ts=time.monotonic(), meta=ev_code)
                    )
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()


# ──────────────────────────────────────────────────────────────────────────────
# HardwareInput – public interface
# ──────────────────────────────────────────────────────────────────────────────

class HardwareInput:
    """Event-driven GPIO + IR input handler.

    Parameters
    ----------
    config:
        Pin assignments and timing constants.

    After construction, wire callbacks then call ``start()``:

        hw = HardwareInput()
        hw.on_encoder_rotate  = lcd.on_encoder_rotate      # (delta: int)
        hw.on_encoder_press   = lcd.on_encoder_press       # (long: bool)
        hw.on_button          = handle_button              # (id: str, long: bool)
        hw.on_power_short     = handle_power_short         # ()
        hw.on_power_long      = handle_power_long          # ()
        hw.on_ir_key          = handle_ir                  # (key_name: str, code: int)
        hw.start()
    """

    def __init__(self, config: Optional[PinConfig] = None) -> None:
        self._cfg = config or PinConfig()
        self._gpio = _get_gpio()
        self._q: queue.Queue[_EdgeEvent] = queue.Queue(maxsize=128)
        self._stop = threading.Event()

        # State machines
        self._enc = _EncoderTracker(self._cfg.encoder_debounce_ms)
        self._enc_btn = _ButtonTracker(self._cfg.long_press_ms, self._cfg.debounce_ms)
        self._pwr_btn = _ButtonTracker(self._cfg.long_press_ms, self._cfg.debounce_ms)
        self._action_trackers: dict[int, tuple[str, _ButtonTracker]] = {
            pin: (btn_id, _ButtonTracker(self._cfg.long_press_ms, self._cfg.debounce_ms))
            for pin, btn_id in self._cfg.action_buttons
        }

        # Callbacks – replace after construction
        self.on_encoder_rotate: Callable[[int], None] = lambda delta: None
        self.on_encoder_press:  Callable[[bool], None] = lambda long: None
        self.on_button:         Callable[[str, bool], None] = lambda btn_id, long: None
        self.on_power_short:    Callable[[], None] = lambda: None
        self.on_power_long:     Callable[[], None] = lambda: None
        self.on_ir_key:         Callable[[str, int], None] = lambda key_name, code: None

        self._worker: Optional[threading.Thread] = None
        self._ir: Optional[_IRReader] = None

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Configure GPIO, start the worker and IR threads."""
        self._setup_gpio()

        if self._cfg.ir_device:
            self._ir = _IRReader(self._cfg.ir_device, self._q)
            self._ir.start()

        self._worker = threading.Thread(
            target=self._dispatch_loop, name="hw-input", daemon=True
        )
        self._worker.start()
        logger.info("hardware_input: started.")

    def stop(self) -> None:
        """Stop all threads and clean up GPIO."""
        self._stop.set()
        if self._ir:
            self._ir.stop()
        if self._worker:
            self._worker.join(timeout=2)
        try:
            self._gpio.cleanup()
        except Exception:  # noqa: BLE001
            pass
        logger.info("hardware_input: stopped.")

    # ── GPIO setup ─────────────────────────────────────────────────────────────

    def _setup_gpio(self) -> None:
        gpio = self._gpio
        cfg = self._cfg
        gpio.setmode(gpio.BCM)
        gpio.setwarnings(False)

        # Encoder A/B channels
        for pin in (cfg.encoder_a, cfg.encoder_b):
            gpio.setup(pin, gpio.IN, pull_up_down=gpio.PUD_UP)
            gpio.add_event_detect(pin, gpio.BOTH, callback=self._isr_encoder)

        # Encoder button
        gpio.setup(cfg.encoder_btn, gpio.IN, pull_up_down=gpio.PUD_UP)
        gpio.add_event_detect(cfg.encoder_btn, gpio.BOTH, callback=self._isr_encoder_btn)

        # Power button
        gpio.setup(cfg.power_btn, gpio.IN, pull_up_down=gpio.PUD_UP)
        gpio.add_event_detect(cfg.power_btn, gpio.BOTH, callback=self._isr_power)

        # Action buttons
        for pin, btn_id in cfg.action_buttons:
            gpio.setup(pin, gpio.IN, pull_up_down=gpio.PUD_UP)
            gpio.add_event_detect(
                pin, gpio.BOTH,
                callback=lambda ch, _pin=pin, _id=btn_id: self._isr_action(ch, _pin, _id),
            )

        logger.debug(
            "hardware_input: GPIO configured  enc_a=%d enc_b=%d enc_btn=%d pwr=%d",
            cfg.encoder_a, cfg.encoder_b, cfg.encoder_btn, cfg.power_btn,
        )

    # ── ISR callbacks (RPi.GPIO ISR thread – keep minimal) ────────────────────

    def _isr_encoder(self, channel: int) -> None:
        ts = time.monotonic()
        a = self._gpio.input(self._cfg.encoder_a)
        b = self._gpio.input(self._cfg.encoder_b)
        self._put(_EdgeEvent(kind="encoder_ab", pin=channel, level=0, ts=ts, meta=(a, b)))

    def _isr_encoder_btn(self, channel: int) -> None:
        ts = time.monotonic()
        level = self._gpio.input(channel)
        self._put(_EdgeEvent(kind="encoder_btn", pin=channel, level=level, ts=ts))

    def _isr_power(self, channel: int) -> None:
        ts = time.monotonic()
        level = self._gpio.input(channel)
        self._put(_EdgeEvent(kind="power", pin=channel, level=level, ts=ts))

    def _isr_action(self, channel: int, pin: int, btn_id: str) -> None:
        ts = time.monotonic()
        level = self._gpio.input(pin)
        self._put(_EdgeEvent(kind="action", pin=pin, level=level, ts=ts, meta=btn_id))

    def _put(self, event: _EdgeEvent) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            pass  # Drop oldest — input responsiveness is more important than history.

    # ── Worker thread (event dispatch) ────────────────────────────────────────

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._q.get(timeout=_WORKER_TIMEOUT)
            except queue.Empty:
                continue
            try:
                self._dispatch(event)
            except Exception as exc:  # noqa: BLE001
                logger.error("hardware_input: dispatch error: %s", exc, exc_info=True)

    def _dispatch(self, ev: _EdgeEvent) -> None:
        if ev.kind == "encoder_ab":
            a, b = ev.meta
            delta = self._enc.on_edge(a, b, ev.ts)
            if delta:
                logger.debug("hardware_input: encoder rotate delta=%+d", delta)
                self.on_encoder_rotate(delta)

        elif ev.kind == "encoder_btn":
            result = self._enc_btn.on_edge(ev.level, ev.ts)
            if result:
                long = result == "long"
                logger.debug("hardware_input: encoder press long=%s", long)
                self.on_encoder_press(long)

        elif ev.kind == "power":
            result = self._pwr_btn.on_edge(ev.level, ev.ts)
            if result == "short":
                logger.info("hardware_input: power short press → toggle audio")
                self.on_power_short()
            elif result == "long":
                logger.warning("hardware_input: power long press → shutdown")
                self.on_power_long()

        elif ev.kind == "action":
            btn_id: str = ev.meta
            tracker_entry = self._action_trackers.get(ev.pin)
            if tracker_entry:
                _, tracker = tracker_entry
                result = tracker.on_edge(ev.level, ev.ts)
                if result:
                    long = result == "long"
                    logger.debug("hardware_input: button %r long=%s", btn_id, long)
                    self.on_button(btn_id, long)

        elif ev.kind == "ir":
            code: int = ev.meta
            key_name = IR_KEY_MAP.get(code, f"0x{code:02X}")
            logger.debug("hardware_input: IR key %s (0x%02X)", key_name, code)
            self.on_ir_key(key_name, code)


# ──────────────────────────────────────────────────────────────────────────────
# Power actions
# ──────────────────────────────────────────────────────────────────────────────

def shutdown_system() -> None:
    """Initiate a clean system shutdown.  Runs as a blocking subprocess."""
    logger.warning("hardware_input: initiating system shutdown.")
    try:
        subprocess.run(["/sbin/shutdown", "-h", "now"], check=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("hardware_input: shutdown failed: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Integration helper – wire everything together
# ──────────────────────────────────────────────────────────────────────────────

def wire_hardware(
    hw: HardwareInput,
    lcd: Any,                # LcdUI instance
    state_manager: Any,      # StateManager instance
    audio_sources: list[str],
    current_source_fn: Callable[[], str],
) -> None:
    """Connect HardwareInput callbacks to LcdUI and StateManager.

    This helper keeps main.py free of wiring boilerplate.  All imports are
    local to avoid circular dependencies between modules.

    Parameters
    ----------
    hw:
        The HardwareInput instance (not yet started).
    lcd:
        The LcdUI instance.
    state_manager:
        The StateManager instance.
    audio_sources:
        Ordered list of non-idle sources, e.g. ["mpd", "airplay", "plexamp", "bluetooth"].
    current_source_fn:
        Zero-argument callable that returns the current source name.
    """
    from backend.app.audio_controller import AudioSource

    # ── encoder → LCD ─────────────────────────────────────────────────────────
    hw.on_encoder_rotate = lcd.on_encoder_rotate
    hw.on_encoder_press  = lcd.on_encoder_press

    # ── power button ──────────────────────────────────────────────────────────
    def _toggle_audio() -> None:
        """Short press: if active → IDLE; if IDLE → last/first source."""
        current = current_source_fn()
        if current == "idle":
            first = audio_sources[0] if audio_sources else "mpd"
            try:
                state_manager.switch(AudioSource(first))
            except Exception as exc:
                logger.error("hardware_input: toggle-on failed: %s", exc)
        else:
            try:
                state_manager.switch(AudioSource.IDLE)
            except Exception as exc:
                logger.error("hardware_input: toggle-off failed: %s", exc)

    hw.on_power_short = _toggle_audio
    hw.on_power_long  = shutdown_system

    # ── action buttons ────────────────────────────────────────────────────────
    def _handle_button(btn_id: str, long: bool) -> None:
        lcd.on_activity()
        if btn_id == "play_pause":
            from backend.app.api.playback import dispatch, PlaybackError
            src = current_source_fn()
            try:
                dispatch(source=src, action="pause" if not long else "stop")
            except PlaybackError as exc:
                logger.warning("hardware_input: playback error: %s", exc)
        elif btn_id == "next":
            from backend.app.api.playback import dispatch, PlaybackError
            try:
                dispatch(source=current_source_fn(), action="next")
            except PlaybackError:
                pass
        elif btn_id == "prev":
            from backend.app.api.playback import dispatch, PlaybackError
            try:
                dispatch(source=current_source_fn(), action="previous")
            except PlaybackError:
                pass

    hw.on_button = _handle_button

    # ── IR keys ───────────────────────────────────────────────────────────────
    def _handle_ir(key_name: str, code: int) -> None:
        lcd.on_activity()
        from backend.app.api.playback import dispatch, PlaybackError

        _PLAYBACK_KEYS = {"play_pause", "next", "prev", "vol_up", "vol_down", "mute"}
        _SOURCE_KEY_MAP = {
            "source_mpd":       AudioSource.MPD,
            "source_airplay":   AudioSource.AIRPLAY,
            "source_plexamp":   AudioSource.PLEXAMP,
            "source_bluetooth": AudioSource.BLUETOOTH,
        }

        action_map = {
            "play_pause": "pause",
            "next":       "next",
            "prev":       "previous",
            "vol_up":     "volume_up",
            "vol_down":   "volume_down",
        }

        if key_name in action_map:
            try:
                dispatch(source=current_source_fn(), action=action_map[key_name])
            except PlaybackError as exc:
                logger.debug("hardware_input: IR playback: %s", exc)
        elif key_name in _SOURCE_KEY_MAP:
            try:
                state_manager.switch(_SOURCE_KEY_MAP[key_name])
            except Exception as exc:
                logger.error("hardware_input: IR source switch: %s", exc)
        else:
            logger.debug("hardware_input: unmapped IR key %r (0x%02X)", key_name, code)

    hw.on_ir_key = _handle_ir
