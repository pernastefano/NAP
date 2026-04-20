"""
lcd_ui.py – I2C 16×2 LCD UI for NAP.

Design principles
-----------------
1. **Double-buffer, cell-differential writes** – The only way a character
   ever reaches the hardware is when it differs from what is already on
   screen.  `lcd.clear()` is never called after startup, so the display
   never flashes blank.

2. **State caching** – Rendering a page is separated from flushing it.
   `_render()` produces a `Frame`; `_flush()` compares it to the committed
   frame and issues per-cell writes only for changed positions.

3. **No blocking I/O on the update path** – `LcdUI.update()` queues a
   render request; a background thread drains the queue and owns all
   hardware writes.  The encoder ISR therefore never blocks.

4. **Hardware abstraction** – `_open_lcd()` returns an RPLCD `CharLCD`
   when the hardware is present and a `_MockLCD` otherwise.  All higher-
   level code is hardware-agnostic.

Menu structure (16×2 display)
------------------------------
ROOT (selector):
  ► 0  Source        → SourcePage   (show current + encoder selects next)
    1  Now Playing   → NowPlayingPage
    2  Settings      → SettingsPage  (backlight timeout, default source)
    3  System Info   → SysInfoPage

Row 0: page title / label
Row 1: current value / scrolling text

Encoder:
  rotate  → navigate list / change value
  press   → enter submenu / confirm
  long    → go back / cancel
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

COLS = 16
ROWS = 2
_SCROLL_INTERVAL = 0.4   # seconds between scroll steps for long text
_RENDER_INTERVAL = 0.1   # background thread tick rate (100 ms)
_BACKLIGHT_DEFAULT = 30  # seconds; 0 = always on


# ──────────────────────────────────────────────────────────────────────────────
# Frame (double-buffer primitive)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Frame:
    """Two rows of exactly COLS characters each."""
    rows: list[str] = field(default_factory=lambda: [" " * COLS, " " * COLS])

    def __post_init__(self) -> None:
        self.rows = [_pad(r) for r in self.rows]

    def diff(self, other: "Frame") -> list[tuple[int, int, str]]:
        """Return (row, col, char) tuples where self differs from *other*."""
        changes: list[tuple[int, int, str]] = []
        for r in range(ROWS):
            for c in range(COLS):
                if self.rows[r][c] != other.rows[r][c]:
                    changes.append((r, c, self.rows[r][c]))
        return changes

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Frame):
            return NotImplemented
        return self.rows == other.rows


def _pad(s: str) -> str:
    """Truncate or right-pad *s* to exactly COLS characters."""
    return s[:COLS].ljust(COLS)


# ──────────────────────────────────────────────────────────────────────────────
# LCD hardware abstraction
# ──────────────────────────────────────────────────────────────────────────────

class _MockLCD:
    """Drop-in replacement used when I2C hardware is absent (dev / CI)."""

    def __init__(self) -> None:
        self.backlight_enabled = True
        self._screen: list[list[str]] = [[" "] * COLS for _ in range(ROWS)]
        logger.warning("lcd_ui: I2C LCD not available – using MockLCD.")

    def cursor_pos(self, row: int, col: int) -> None:
        pass

    def write_string(self, text: str) -> None:
        pass

    def close(self, clear: bool = True) -> None:
        pass


def _open_lcd(i2c_address: int, i2c_port: int, expander: str) -> Any:
    """Return a real CharLCD or a MockLCD if hardware init fails."""
    try:
        from RPLCD.i2c import CharLCD  # type: ignore[import]
        lcd = CharLCD(
            i2c_expander=expander,
            address=i2c_address,
            port=i2c_port,
            cols=COLS,
            rows=ROWS,
            dotsize=8,
            auto_linebreaks=False,
        )
        lcd.clear()
        logger.info(
            "lcd_ui: CharLCD ready  address=0x%02x  port=%d", i2c_address, i2c_port
        )
        return lcd
    except Exception as exc:  # noqa: BLE001
        logger.warning("lcd_ui: cannot open CharLCD (%s) – falling back to MockLCD.", exc)
        return _MockLCD()


# ──────────────────────────────────────────────────────────────────────────────
# UI state passed into LcdUI
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class UIState:
    """Snapshot of the system state relevant to the LCD."""
    source: str = "idle"
    now_playing: str = ""
    uptime_seconds: int = 0
    cpu_temp_c: float = 0.0
    backlight_timeout: int = _BACKLIGHT_DEFAULT
    default_source: str = "idle"


# ──────────────────────────────────────────────────────────────────────────────
# Menu pages
# ──────────────────────────────────────────────────────────────────────────────

class PageID(Enum):
    ROOT = auto()
    SOURCE = auto()
    NOW_PLAYING = auto()
    SETTINGS = auto()
    SYS_INFO = auto()


_SOURCES = ["idle", "mpd", "airplay", "plexamp", "bluetooth"]
_SETTINGS_ITEMS = ["Default src", "Backlight"]

# Cursor character (custom or ASCII fallback)
_CURSOR = "\x7e"  # → (right arrow, present on most HD44780 ROMs)


@dataclass
class _MenuCtx:
    """Mutable navigation context, owned by LcdUI."""
    page: PageID = PageID.ROOT
    root_idx: int = 0
    source_idx: int = 0
    settings_idx: int = 0
    settings_editing: bool = False
    scroll_offset: int = 0
    scroll_text: str = ""
    last_scroll_tick: float = field(default_factory=time.monotonic)


# Page label list for the root menu
_ROOT_ITEMS = ["Source", "Now Playing", "Settings", "System Info"]
_ROOT_PAGE_MAP = {
    0: PageID.SOURCE,
    1: PageID.NOW_PLAYING,
    2: PageID.SETTINGS,
    3: PageID.SYS_INFO,
}


def _render_root(ctx: _MenuCtx, state: UIState) -> Frame:
    idx = ctx.root_idx
    label = _ROOT_ITEMS[idx]
    row0 = _pad(f"{_CURSOR} {label}")
    # Show neighbouring items as context
    prev_label = _ROOT_ITEMS[(idx - 1) % len(_ROOT_ITEMS)]
    next_label = _ROOT_ITEMS[(idx + 1) % len(_ROOT_ITEMS)]
    row1 = _pad(f"  {next_label}")
    return Frame([row0, row1])


def _render_source(ctx: _MenuCtx, state: UIState) -> Frame:
    idx = ctx.source_idx
    selected = _SOURCES[idx]
    marker = "*" if selected == state.source else " "
    row0 = _pad(f"Source: {state.source}")
    row1 = _pad(f"{_CURSOR}{marker}{selected}")
    return Frame([row0, row1])


def _render_now_playing(ctx: _MenuCtx, state: UIState, now: float) -> Frame:
    text = state.now_playing or f"[ {state.source} ]"
    row0 = _pad("Now Playing")

    if len(text) <= COLS:
        row1 = _pad(text)
    else:
        # Smooth scroll: advance one char every _SCROLL_INTERVAL seconds
        if now - ctx.last_scroll_tick >= _SCROLL_INTERVAL:
            ctx.scroll_offset = (ctx.scroll_offset + 1) % (len(text) + 4)
            ctx.last_scroll_tick = now
        padded = text + "    " + text  # seamless wrap
        row1 = _pad(padded[ctx.scroll_offset: ctx.scroll_offset + COLS])

    return Frame([row0, row1])


def _render_settings(ctx: _MenuCtx, state: UIState) -> Frame:
    idx = ctx.settings_idx
    item = _SETTINGS_ITEMS[idx]
    if item == "Default src":
        value = state.default_source
    elif item == "Backlight":
        value = f"{state.backlight_timeout}s" if state.backlight_timeout else "on"
    else:
        value = "?"

    edit_marker = ">" if ctx.settings_editing else " "
    row0 = _pad(f"Settings {edit_marker}")
    row1 = _pad(f"{_CURSOR}{item}: {value}")
    return Frame([row0, row1])


def _render_sysinfo(ctx: _MenuCtx, state: UIState) -> Frame:
    h, rem = divmod(state.uptime_seconds, 3600)
    m, _ = divmod(rem, 60)
    row0 = _pad(f"Up {h:02d}:{m:02d}  {state.source[:6]}")
    row1 = _pad(f"CPU {state.cpu_temp_c:.1f}\xdfC")  # \xdf = degree symbol
    return Frame([row0, row1])


def _render_page(ctx: _MenuCtx, state: UIState, now: float) -> Frame:
    page = ctx.page
    if page == PageID.ROOT:
        return _render_root(ctx, state)
    if page == PageID.SOURCE:
        return _render_source(ctx, state)
    if page == PageID.NOW_PLAYING:
        return _render_now_playing(ctx, state, now)
    if page == PageID.SETTINGS:
        return _render_settings(ctx, state)
    if page == PageID.SYS_INFO:
        return _render_sysinfo(ctx, state)
    return Frame()


# ──────────────────────────────────────────────────────────────────────────────
# LcdUI – public interface
# ──────────────────────────────────────────────────────────────────────────────

class LcdUI:
    """Thread-safe LCD UI with double-buffer rendering.

    Parameters
    ----------
    i2c_address:
        I2C address of the PCF8574 expander (default 0x27 or 0x3F).
    i2c_port:
        Linux I2C bus number (1 on Pi models after model A rev 2).
    expander:
        PCF8574 = single-signal expander (most common).
        MCP23008 / MCP23017 also supported by RPLCD.
    backlight_timeout:
        Seconds of inactivity before backlight is turned off (0 = always on).
    """

    def __init__(
        self,
        i2c_address: int = 0x27,
        i2c_port: int = 1,
        expander: str = "PCF8574",
        backlight_timeout: int = _BACKLIGHT_DEFAULT,
    ) -> None:
        self._lcd = _open_lcd(i2c_address, i2c_port, expander)
        self._backlight_timeout = backlight_timeout
        self._last_activity = time.monotonic()

        # Double buffer: _committed is what the hardware shows; _pending is
        # what the renderer has prepared.
        self._committed = Frame()
        self._state = UIState()
        self._ctx = _MenuCtx()

        # Thread-safe update queue: other threads post UIState snapshots here.
        self._update_q: queue.Queue[UIState] = queue.Queue(maxsize=8)

        # Event for clean shutdown.
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._render_loop, name="lcd-render", daemon=True
        )
        self._thread.start()

    # ── public API ─────────────────────────────────────────────────────────────

    def update(self, state: UIState) -> None:
        """Enqueue a new system-state snapshot.

        Non-blocking: if the queue is full (render thread is busy), the oldest
        entry is dropped and the new one is enqueued at the front.
        """
        try:
            self._update_q.put_nowait(state)
        except queue.Full:
            try:
                self._update_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._update_q.put_nowait(state)
            except queue.Full:
                pass

    def on_encoder_rotate(self, delta: int) -> None:
        """Called by hardware_input when the encoder turns.

        *delta* is +1 (clockwise) or -1 (counter-clockwise).
        Thread-safe: posts an event to the render thread.
        """
        self._post_input(("rotate", delta))

    def on_encoder_press(self, long: bool = False) -> None:
        """Called by hardware_input on encoder button press.

        *long* = True for a long press (≥ 600 ms).
        """
        self._post_input(("press", long))

    def on_activity(self) -> None:
        """Reset the backlight inactivity timer."""
        self._last_activity = time.monotonic()
        if not self._lcd.backlight_enabled:
            self._lcd.backlight_enabled = True

    def close(self) -> None:
        """Stop the render thread and release the LCD."""
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._lcd.close(clear=True)
        except Exception:  # noqa: BLE001
            pass

    # ── input event queue ──────────────────────────────────────────────────────

    _INPUT_Q_MAX = 16

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)

    def _post_input(self, event: tuple) -> None:
        # Lazy-create a separate input queue on first use.
        if not hasattr(self, "_input_q"):
            self._input_q: queue.Queue[tuple] = queue.Queue(maxsize=self._INPUT_Q_MAX)
        try:
            self._input_q.put_nowait(event)
        except queue.Full:
            pass  # Encoder events are best-effort; dropping is safe.

    def _drain_input(self) -> None:
        if not hasattr(self, "_input_q"):
            return
        while True:
            try:
                event = self._input_q.get_nowait()
                self._handle_input(event)
            except queue.Empty:
                break

    def _handle_input(self, event: tuple) -> None:
        self._last_activity = time.monotonic()
        if not self._lcd.backlight_enabled:
            self._lcd.backlight_enabled = True
            return  # First press just wakes the display

        kind = event[0]
        ctx = self._ctx

        if kind == "rotate":
            delta: int = event[1]
            if ctx.page == PageID.ROOT:
                ctx.root_idx = (ctx.root_idx + delta) % len(_ROOT_ITEMS)
            elif ctx.page == PageID.SOURCE:
                ctx.source_idx = (ctx.source_idx + delta) % len(_SOURCES)
            elif ctx.page == PageID.NOW_PLAYING:
                ctx.scroll_offset = max(0, ctx.scroll_offset + delta)
            elif ctx.page == PageID.SETTINGS:
                if ctx.settings_editing:
                    self._adjust_setting(ctx, delta)
                else:
                    ctx.settings_idx = (ctx.settings_idx + delta) % len(_SETTINGS_ITEMS)
            elif ctx.page == PageID.SYS_INFO:
                pass  # read-only page; encoder scrolls nothing

        elif kind == "press":
            long: bool = event[1]
            if long:
                # Long press always goes back / cancels edit
                if ctx.settings_editing:
                    ctx.settings_editing = False
                elif ctx.page != PageID.ROOT:
                    ctx.page = PageID.ROOT
            else:
                # Short press = enter / confirm
                if ctx.page == PageID.ROOT:
                    ctx.page = _ROOT_PAGE_MAP[ctx.root_idx]
                    ctx.scroll_offset = 0
                elif ctx.page == PageID.SOURCE:
                    # Emit a source-switch request via callback if registered
                    chosen = _SOURCES[ctx.source_idx]
                    self._on_source_select(chosen)
                elif ctx.page == PageID.SETTINGS:
                    ctx.settings_editing = not ctx.settings_editing

    def _adjust_setting(self, ctx: _MenuCtx, delta: int) -> None:
        item = _SETTINGS_ITEMS[ctx.settings_idx]
        if item == "Default src":
            # Cycle through sources
            idx = _SOURCES.index(self._state.default_source)
            new_idx = (idx + delta) % len(_SOURCES)
            self._state = _replace_state(self._state, default_source=_SOURCES[new_idx])
            self._on_settings_change({"default_source": _SOURCES[new_idx]})
        elif item == "Backlight":
            options = [0, 10, 30, 60, 120, 300]
            cur = self._state.backlight_timeout
            try:
                idx = options.index(cur)
            except ValueError:
                idx = 2  # default 30 s
            new_idx = (idx + delta) % len(options)
            new_val = options[new_idx]
            self._state = _replace_state(self._state, backlight_timeout=new_val)
            self._on_settings_change({"backlight_timeout": new_val})

    # ── callbacks (override or monkey-patch after construction) ───────────────

    def _on_source_select(self, source: str) -> None:
        """Called when the user confirms a source selection via the encoder.

        Override or assign after construction:
            lcd.on_source_select = lambda s: state_manager.switch(AudioSource(s))
        """
        logger.info("lcd_ui: source selected via menu: %r", source)

    def _on_settings_change(self, changes: dict[str, Any]) -> None:
        """Called when a setting is adjusted via the encoder.

        Override after construction to persist via config_manager.
        """
        logger.info("lcd_ui: settings changed via menu: %s", changes)

    # ── render loop (background thread) ───────────────────────────────────────

    def _render_loop(self) -> None:
        while not self._stop.is_set():
            # 1. Drain state updates from other threads.
            try:
                while True:
                    self._state = self._update_q.get_nowait()
            except queue.Empty:
                pass

            # 2. Drain encoder/button input events.
            self._drain_input()

            # 3. Manage backlight.
            self._tick_backlight()

            # 4. Render current page to back-buffer.
            back = _render_page(self._ctx, self._state, time.monotonic())

            # 5. Flush only changed cells (anti-flicker core).
            self._flush(back)

            time.sleep(_RENDER_INTERVAL)

    def _tick_backlight(self) -> None:
        if self._backlight_timeout <= 0:
            return
        elapsed = time.monotonic() - self._last_activity
        should_be_on = elapsed < self._backlight_timeout
        if hasattr(self._lcd, "backlight_enabled"):
            if self._lcd.backlight_enabled != should_be_on:
                self._lcd.backlight_enabled = should_be_on

    def _flush(self, back: Frame) -> None:
        """Write only the cells that differ from _committed to the hardware."""
        changes = back.diff(self._committed)
        if not changes:
            return

        for row, col, char in changes:
            try:
                self._lcd.cursor_pos = (row, col)
                self._lcd.write_string(char)
            except Exception as exc:  # noqa: BLE001
                logger.error("lcd_ui: write error at (%d,%d): %s", row, col, exc)
                return  # Abort this flush; retry next tick

        self._committed = back


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _replace_state(state: UIState, **kw: Any) -> UIState:
    """Return a new UIState with *kw* fields overwritten."""
    import dataclasses
    return dataclasses.replace(state, **kw)


def read_cpu_temp() -> float:
    """Return CPU temperature in °C (Pi-specific; returns 0.0 on other hosts)."""
    try:
        raw = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, timeout=2,
        ).stdout.decode()
        return float(raw.strip().removeprefix("temp=").removesuffix("'C"))
    except Exception:  # noqa: BLE001
        try:
            return float(
                open("/sys/class/thermal/thermal_zone0/temp").read().strip()
            ) / 1000.0
        except Exception:
            return 0.0


def read_uptime() -> int:
    """Return system uptime in whole seconds."""
    try:
        return int(float(open("/proc/uptime").read().split()[0]))
    except Exception:
        return 0
