# NAP – Network Audio Player

[![Build](https://github.com/pernastefano/NAP/actions/workflows/ci.yml/badge.svg)](https://github.com/pernastefano/NAP/actions)
[![Version](https://img.shields.io/github/v/release/pernastefano/NAP)](https://github.com/pernastefano/NAP/releases)
[![License](https://img.shields.io/github/license/pernastefano/NAP)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi%204-red)](https://www.raspberrypi.com/)

A production-grade, headless network audio player built on Raspberry Pi 4. NAP provides stable, deterministic multi-source audio switching between MPD, AirPlay, Plexamp, and Bluetooth — controlled via a REST API, WebSocket-powered Web UI, physical buttons, rotary encoder, and IR remote.

---

## Why NAP?

Solutions like Volumio and Moode Audio are excellent general-purpose players, but they manage audio services in-process, which means source conflicts, unpredictable ALSA state, and hard-to-debug audio dropout on switching.

NAP takes a fundamentally different approach:

> **systemd is the service orchestrator. Python is the control layer. ALSA is never shared.**

| | Volumio / Moode | NAP |
|---|---|---|
| Service switching | In-process start/stop | `systemctl isolate` (kernel-level) |
| ALSA access | Shared / dmix | Exclusive lock per source |
| State machine | Implicit | Explicit `IDLE → SOURCE` with rollback |
| Source conflict prevention | Best-effort | Guaranteed by `Conflicts=` in unit files |
| OTA updates | Via image | `git pull` + rollback |

---

## Key Features

- **Four audio sources** — MPD (FLAC/web radio), AirPlay (shairport-sync), Plexamp Headless, Bluetooth A2DP Sink
- **Deterministic switching** — `systemctl isolate` transitions with a two-phase commit and automatic rollback on failure
- **Global ALSA lock** — `flock(2)` on `/var/run/audio.lock` prevents any two sources from touching the DAC simultaneously
- **FastAPI backend** — REST API + live WebSocket events; OpenAPI docs at `/docs`
- **Single-file Web UI** — source selection, playback controls, volume, config, OTA trigger, live log panel; zero JS framework dependencies
- **16×2 LCD UI** — I2C HD44780 display with double-buffer anti-flicker rendering and rotary encoder navigation
- **Hardware controls** — rotary encoder, dedicated power button (short press = toggle, long press = shutdown), action buttons, IR remote (LIRC/evdev)
- **OTA updates** — `git pull`-based update with dependency refresh, import verification, and automatic rollback on failure; triggered manually (API or Web UI) or on a configurable cron schedule
- **Structured logging** — JSON log entries, in-memory ring buffer, queryable via API

---

## Architecture

### Separation of Concerns

```
┌─────────────────────────────────────────────────────────┐
│  Web UI / LCD UI / Hardware Input                        │  presentation
├─────────────────────────────────────────────────────────┤
│  FastAPI  (REST + WebSocket)                             │  API layer
├─────────────────────────────────────────────────────────┤
│  AudioController  (state machine)                        │  control layer
│  StateManager     (event bus)                            │
│  ConfigManager    (JSON + env vars)                      │
├─────────────────────────────────────────────────────────┤
│  audio_lock.py  (flock on /var/run/audio.lock)           │  safety layer
├─────────────────────────────────────────────────────────┤
│  systemd targets + services  (mpd, shairport-sync, …)   │  OS layer
└─────────────────────────────────────────────────────────┘
```

### systemd Targets

Each audio source is a dedicated systemd target. Switching is performed with `systemctl isolate`, which atomically stops all conflicting units before starting the requested one.

```
audio-mpd.target        Wants=mpd.service
audio-airplay.target    Wants=shairport-sync.service
audio-plexamp.target    Wants=plexamp.service
audio-bluetooth.target  Wants=bluetooth-audio.service
```

Every target declares `Conflicts=` against the other three, so it is **physically impossible** for two audio services to run simultaneously — even if the Python layer fails.

### Audio Lock

Before any `systemctl isolate` call, `AudioController` acquires an exclusive `flock(2)` lock on `/var/run/audio.lock`. This single serialisation point ensures:

- No two switch requests race at the kernel level
- The lock file records the current holder (readable by monitoring tools)
- Timeout (default 8 s) with a `SwitchTimeout` exception if the lock is not acquired

### State Machine

`AudioController` transitions between five states:

```
IDLE  ←→  MPD
      ←→  AIRPLAY
      ←→  PLEXAMP
      ←→  BLUETOOTH
```

Every transition: **acquire lock → isolate → verify active → commit state**.  
On any failure: **rollback to previous target → release lock → raise exception**.

---

## Repository Structure

```
NAP/
├── backend/
│   ├── app/
│   │   ├── audio_controller.py   # State machine; only module that calls systemctl
│   │   ├── state_manager.py      # Wraps AudioController + WebSocket event bus
│   │   ├── config_manager.py     # Pydantic-Settings: JSON file + NAP_* env vars
│   │   ├── ota_updater.py        # Git-based OTA: fetch, pull, verify, rollback
│   │   ├── lcd_ui.py             # I2C 16×2 LCD double-buffer renderer
│   │   ├── hardware_input.py     # GPIO encoder, buttons, IR receiver (evdev)
│   │   ├── main.py               # FastAPI app factory + lifespan
│   │   ├── api/
│   │   │   ├── routes.py         # REST endpoints (/health, /source, /playback, …)
│   │   │   ├── websocket.py      # /ws WebSocket with keepalive
│   │   │   ├── ota.py            # OTA endpoints (/ota/update, /ota/version, …)
│   │   │   ├── playback.py       # mpc / amixer dispatch per source
│   │   │   └── schemas.py        # Pydantic request/response models
│   │   └── utils/
│   │       ├── audio_lock.py     # flock(2) context manager
│   │       └── logger.py         # JSON formatter + in-memory ring buffer
│   └── requirements.txt
├── systemd/
│   ├── audio-mpd.target          # systemd audio source targets (AllowIsolate=yes)
│   ├── audio-airplay.target
│   ├── audio-plexamp.target
│   ├── audio-bluetooth.target
│   ├── mpd.service
│   ├── shairport-sync.service
│   ├── plexamp.service
│   └── bluetooth-audio.service
├── config/
│   ├── asound.conf               # System-wide ALSA: buffer geometry, named PCMs
│   ├── mpd.conf                  # MPD: soxr resampler, ALSA output, buffer tuning
│   ├── shairport-sync.conf       # AirPlay receiver: soxr clock-lock, ALSA output
│   ├── 90-nap-defaults.conf      # Kernel-level ALSA defaults (alsa.conf.d)
│   ├── wiring.conf               # GPIO pin assignments (INI reference)
│   └── wiring_diagram.txt        # ASCII hardware wiring diagram
├── scripts/
│   └── install.sh                # Production installer (idempotent, 14 steps)
├── tests/
│   └── test_audio_controller.py  # 14 unit tests (no root, no hardware required)
├── web/
│   └── index.html                # Single-file Web UI (HTML + CSS + JS)
├── docs/
│   ├── INSTALL.md                # Complete software installation guide
│   └── HARDWARE.md               # Hardware assembly, GPIO wiring, testing
├── LICENSE
└── SPEC.md                       # Product specification
```

---

## Hardware Requirements

| Component | Details |
|---|---|
| Raspberry Pi 4 Model B | Any RAM variant; Raspberry Pi OS Bookworm recommended |
| DAC | USB DAC **or** I2S HAT (e.g. HiFiBerry DAC+, Allo Boss). The onboard 3.5 mm jack is not recommended for quality audio. |
| 16×2 LCD | HD44780 with PCF8574 I2C backpack (address 0x27 or 0x3F) |
| Rotary encoder | KY-040, Alps EC11, or any 2-bit Gray-code encoder with push switch |
| Push buttons | 3–4 momentary NO buttons (power, play/pause, next, previous) |
| IR receiver | TSOP4838, VS1838B, or equivalent 38 kHz demodulator |
| Passive components | 10 kΩ + 100 nF (encoder RC filter), 1 kΩ (IR protection), 4.7 kΩ (I2C pull-ups, usually on LCD backpack) |
| MicroSD / SSD | 16 GB minimum; Class 10 / A1 or USB SSD for reliability |
| Power supply | Official Raspberry Pi 4 USB-C PSU (5.1 V / 3 A) |

Full assembly instructions, circuit diagrams, and troubleshooting are in **[docs/HARDWARE.md](docs/HARDWARE.md)**. Machine-readable pin assignments are in [config/wiring.conf](config/wiring.conf) and ASCII diagrams in [config/wiring_diagram.txt](config/wiring_diagram.txt).

**GPIO Summary (BCM numbering):**

| BCM | Physical | Function | Pull |
|---|---|---|---|
| 2 | 3 | LCD SDA (I2C) | 4.7 kΩ (on board) |
| 3 | 5 | LCD SCL (I2C) | 4.7 kΩ (on board) |
| 17 | 11 | Encoder A | PUD_UP + RC filter |
| 18 | 12 | Encoder B | PUD_UP + RC filter |
| 27 | 13 | Encoder button | PUD_UP |
| 22 | 15 | Power button | PUD_UP |
| 23 | 16 | Play/Pause button | PUD_UP |
| 24 | 18 | Next button | PUD_UP |
| 25 | 22 | Previous button | PUD_UP |
| 16 | 36 | IR receiver data | kernel (gpio-ir) |

> **Note:** GPIO18 is also PCM_CLK (I2S). If you use an I2S DAC HAT, move `encoder_b` to GPIO20 or GPIO23 and update `PinConfig.encoder_b` in `hardware_input.py`.

---

## Installation

> For a complete step-by-step guide including OS flashing, interface setup, and Plexamp authentication, see **[docs/INSTALL.md](docs/INSTALL.md)**.

### Prerequisites

- Raspberry Pi 4 running Raspberry Pi OS **Bookworm** (64-bit recommended)
- Internet connection for package downloads
- SSH access or keyboard/monitor

### Quick Start

```bash
git clone https://github.com/pernastefano/NAP.git
cd NAP
sudo bash scripts/install.sh
```

The installer is fully idempotent — safe to run multiple times. It performs 14 steps:

1. System packages (`apt-get`: mpd, shairport-sync, bluealsa, avahi, Python 3, I2C tools, …)
2. Service accounts (`nap`, `mpd`, `shairport-sync`, `plexamp`)
3. Directory structure (`/opt/nap`, `/etc/nap`, `/var/log/nap`, `/var/lib/nap`)
4. Application code sync to `/opt/nap`
5. Python virtual environment at `/opt/nap/venv` with all dependencies
5b. Plexamp Headless — Node.js (LTS) + binary download from `plexamp.plex.tv`
6. Default configuration at `/etc/nap/config.json` (never overwrites existing)
7. systemd unit installation (4 targets + 4 audio services + `nap-backend.service`)
8. Minimal `sudoers` rule (only `systemctl isolate` + `systemctl restart nap-backend`)
9. udev rules (I2C, GPIO, IR device symlink)
10. `/var/run/audio.lock` creation and permissions
11. Log rotation (`/etc/logrotate.d/nap`)
12. ALSA configuration (`/etc/asound.conf`, `/etc/mpd.conf`, `/etc/shairport-sync.conf`)
13. Enable and start `nap-backend.service`
14. I2C / SPI interface enablement via `raspi-config`

### Installer Flags

```bash
sudo bash scripts/install.sh --no-apt       # Skip apt (packages already installed)
sudo bash scripts/install.sh --no-services  # Install files only; do not start services
sudo bash scripts/install.sh --dev          # Skip RPi-specific hardware packages
```

### Manual Dependency Install

```bash
cd /opt/nap
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
pip install RPLCD smbus2 RPi.GPIO evdev
```

---

## Configuration

The configuration file lives at `/etc/nap/config.json`. All fields are also overridable via environment variables prefixed `NAP_` (e.g. `NAP_API_PORT=9000`).

```json
{
  "default_source": "idle",
  "lock_timeout": 8.0,
  "systemd_verify_timeout": 10.0,
  "lcd_enabled": true,
  "lcd_backlight_timeout": 30,
  "ota_enabled": true,
  "ota_github_repo": "pernastefano/NAP",
  "ota_schedule_cron": "0 3 * * *",
  "api_host": "0.0.0.0",
  "api_port": 8000,
  "log_level": "INFO",
  "log_max_lines": 500
}
```

Changes take effect after a service restart (`sudo systemctl restart nap-backend`) or via the `PATCH /api/v1/config` endpoint.

---

## Accessing the Web UI

1. Find your Pi's IP address:
   ```bash
   hostname -I
   # or, from another machine:
   ping raspberrypi.local
   ```

2. Open in your browser:
   ```
   http://<pi-ip-address>:8000/
   ```

The Web UI provides:
- **Source grid** — one-click switching between MPD, AirPlay, Plexamp, Bluetooth, Idle
- **Playback controls** — play, pause, stop, next, previous, volume slider
- **Config panel** — edit all settings live
- **OTA panel** — trigger an update and watch the progress
- **Log viewer** — filterable live log panel

The interactive API documentation is available at `http://<pi-ip-address>:8000/docs`.

---

## REST API

All endpoints are prefixed `/api/v1`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Service health and current source |
| `GET` | `/source` | Current audio source |
| `POST` | `/source` | Switch source (`{"source": "mpd"}`) |
| `POST` | `/playback` | Playback action (`play`, `pause`, `stop`, `next`, `previous`, `set_volume`) |
| `GET` | `/config` | Current configuration |
| `PATCH` | `/config` | Update configuration fields |
| `GET` | `/logs` | Recent log entries (filterable by level) |
| `POST` | `/ota/update` | Trigger OTA update |
| `GET` | `/ota/version` | Current application version |
| `GET` | `/ota/history` | Last 50 OTA update records |
| `WS` | `/ws` | WebSocket: live `state_changed` and `ping` events |

---

## OTA Updates

NAP updates itself directly from this repository.

### Manual Update

**Via Web UI:** Click the **Update** button in the OTA panel.

**Via API:**
```bash
curl -X POST http://<pi-ip>:8000/api/v1/ota/update
```

**Via command line** (on the Pi):
```bash
sudo -u nap bash -c 'cd /opt/nap && git pull && \
  venv/bin/pip install -q -r backend/requirements.txt && \
  systemctl restart nap-backend'
```

### Automatic Updates

Set `ota_schedule_cron` in `/etc/nap/config.json` to a standard 5-field cron expression:

```json
"ota_schedule_cron": "0 3 * * *"
```

This schedules a nightly update at 03:00. Disable automatic updates by setting `"ota_enabled": false`.

### Update Process

1. `git fetch origin` — check for new commits without touching the working tree
2. Compare HEAD to `origin/<branch>` — if identical, stop (nothing to do)
3. Stash any local uncommitted changes
4. `git pull --ff-only origin <branch>` — fast-forward only; force-pushes are rejected
5. `pip install -r backend/requirements.txt` — refresh dependencies
6. Spawn a fresh Python process to verify the application imports cleanly
7. Write a `VERSION` file with the new commit SHA
8. `systemctl restart nap-backend` — 1.5 s delayed so the API response is sent first

**Rollback:** If any step from 4 onwards fails, NAP automatically runs `git reset --hard <previous-commit>` and re-installs the previous dependencies. The update history (last 50 entries) is recorded in `ota_history.json`.

---

## Running Tests

No hardware or root access required.

```bash
cd /opt/nap   # or your development clone
python3 -m pytest tests/ -v
```

```
tests/test_audio_controller.py::test_initial_state PASSED
tests/test_audio_controller.py::test_noop_switch PASSED
tests/test_audio_controller.py::test_switch_to_mpd PASSED
... 14 passed in 0.04s
```

The test suite patches `_isolate` and `_verify_active` so systemd is never called. Lock files use a per-test `tempfile` path, so no write access to `/var/run` is needed.

---

## Screenshots

| Web UI | LCD UI |
|---|---|
| ![Web UI](docs/screenshots/webui.png) | ![LCD UI](docs/screenshots/lcd.png) |

---

## Roadmap

- [ ] AirPlay 2 support (shairport-sync 4.x)
- [ ] Spotify Connect source (librespot)
- [ ] Multi-room synchronisation (snapcast)
- [ ] Home Assistant MQTT integration
- [ ] Touchscreen UI (Waveshare 3.5")
- [ ] Per-source volume memory
- [ ] Playlist management via Web UI

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Documentation

| Guide | Description |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Complete software installation guide (OS flash → first boot → verify) |
| [docs/HARDWARE.md](docs/HARDWARE.md) | Hardware assembly, GPIO wiring table, RC filters, testing scripts, troubleshooting |
| [config/wiring.conf](config/wiring.conf) | Machine-readable GPIO pin assignments (INI format) |
| [config/wiring_diagram.txt](config/wiring_diagram.txt) | ASCII circuit diagrams for all subsystems |
| [config/asound.conf](config/asound.conf) | Annotated ALSA configuration |
| [SPEC.md](SPEC.md) | Product specification |

---

## Acknowledgements

- [MPD](https://www.musicpd.org/) — Music Player Daemon
- [shairport-sync](https://github.com/mikebrady/shairport-sync) — AirPlay audio receiver
- [bluez-alsa](https://github.com/arkq/bluez-alsa) — Bluetooth A2DP ALSA integration
- [FastAPI](https://fastapi.tiangolo.com/) — Python web framework
- [RPLCD](https://github.com/dbrgn/RPLCD) — Raspberry Pi LCD library
