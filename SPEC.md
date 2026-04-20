# Network Music Player – Product Specification

## Overview

This project defines a production-grade Network Music Player built on Raspberry Pi 4.

The system must provide stable multi-source audio playback with deterministic switching and zero ALSA conflicts.

---

## Supported Sources

* MPD (FLAC Web Radio)
* Plexamp Headless
* AirPlay (Shairport-sync)
* Bluetooth (A2DP Sink)

---

## Core Principles

* Only ONE audio service active at a time
* systemd is responsible for service orchestration
* Python is used only for control logic and UI
* No direct process handling in Python
* Deterministic switching via systemctl isolate
* Global ALSA lock mechanism

---

## Architecture

### systemd Targets

* audio-mpd.target
* audio-airplay.target
* audio-plexamp.target
* audio-bluetooth.target

Each target:

* Starts required services
* Stops conflicting services via Conflicts=

---

### Audio Lock

File:

* /var/run/audio.lock

Behavior:

* Acquire before switching
* Release after switch
* Prevent concurrent ALSA access

---

### Backend

Language:

* Python 3.11+

Framework:

* FastAPI

Modules:

* audio_controller.py
* state_manager.py
* config_manager.py
* ota_updater.py
* lcd_ui.py
* hardware_input.py

---

### Hardware

Components:

* I2C LCD 16x2
* Rotary Encoder
* Buttons
* IR Receiver
* Power Button

---

### Power Button Behavior

* Short press → toggle audio system ON/OFF
* Long press → system shutdown

---

### LCD UI

* Menu navigation via encoder
* No flickering (state caching required)
* Double-buffer rendering

---

### Web UI

* Lightweight HTML + JS
* REST API + WebSocket
* Full control and configuration

---

### OTA Updates

* GitHub-based updates
* Manual and scheduled
* Rollback on failure

---

### Installation

* install.sh must install and configure everything
* Must be idempotent

---

### Stability Requirements

* No race conditions
* ALSA lock enforced
* systemd-based switching only
* Watchdog monitoring
* Structured logging

---
