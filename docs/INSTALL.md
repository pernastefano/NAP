# NAP – Software Installation Guide

**Target:** Raspberry Pi OS Lite (Bookworm, 64-bit) on Raspberry Pi 4 Model B  
**Repository:** https://github.com/pernastefano/NAP  
**Install time:** ~10–15 minutes on a fresh image with a good internet connection

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Flash and First Boot](#2-flash-and-first-boot)
3. [System Preparation](#3-system-preparation)
4. [Enable Hardware Interfaces](#4-enable-hardware-interfaces)
5. [Clone the Repository](#5-clone-the-repository)
6. [Run the Installer](#6-run-the-installer)
7. [Plexamp Headless Setup](#7-plexamp-headless-setup)
8. [First Boot Behaviour](#8-first-boot-behaviour)
9. [Access the Web UI](#9-access-the-web-ui)
10. [OTA Update System](#10-ota-update-system)
11. [Verify Installation](#11-verify-installation)
12. [Troubleshooting](#12-troubleshooting)
13. [Logs](#13-logs)

---

## 1. Prerequisites

Before starting, ensure you have:

- Raspberry Pi 4 Model B (any RAM variant)
- MicroSD card (16 GB minimum, Class 10 / A1 or better) **or** USB SSD
- USB DAC or I2S DAC HAT connected
- Wired Ethernet or Wi-Fi credentials ready
- A computer to flash the SD card and open an SSH session

Software on your computer:
- [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (v1.8+)

---

## 2. Flash and First Boot

### 2.1 Write the OS Image

1. Open **Raspberry Pi Imager**
2. **Choose Device** → Raspberry Pi 4
3. **Choose OS** → Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**
4. **Choose Storage** → your SD card
5. Click the **gear icon (⚙)** before writing to pre-configure:
   - Hostname: `nap` (or your preference)
   - Enable SSH → Use password authentication
   - Set username and password
   - Configure Wi-Fi (SSID + password) if not using Ethernet
   - Set locale / timezone
6. Click **Save**, then **Write**

### 2.2 Boot and Connect

Insert the card, power on the Pi, and wait ~60 seconds for first boot.

```bash
# From your computer — find the Pi on your network
ping nap.local

# SSH in
ssh pi@nap.local
# or by IP: ssh pi@192.168.x.x
```

---

## 3. System Preparation

Once connected via SSH, update the system and install `git`:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git
```

> **Why upgrade first?** The installer installs specific versions of audio daemons (shairport-sync, bluealsa). Upgrading the base system first prevents dependency conflicts with newer libc / libssl.

Set the correct timezone if you did not do so via Imager:

```bash
sudo timedatectl set-timezone Europe/Rome   # replace with your timezone
timedatectl status                          # verify
```

---

## 4. Enable Hardware Interfaces

### 4.1 Using raspi-config (interactive)

```bash
sudo raspi-config
```

Navigate to **Interface Options** and enable:

| Interface | Required for |
|---|---|
| **I2C** | HD44780 LCD via PCF8574 backpack |
| **SSH** | Remote access (already enabled if configured via Imager) |

Select **Finish** and **reboot** when prompted:

```bash
sudo reboot
```

### 4.2 Using raspi-config non-interactively (scripted)

```bash
sudo raspi-config nonint do_i2c 0   # 0 = enable
sudo raspi-config nonint do_ssh 0
```

> The installer (`install.sh`) runs both of these commands automatically on step 14. You only need to do this manually if you want to verify the interfaces before running the installer.

### 4.3 Enable the IR Receiver Overlay

Add the following to `/boot/firmware/config.txt` (Bookworm) to enable the kernel IR receiver driver on GPIO16:

```bash
echo "dtoverlay=gpio-ir,gpio_pin=16" | sudo tee -a /boot/firmware/config.txt
```

If your DAC is an I2S HAT (e.g. HiFiBerry DAC+), also add its overlay. For a USB DAC, disable the onboard audio to prevent ALSA card numbering issues:

```bash
# USB DAC only – prevent onboard audio from becoming card 0
echo "dtparam=audio=off" | sudo tee -a /boot/firmware/config.txt

# I2S DAC example (HiFiBerry DAC+)
# echo "dtoverlay=hifiberry-dacplus" | sudo tee -a /boot/firmware/config.txt
```

Reboot to apply:

```bash
sudo reboot
```

---

## 5. Clone the Repository

```bash
git clone https://github.com/pernastefano/NAP.git
cd NAP
```

> The installer uses the clone location to determine the repository root. If you want NAP installed to `/opt/nap` (the default), the installer will sync the code there automatically. You can clone anywhere.

Verify the directory structure looks correct:

```bash
ls
# backend/  config/  scripts/  systemd/  tests/  web/  README.md  SPEC.md
```

---

## 6. Run the Installer

```bash
sudo bash scripts/install.sh
```

The script is **fully idempotent** — running it multiple times is safe and will not overwrite configuration you have already customised.

### What the Installer Does (14 Steps)

#### Step 1 — System Packages

Installs via `apt-get` (only packages not already present):

| Package | Purpose |
|---|---|
| `mpd`, `mpc` | Music Player Daemon + CLI client |
| `shairport-sync` | AirPlay audio receiver |
| `bluez`, `bluez-tools` | Bluetooth stack |
| `bluealsa`, `bluealsa-utils` | Bluetooth A2DP → ALSA bridge |
| `avahi-daemon`, `avahi-utils` | mDNS (required for AirPlay discovery) |
| `python3`, `python3-pip`, `python3-venv`, `python3-dev` | Python runtime |
| `i2c-tools` | I2C bus utilities (`i2cdetect`) |
| `gcc`, `build-essential`, `libffi-dev`, `libssl-dev` | Build tools for Python C extensions |
| `raspi-gpio`, `libraspberrypi-bin` | RPi GPIO headers |
| `curl`, `wget`, `jq` | General utilities |

#### Step 2 — Service Accounts

Creates dedicated system users with no login shell:

| User | Home | Groups |
|---|---|---|
| `nap` | `/opt/nap` | `audio`, `video`, `gpio`, `i2c`, `bluetooth`, `input` |
| `mpd` | `/var/lib/mpd` | `audio` |
| `shairport-sync` | `/var/run/shairport-sync` | `audio` |
| `plexamp` | `/opt/plexamp` | `audio` |

#### Step 3 — Directory Structure

Creates and sets ownership on:

```
/opt/nap          Application root (owned by nap:nap)
/etc/nap          Configuration files (root:nap, mode 750)
/var/log/nap      Log files
/var/lib/nap      Persistent state, OTA history
/var/run/nap      Runtime files (recreated on boot via tmpfiles.d)
```

Also configures `systemd-tmpfiles` to recreate `/var/run/nap` and `/var/run/audio.lock` on every boot.

#### Step 4 — Application Code

If running the installer from a development clone, `rsync`es the repository to `/opt/nap` (excluding `.git`, `__pycache__`, tests). If already running from `/opt/nap`, this step is skipped.

#### Step 5 — Python Virtual Environment

```
/opt/nap/venv/
```

Creates a virtualenv with `--system-site-packages`, then installs:
- `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings` (from `backend/requirements.txt`)
- `RPLCD`, `smbus2`, `RPi.GPIO`, `evdev` (hardware libraries)

#### Step 6 — Default Configuration

Writes `/etc/nap/config.json` **only if it does not already exist**:

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

#### Step 7 — systemd Units

Installs all unit files from `systemd/` to `/etc/systemd/system/`:

- `audio-mpd.target`, `audio-airplay.target`, `audio-plexamp.target`, `audio-bluetooth.target`
- `mpd.service`, `shairport-sync.service`, `plexamp.service`, `bluetooth-audio.service`

Generates `nap-backend.service` pointing to the correct venv interpreter. Runs `systemctl daemon-reload`.

#### Step 8 — sudoers

Writes `/etc/sudoers.d/nap` (validated with `visudo -c`) granting the `nap` user passwordless access to exactly:

```
systemctl isolate audio-{mpd,airplay,plexamp,bluetooth}.target
systemctl isolate multi-user.target
systemctl is-active *
systemctl restart nap-backend.service
```

No broader sudo access is granted.

#### Step 9 — udev Rules

Writes `/etc/udev/rules.d/99-nap.rules`:
- I2C bus 1 → group `i2c`, mode `0660`
- IR receiver input device → symlink `/dev/input/ir-keys`, group `input`
- GPIO subsystem → group `gpio`, mode `0660`

#### Step 10 — Audio Lock File

Creates `/var/run/audio.lock` with owner `nap:audio`, mode `660`. This file is the single serialisation point for all ALSA access.

#### Step 11 — Log Rotation

Writes `/etc/logrotate.d/nap`: daily rotation, 7 days retention, compressed, `HUP` signal on rotate.

#### Step 12 — ALSA Configuration

Installs (never overwrites existing):
- `/etc/asound.conf` — named PCMs (`nap_mpd`, `nap_airplay`, `nap_bluetooth`), buffer geometry, softvol
- `/etc/alsa/alsa.conf.d/90-nap-defaults.conf` — kernel-level ALSA defaults
- `/etc/mpd.conf` — MPD with soxr resampler, 512 KiB audio buffer
- `/etc/shairport-sync.conf` — AirPlay with soxr clock-lock

#### Step 13 — Enable and Start Services

```bash
systemctl enable --now avahi-daemon.service
systemctl enable --now bluetooth.service
systemctl enable audio-{mpd,airplay,plexamp,bluetooth}.target
systemctl enable mpd.service shairport-sync.service plexamp.service bluetooth-audio.service
systemctl enable --now nap-backend.service
```

Audio source services are **enabled but not started** — only one can run at a time. The backend starts them on demand via `systemctl isolate`.

#### Step 14 — I2C / SPI Interfaces

```bash
raspi-config nonint do_i2c 0
raspi-config nonint do_spi 0
```

### Installer Flags

```bash
# Skip apt (already installed or offline environment)
sudo bash scripts/install.sh --no-apt

# Install files only — do not enable or start any services
sudo bash scripts/install.sh --no-services

# Development machine — skip RPi.GPIO and hardware-specific packages
sudo bash scripts/install.sh --dev
```

### Expected Output

A successful run ends with:

```
[OK]  NAP installation complete.

      Config:  /etc/nap/config.json
      Logs:    journalctl -u nap-backend.service -f
      API:     http://<pi-address>:8000
      Web UI:  http://<pi-address>:8000/
```

---

## 7. Plexamp Headless Setup

Plexamp Headless is not available via `apt` and must be installed manually before its systemd service can start.

### 7.1 Download

Visit https://plexamp.com/headless/ and copy the latest tarball URL, then:

```bash
cd /opt/plexamp
sudo -u plexamp wget -O plexamp.tar.bz2 "https://plexamp.plex.tv/headless/Plexamp-Linux-headless-v4.x.x.tar.bz2"
sudo -u plexamp tar -xjf plexamp.tar.bz2 --strip-components=1
sudo -u plexamp rm plexamp.tar.bz2
```

### 7.2 Install Node.js (if not present)

```bash
node --version 2>/dev/null || {
    curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
    sudo apt-get install -y nodejs
}
```

### 7.3 First-run Authentication

```bash
sudo -u plexamp /usr/bin/node /opt/plexamp/js/index.js
```

Follow the on-screen prompt to claim the player with your Plex account. After claiming, stop the process (`Ctrl+C`) and start the systemd service:

```bash
sudo systemctl start plexamp.service
sudo systemctl status plexamp.service
```

---

## 8. First Boot Behaviour

After installation completes:

| Behaviour | Detail |
|---|---|
| **Default source** | `idle` — no audio service runs until explicitly switched |
| **Backend auto-start** | `nap-backend.service` starts automatically on every boot via `WantedBy=multi-user.target` |
| **Audio services** | Enabled but stopped; started on demand by `systemctl isolate` |
| **LCD** | Initialises on backend startup; displays "NAP" and current source on the root page |
| **Lock file** | `/var/run/audio.lock` recreated by `systemd-tmpfiles` on every boot before any service starts |
| **OTA scheduler** | If `ota_enabled=true` and `ota_schedule_cron` is set, the scheduler starts within the backend process and fires at the configured time |

To change the default source that activates on backend startup, edit `/etc/nap/config.json`:

```json
"default_source": "mpd"
```

---

## 9. Access the Web UI

### Find Your Pi's IP Address

On the Pi:

```bash
hostname -I
# Example output: 192.168.1.42 fd00::1
```

From another machine on the same network:

```bash
ping nap.local         # works if mDNS / Bonjour is active
arp -n | grep -i dc:a6  # look for the Pi MAC prefix
```

### Open in Browser

```
http://nap.local:8000/
# or
http://192.168.1.42:8000/
```

The Web UI provides:
- **Source grid** — switch between MPD, AirPlay, Plexamp, Bluetooth, Idle
- **Playback controls** — play, pause, stop, next, previous, volume slider
- **Config panel** — edit all settings; changes persist to `/etc/nap/config.json`
- **OTA panel** — trigger an update and monitor progress
- **Log viewer** — filterable live log panel with level filter

Interactive API documentation (OpenAPI / Swagger):

```
http://nap.local:8000/docs
```

---

## 10. OTA Update System

NAP updates itself directly from https://github.com/pernastefano/NAP.

### Manual Update — Via Web UI

1. Open `http://nap.local:8000/`
2. Navigate to the **OTA** panel
3. Click **Update Now**
4. The progress bar advances through fetch → pull → dependency check → import verify → restart

### Manual Update — Via API

```bash
curl -s -X POST http://nap.local:8000/api/v1/ota/update | jq .
```

Expected response on success:

```json
{
  "ok": true,
  "message": "Updated a1b2c3d4 → e5f6g7h8.",
  "version": "e5f6g7h8",
  "previous_version": "a1b2c3d4",
  "rolled_back": false
}
```

### Manual Update — Via CLI

```bash
# On the Pi (re-runs the full installer against the new code)
cd /opt/nap
sudo git pull origin main
sudo bash scripts/install.sh --no-services
sudo systemctl restart nap-backend.service
```

### Automatic Updates

Automatic updates are configured in `/etc/nap/config.json`:

```json
"ota_enabled": true,
"ota_schedule_cron": "0 3 * * *"
```

The `ota_schedule_cron` field accepts a standard 5-field cron expression:

| Expression | Schedule |
|---|---|
| `"0 3 * * *"` | Every day at 03:00 (default) |
| `"0 2 * * 0"` | Every Sunday at 02:00 |
| `"30 4 * * 1-5"` | Weekdays at 04:30 |
| `"0 0 1 * *"` | First day of each month at midnight |

To disable automatic updates:

```json
"ota_enabled": false
```

Changes take effect after restarting the backend:

```bash
sudo systemctl restart nap-backend.service
```

### Update Process (Step by Step)

```
1. git fetch origin                    — download remote refs (no working tree changes)
2. Compare HEAD vs origin/main         — if identical: stop, report "already up-to-date"
3. git stash (if dirty)                — save any local uncommitted changes
4. git pull --ff-only origin main      — fast-forward only; rejects force-pushes
5. pip install -r requirements.txt     — refresh Python dependencies
6. python3 -c "import backend.app.main"— verify the app still imports cleanly
7. Write VERSION file                  — record new commit SHA
8. systemctl restart nap-backend       — restart after 1.5 s delay (response sent first)
```

### Rollback Strategy

If any step from 4 onwards fails, NAP automatically recovers:

```
git reset --hard <previous_commit>
pip install -r requirements.txt       — restore previous dependencies
```

The `rolled_back: true` flag is set in the API response and in `ota_history.json`.

Check the update history:

```bash
curl -s http://nap.local:8000/api/v1/ota/history | jq '.history[-5:]'
```

Or inspect the file directly:

```bash
cat /opt/nap/ota_history.json | jq '.[-5:]'
```

---

## 11. Verify Installation

### Service Status

```bash
# NAP backend
sudo systemctl status nap-backend.service

# Audio targets (all should be inactive/dead — only one activates at a time)
sudo systemctl status audio-mpd.target
sudo systemctl status audio-airplay.target
sudo systemctl status audio-plexamp.target
sudo systemctl status audio-bluetooth.target
```

### Test Source Switching

Switch to MPD (starts `mpd.service`, stops everything else):

```bash
sudo systemctl isolate audio-mpd.target
sudo systemctl status mpd.service
```

Switch to AirPlay:

```bash
sudo systemctl isolate audio-airplay.target
sudo systemctl status shairport-sync.service

# mpd.service should now be stopped:
sudo systemctl status mpd.service
```

Return to idle:

```bash
sudo systemctl isolate multi-user.target
```

### Test the REST API

```bash
# Health check
curl -s http://localhost:8000/api/v1/health | jq .

# Current source
curl -s http://localhost:8000/api/v1/source | jq .

# Switch to MPD
curl -s -X POST http://localhost:8000/api/v1/source \
  -H "Content-Type: application/json" \
  -d '{"source": "mpd"}' | jq .

# Switch back to idle
curl -s -X POST http://localhost:8000/api/v1/source \
  -H "Content-Type: application/json" \
  -d '{"source": "idle"}' | jq .
```

### Verify ALSA Configuration

```bash
# List recognised sound cards
aplay -l

# Verify named PCMs are registered
aplay -L | grep nap

# Test audio output (plays 3 seconds of 1 kHz sine wave through nap_mpd)
speaker-test -D nap_mpd -t sine -f 1000 -l 1
```

### Verify I2C (LCD)

```bash
sudo i2cdetect -y 1
```

You should see `27` (or `3f`) in the grid indicating the PCF8574 LCD backpack.

### Verify IR Receiver

```bash
ls -la /dev/input/ir-keys     # udev symlink must exist

# Listen for raw IR events (press a button on the remote)
evtest /dev/input/ir-keys
```

---

## 12. Troubleshooting

### ALSA Device Busy

**Symptom:** `aplay: Device or resource busy` or audio fails to start.

**Cause:** Another process holds the ALSA device open, or a previous service did not release it.

```bash
# Find what holds the audio device
fuser /dev/snd/*
lsof /dev/snd

# Check who holds the audio lock
cat /var/run/audio.lock

# Force-release if stuck (only if backend is not running)
sudo fuser -k /dev/snd/*
sudo systemctl restart nap-backend.service
```

### Wrong ALSA Card Number

**Symptom:** `nap_mpd: No such device` or MPD fails to open the output.

**Cause:** The DAC is not card 0 (e.g. onboard audio is still active and was enumerated first).

```bash
# Check actual card numbering
aplay -l

# If your DAC is card 1, edit /etc/asound.conf:
# Change: card 0  →  card 1  in pcm.nap_hw and ctl.nap_hw
sudo nano /etc/asound.conf
sudo systemctl restart nap-backend.service
```

Alternatively, disable the onboard audio in `/boot/firmware/config.txt`:

```
dtparam=audio=off
```

### Bluetooth Not Connecting

**Symptom:** Phone cannot pair or audio does not stream.

```bash
# Check Bluetooth service
sudo systemctl status bluetooth.service

# Check bluealsa
sudo systemctl status bluetooth-audio.service

# Enter interactive Bluetooth management
bluetoothctl
  power on
  agent on
  discoverable on
  pairable on
  scan on
  # Wait for device MAC to appear, then:
  pair XX:XX:XX:XX:XX:XX
  trust XX:XX:XX:XX:XX:XX
  connect XX:XX:XX:XX:XX:XX
  quit

# Check bluealsa sees connected devices
bluealsa-aplay -l
```

### Web UI Not Reachable

**Symptom:** Browser shows "connection refused" or times out.

```bash
# Check the backend is running
sudo systemctl status nap-backend.service

# Check it is listening on port 8000
ss -tlnp | grep 8000

# Check for startup errors
journalctl -u nap-backend.service -n 50 --no-pager

# Try from the Pi itself
curl -s http://127.0.0.1:8000/api/v1/health
```

If the service failed to start, the most common causes are:

```bash
# Python import error
cd /opt/nap && venv/bin/python -c "import backend.app.main"

# Missing config file
ls /etc/nap/config.json

# Permission on lock file
ls -la /var/run/audio.lock
```

### nap-backend.service Fails to Start

```bash
journalctl -u nap-backend.service -n 100 --no-pager

# Common fix: re-run installer to repair any missing file
sudo bash /opt/nap/scripts/install.sh --no-apt
```

### MPD Not Playing

```bash
sudo systemctl status mpd.service
journalctl -u mpd.service -n 50 --no-pager

# Check MPD config
sudo mpd --check-config /etc/mpd.conf

# Test MPD CLI
mpc -h 127.0.0.1 status
mpc -h 127.0.0.1 play
```

### AirPlay Not Discoverable

```bash
# avahi-daemon must be running for mDNS
sudo systemctl status avahi-daemon.service

# Verify shairport-sync is active
sudo systemctl status audio-airplay.target
sudo systemctl status shairport-sync.service

# Check shairport-sync logs
journalctl -u shairport-sync.service -n 50 --no-pager
```

### Source Switch Hangs or Times Out

**Symptom:** `POST /api/v1/source` takes longer than `lock_timeout` (default 8 s) and returns `{"detail": "..."}`.

```bash
# Check if audio.lock is held
cat /var/run/audio.lock
fuser /var/run/audio.lock

# Increase timeout in config if hardware is slow
# /etc/nap/config.json:  "lock_timeout": 15.0
sudo systemctl restart nap-backend.service
```

---

## 13. Logs

### Backend Service Logs (systemd journal)

```bash
# Follow live
journalctl -u nap-backend.service -f

# Last 100 lines
journalctl -u nap-backend.service -n 100 --no-pager

# Since last boot
journalctl -u nap-backend.service -b --no-pager

# Errors only
journalctl -u nap-backend.service -p err --no-pager
```

### All Audio Service Logs

```bash
# All NAP-related units at once
journalctl -u "nap-*" -u mpd -u shairport-sync -u bluetooth-audio -f

# System-wide errors (useful after a failed boot)
journalctl -xe --no-pager | head -100
```

### In-Memory Log Buffer (API)

The backend keeps the last 500 log lines in memory, queryable without SSH:

```bash
# Last 50 lines
curl -s "http://nap.local:8000/api/v1/logs?n=50" | jq '.entries[].message'

# Errors only
curl -s "http://nap.local:8000/api/v1/logs?level=ERROR" | jq .
```

### OTA Update History

```bash
curl -s http://nap.local:8000/api/v1/ota/history | jq '.history | reverse | .[:5]'
```

---

*For hardware wiring details see [config/wiring_diagram.txt](../config/wiring_diagram.txt).*  
*For ALSA tuning details see [config/asound.conf](../config/asound.conf) and [config/mpd.conf](../config/mpd.conf).*
