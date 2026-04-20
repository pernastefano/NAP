# NAP – Hardware Installation Guide

**Target:** Raspberry Pi 4 Model B  
**Repository:** https://github.com/pernastefano/NAP  
**Assembly time:** ~1–2 hours for a first build

> **Safety first:** Always power off the Raspberry Pi before connecting or disconnecting components. The Pi's GPIO pins are **not** 5 V tolerant — connecting a 5 V signal directly will permanently damage the SoC.

---

## Table of Contents

1. [Components List](#1-components-list)
2. [Tools Required](#2-tools-required)
3. [GPIO Reference](#3-gpio-reference)
4. [Wiring Rules](#4-wiring-rules)
5. [Step 1 — Connect the LCD](#5-step-1--connect-the-lcd)
6. [Step 2 — Connect the Rotary Encoder](#6-step-2--connect-the-rotary-encoder)
7. [Step 3 — Connect the Power Button](#7-step-3--connect-the-power-button)
8. [Step 4 — Connect Action Buttons](#8-step-4--connect-action-buttons)
9. [Step 5 — Connect the IR Receiver](#9-step-5--connect-the-ir-receiver)
10. [Step 6 — Connect the DAC](#10-step-6--connect-the-dac)
11. [Testing Hardware](#11-testing-hardware)
12. [Common Issues](#12-common-issues)
13. [Optional Improvements](#13-optional-improvements)

---

## 1. Components List

### Required

| # | Component | Notes |
|---|---|---|
| 1 | Raspberry Pi 4 Model B | Any RAM variant (1/2/4/8 GB) |
| 1 | MicroSD card | 16 GB min, Class 10 / A1 |
| 1 | Official Pi 4 USB-C PSU | 5.1 V / 3 A; cheap chargers cause undervoltage |
| 1 | HD44780 16×2 LCD | Must have PCF8574 I2C backpack attached |
| 1 | Rotary encoder | KY-040, Alps EC11, or PEC11R series |
| 1 | Momentary push button (power) | Panel-mount, 12 mm or 16 mm, normally open (NO) |
| 3 | Momentary push buttons (action) | 6×6 mm tactile or panel-mount, normally open (NO) |
| 1 | IR receiver module | VS1838B, TSOP4838, or TSOP31238 (38 kHz) |
| — | Jumper wires | Male-to-female for breadboard/header work |
| — | Breadboard (optional) | For prototyping before final build |

### Passive Components (strongly recommended)

| Component | Qty | Value | Purpose |
|---|---|---|---|
| Resistor | 2 | 10 kΩ | Encoder RC filter (one per channel) |
| Capacitor (film/ceramic) | 2 | 100 nF | Encoder RC filter (one per channel) |
| Capacitor (ceramic) | 3 | 100 nF | Button debounce (one per button, optional) |
| Resistor | 1 | 1 kΩ | IR receiver data line protection |
| Resistor | 1 | 100 Ω | IR receiver VCC supply decoupling |
| Capacitor (electrolytic) | 1 | 100 µF | IR receiver VCC bulk decoupling |

### Optional

| Component | Notes |
|---|---|
| USB DAC | Any USB Audio Class 1/2 device (e.g. AudioQuest DragonFly, FiiO E10K) |
| I2S DAC HAT | HiFiBerry DAC+, Allo Boss, JustBoom DAC — fits directly on the header |
| IR remote control | NEC protocol (most common); Sony and RC-5 also work |
| Enclosure | Any project box; suggest ≥120×80×40 mm to fit Pi + PCB + connectors |

---

## 2. Tools Required

- Small Phillips screwdriver
- Wire stripper / cutter
- Multimeter (for continuity checks)
- Soldering iron + solder (for final assembly; not required for breadboard)
- Fine-tip tweezers (for small capacitors)

---

## 3. GPIO Reference

All pin numbers in this guide use **BCM (Broadcom) numbering** — the same numbering used in `hardware_input.py` and `config/wiring.conf`.

### Complete Wiring Table

| Component | Signal | BCM GPIO | Physical Pin | Direction | Pull |
|---|---|---|---|---|---|
| LCD (I2C backpack) | SDA | 2 | 3 | I2C | 4.7 kΩ (on board) |
| LCD (I2C backpack) | SCL | 3 | 5 | I2C | 4.7 kΩ (on board) |
| LCD (I2C backpack) | VCC | — | 2 (5 V) | Power | — |
| LCD (I2C backpack) | GND | — | 6 (GND) | Power | — |
| Rotary encoder | CLK (A) | 17 | 11 | INPUT | PUD_UP + RC filter |
| Rotary encoder | DT (B) | 18 | 12 | INPUT | PUD_UP + RC filter |
| Rotary encoder | SW (btn) | 27 | 13 | INPUT | PUD_UP |
| Rotary encoder | + (VCC) | — | 17 (3.3 V) | Power | — |
| Rotary encoder | GND | — | 14 (GND) | Power | — |
| Power button | Terminal A | 22 | 15 | INPUT | PUD_UP |
| Power button | Terminal B | — | 14 (GND) | Power | — |
| Action button — play/pause | Terminal A | 23 | 16 | INPUT | PUD_UP |
| Action button — next | Terminal A | 24 | 18 | INPUT | PUD_UP |
| Action button — previous | Terminal A | 25 | 22 | INPUT | PUD_UP |
| All action buttons | Terminal B | — | 14/20/25 (GND) | Power | — |
| IR receiver | OUT (data) | 16 | 36 | kernel | Internal (TSOP) |
| IR receiver | VCC | — | 1 (3.3 V via 100 Ω) | Power | — |
| IR receiver | GND | — | 39 (GND) | Power | — |

### 40-Pin Header Overview

```
         3V3  [1]  [2]  5V
   SDA / GPIO2  [3]  [4]  5V
   SCL / GPIO3  [5]  [6]  GND
              [7]  [8]
         GND  [9]  [10]
  enc_a GPIO17 [11]  [12] GPIO18 enc_b
  enc_btn GPIO27 [13]  [14] GND ←── button GND rail
  pwr_btn GPIO22 [15]  [16] GPIO23 play/pause btn
         3V3 [17]  [18] GPIO24 next btn
             [19]  [20] GND
             [21]  [22] GPIO25 prev btn
             [23]  [24]
         GND [25]  [26]
             [27]  [28]
             [29]  [30] GND
             [31]  [32]
             [33]  [34] GND
             [35]  [36] GPIO16 IR data
             [37]  [38]
         GND [39]  [40]
```

> **I2S DAC conflict:** GPIO18 (physical pin 12) is also the I2S PCM clock. If you use an I2S DAC HAT (HiFiBerry, Allo Boss), the kernel claims GPIO18 and it cannot be used for the encoder. In that case, move the encoder DT wire to GPIO20 (physical pin 38) and update `PinConfig.encoder_b = 20` in `backend/app/hardware_input.py`.

---

## 4. Wiring Rules

Understanding these rules will prevent 90% of wiring mistakes.

### Rule 1 — All GPIO signals are 3.3 V

The Raspberry Pi 4's GPIO pins operate at **3.3 V**. They are **not** 5 V tolerant. Connecting a 5 V signal (e.g. from a 5 V Arduino or a 5 V sensor) directly to a GPIO pin will damage or destroy the SoC.

The LCD backpack and IR receiver are powered from the correct rails:
- LCD backpack: **5 V** for the HD44780 and backlight, I2C signals at **3.3 V** (the PCF8574 accepts 3.3 V logic)
- IR receiver: **3.3 V** supply (VS1838B / TSOP4838 work from 2.5 V–5.5 V)

### Rule 2 — Active-LOW wiring for all buttons

Every button and switch in NAP uses **active-LOW** wiring:

```
GPIO pin ──── [ button ] ──── GND
```

When the button is **open** (not pressed): the GPIO pin is pulled HIGH to 3.3 V by the internal pull-up → reads as **1**.  
When the button is **closed** (pressed): the GPIO pin is connected to GND → reads as **0**.

This is the safest convention because pressing the button can only pull the pin to GND, never above 3.3 V.

### Rule 3 — Always enable internal pull-up resistors

The Pi has internal pull-up resistors (~50 kΩ) on every GPIO pin. NAP enables them in software with `gpio.setup(pin, gpio.IN, pull_up_down=gpio.PUD_UP)`.

**Never leave a GPIO input floating** (connected to nothing). A floating pin picks up electromagnetic noise and generates phantom events. Always either enable a pull-up (preferred for active-LOW wiring) or add an external pull-up or pull-down resistor.

### Rule 4 — Add RC filters to the encoder

Mechanical rotary encoders produce electrical bounce when the contacts make or break. The internal 50 kΩ pull-up combined with the GPIO input capacitance (~2 pF) forms an RC filter at ~1.6 MHz — far too high to reject bounce pulses (typically 1–10 µs).

Add an **external RC filter** on each encoder channel:

```
encoder CLK ──── 10 kΩ ──┬──── GPIO17
                          │
                        100 nF
                          │
                         GND
```

This creates a low-pass filter at $f_c = \frac{1}{2\pi \times 10\text{k}\Omega \times 100\text{nF}} \approx 159\text{ Hz}$, rejecting bounce spikes while passing intentional rotations. The 5 ms software debounce in `_EncoderTracker` handles any residual bounce.

### Rule 5 — Share a common GND

All components must share the same ground reference as the Raspberry Pi. Use one of the GND pins (physical pins 6, 9, 14, 20, 25, 30, 34, 39) and connect all component GNDs to it, either directly or via a GND rail on a breadboard.

---

## 5. Step 1 — Connect the LCD

The LCD module is an HD44780 16×2 display with a PCF8574 I2C backpack soldered onto the back. You only need **4 wires** to the Pi.

### Connections

| LCD backpack pin | Pi header | Physical pin |
|---|---|---|
| VCC | 5 V | 2 or 4 |
| GND | GND | 6 |
| SDA | GPIO2 / SDA1 | 3 |
| SCL | GPIO3 / SCL1 | 5 |

### Contrast Adjustment

The backpack has a small blue trimmer potentiometer labelled **CONTRAST** or **VR1**. After powering on:

1. The LCD should show two rows of filled rectangles (all pixels on)
2. Turn the trimmer clockwise or counter-clockwise until characters are clearly visible
3. If you see nothing at all, try turning the trimmer fully in both directions

### Verify

After connecting, with the Pi powered on and I2C enabled:

```bash
sudo i2cdetect -y 1
```

Expected output — `27` should appear in the grid:

```
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:          -- -- -- -- -- -- -- -- -- -- -- -- --
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
20: -- -- -- -- -- -- -- 27 -- -- -- -- -- -- -- --
30: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
```

If you see `3f` instead of `27`, your backpack uses a PCF8574**A** chip. Update `lcd_ui.py`:

```python
lcd = CharLCD('PCF8574', 0x3F)  # change address
```

If nothing appears, check power (5 V and GND) and SDA/SCL connections.

---

## 6. Step 2 — Connect the Rotary Encoder

The KY-040 breakout board has 5 pins. Cheap encoder breakout boards usually include pull-up resistors on CLK and DT — if yours does, the external RC filter components are still recommended for best quality.

### Connections

| Encoder pin | Pi header | Physical pin | Notes |
|---|---|---|---|
| `+` (VCC) | 3.3 V | 17 | NOT 5 V |
| `GND` | GND | 14 | |
| `CLK` | GPIO17 | 11 | via 10 kΩ + 100 nF RC filter |
| `DT` | GPIO18 | 12 | via 10 kΩ + 100 nF RC filter |
| `SW` | GPIO27 | 13 | direct wire |

### RC Filter Wiring (per channel)

Wire each channel (CLK and DT) through the filter before reaching the Pi pin:

```
Encoder CLK ──── [10 kΩ] ──┬──── Pi pin 11 (GPIO17)
                            │
                         [100 nF]
                            │
                           GND
```

Repeat identically for DT → Pi pin 12 (GPIO18).

### Rotation Direction

If rotating clockwise decreases volume instead of increasing it, the encoder channels are swapped. Either:
- Swap the CLK and DT wires physically, **or**
- Swap `encoder_a` and `encoder_b` values in `PinConfig`:

```python
# backend/app/hardware_input.py
encoder_a = 18   # was 17
encoder_b = 17   # was 18
```

---

## 7. Step 3 — Connect the Power Button

The power button is the most important hardware input in NAP. It uses a **momentary normally-open (NO) panel-mount button**.

### Connections

| Button terminal | Pi header | Physical pin |
|---|---|---|
| Terminal A | GPIO22 | 15 |
| Terminal B | GND | 14 |

### Behaviour

| Press duration | Action | Code path |
|---|---|---|
| < 600 ms (short) | Toggle audio ON/OFF (IDLE ↔ last source) | `on_power_short()` |
| ≥ 600 ms (long) | System shutdown (`systemctl poweroff`) | `on_power_long()` |

The 600 ms threshold is defined by `_LONG_PRESS_MS = 600` in `hardware_input.py` and can be adjusted via `PinConfig.long_press_ms`.

### Safe Wiring

The internal 50 kΩ pull-up ensures the pin reads HIGH when the button is open. Pressing the button connects the pin to GND — the only current that flows is $\frac{3.3V}{50k\Omega} = 66\,\mu A$, which is harmless.

**Optional hardware debounce:** place a 100 nF ceramic capacitor between GPIO22 and GND (as close to the Pi pin as possible). This prevents false long-press detection from a noisy button contact.

```
GPIO22 (pin 15) ──┬──── [ Button ] ──── GND (pin 14)
                  │
               [100 nF]
                  │
                 GND
```

### Choosing the Right Button

For a panel-mount power button, use:
- **Latching** buttons — **not suitable**; NAP uses a momentary button; a latching button would hold the GPIO low permanently
- **Momentary NO** — correct; the circuit is open at rest and closes only while pressed
- **Illuminated momentary** buttons work well; wire the LED to 3.3 V and GND separately (do not connect to the GPIO pin)

---

## 8. Step 4 — Connect Action Buttons

Three optional action buttons provide direct playback control without the Web UI.

### Connections

| Button | GPIO | Physical pin | GND pin |
|---|---|---|---|
| Play / Pause | GPIO23 | 16 | 14 |
| Next track | GPIO24 | 18 | 20 |
| Previous track | GPIO25 | 22 | 25 |

Each button: one terminal to the GPIO pin, other terminal to any GND pin.

### Register Buttons in Code

Buttons must be declared in `PinConfig.action_buttons` as `(pin, id)` tuples. Edit `backend/app/hardware_input.py` or pass a custom `PinConfig` at startup:

```python
from backend.app.hardware_input import PinConfig, HardwareInput

cfg = PinConfig(
    action_buttons=[
        (23, "play_pause"),
        (24, "next"),
        (25, "previous"),
    ]
)
hw = HardwareInput(cfg)
hw.on_button = lambda btn_id, long: print(f"Button: {btn_id}, long={long}")
hw.start()
```

The `long` parameter is `True` if the button was held ≥ 600 ms, enabling a secondary function on each button (e.g. long-press Next → shuffle).

---

## 9. Step 5 — Connect the IR Receiver

The VS1838B / TSOP4838 is a 3-lead TO-92 package. Facing the dome (rounded side), the pins are left to right: **OUT — GND — VCC**.

> Check the datasheet for your specific module — pin order varies between manufacturers. The TSOP4838 and VS1838B have **opposite pin orders**.

### Connections

| IR receiver pin | Connection | Notes |
|---|---|---|
| VCC | 3.3 V (pin 1) via 100 Ω resistor | Decoupling resistor on supply rail |
| GND | GND (pin 39) | |
| OUT | GPIO16 (pin 36) via 1 kΩ resistor | Series resistor for GPIO protection |

### Full Circuit

```
3.3V (pin 1) ──── [100 Ω] ──┬──── VCC (IR pin 3)
                             │
                         [100 µF] ║ [100 nF]    (bulk + bypass decoupling to GND)
                             │
                            GND

GND (pin 39) ───────────────────── GND (IR pin 2)

IR OUT (pin 1) ──── [1 kΩ] ──┬──── GPIO16 (pin 36)
                              │
                           [100 nF]
                              │
                             GND
```

### Kernel Setup

The IR receiver data line is managed by the Linux `gpio-ir` kernel driver, not by Python. Add this line to `/boot/firmware/config.txt`:

```
dtoverlay=gpio-ir,gpio_pin=16
```

Reboot. The kernel will create `/dev/lircX` and an input event device. The NAP udev rule (installed by `install.sh`) symlinks it to `/dev/input/ir-keys`.

> **Do not** call `gpio.setup(16, ...)` from Python — the kernel owns this pin after the overlay loads.

### Verify

Point your remote at the receiver and press a button:

```bash
# Check the symlink exists
ls -la /dev/input/ir-keys

# Listen for raw events (press any remote button)
evtest /dev/input/ir-keys
```

You should see `EV_KEY` events printed for each button press.

### IR Key Mapping

NAP maps raw key codes to action names in `IR_KEY_MAP` (in `hardware_input.py`). Defaults:

| Key code | Action |
|---|---|
| `0x0C` | play_pause |
| `0x40` | next |
| `0x19` | prev |
| `0x15` | vol_up |
| `0x07` | vol_down |
| `0x45` | mute |
| `0x16` | source_mpd |
| `0x09` | source_airplay |

To find the codes for your specific remote, run `evtest` and note the `code` value printed when each button is pressed. Update `IR_KEY_MAP` accordingly.

---

## 10. Step 6 — Connect the DAC

### Option A — USB DAC

Plug the USB DAC into any USB-A port on the Pi. No wiring required.

After connecting, verify ALSA detects it:

```bash
aplay -l
```

If the DAC is not card 0 (the onboard audio was detected first), disable the onboard audio in `/boot/firmware/config.txt`:

```
dtparam=audio=off
```

Then update `/etc/asound.conf` — change `card 0` to the correct card number in the `pcm.nap_hw` and `ctl.nap_hw` sections.

### Option B — I2S DAC HAT

I2S HATs plug directly onto the 40-pin header. Follow the manufacturer's instructions for the `dtoverlay` setting. Example for HiFiBerry DAC+:

```
# /boot/firmware/config.txt
dtparam=audio=off
dtoverlay=hifiberry-dacplus
```

> **GPIO18 conflict:** I2S HATs use GPIO18 (PCM_CLK). Move the encoder DT wire from GPIO18 to **GPIO20** (physical pin 38) and update `PinConfig.encoder_b = 20`.

After rebooting, verify:

```bash
aplay -l
# Should show the DAC as card 0
```

---

## 11. Testing Hardware

Power on the Pi with everything connected. Run each test in order.

### 11.1 I2C — LCD Detection

```bash
sudo i2cdetect -y 1
```

Expect address `27` (PCF8574) or `3f` (PCF8574A). If neither appears:
- Check VCC (5 V) and GND connections
- Check SDA (pin 3) and SCL (pin 5) connections
- Verify the I2C interface is enabled: `sudo raspi-config nonint get_i2c` → should print `0`

### 11.2 GPIO — Test a Single Pin

```bash
python3 - <<'EOF'
import RPi.GPIO as GPIO
import time

GPIO.setmode(GPIO.BCM)
GPIO.setup(22, GPIO.IN, pull_up_down=GPIO.PUD_UP)

print("Press the power button (GPIO22). Ctrl+C to exit.")
try:
    while True:
        state = GPIO.input(22)
        print(f"GPIO22 = {state} ({'HIGH - not pressed' if state else 'LOW - PRESSED'})")
        time.sleep(0.1)
except KeyboardInterrupt:
    pass
finally:
    GPIO.cleanup()
EOF
```

Expected: prints `HIGH` at rest, `LOW` while the button is held.

### 11.3 Encoder — Rotation and Button

```bash
python3 - <<'EOF'
import RPi.GPIO as GPIO
import time

A, B, BTN = 17, 18, 27
GPIO.setmode(GPIO.BCM)
for pin in (A, B, BTN):
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

print("Rotate encoder CW/CCW and press the button. Ctrl+C to exit.")
last_a = GPIO.input(A)
try:
    while True:
        a, b, btn = GPIO.input(A), GPIO.input(B), GPIO.input(BTN)
        if a != last_a:
            direction = "CW" if a != b else "CCW"
            print(f"Encoder: {direction}  (A={a} B={b})")
            last_a = a
        if not btn:
            print("Encoder button PRESSED")
            time.sleep(0.3)
        time.sleep(0.001)
except KeyboardInterrupt:
    pass
finally:
    GPIO.cleanup()
EOF
```

### 11.4 IR Receiver

```bash
# Requires gpio-ir overlay loaded and evdev installed
evtest /dev/input/ir-keys
```

Point any NEC-protocol remote at the receiver and press buttons. You should see `EV_KEY` events. If the device does not exist, check:

```bash
ls /dev/input/     # look for eventX devices
dmesg | grep gpio_ir
```

### 11.5 Full Hardware Stack

Once individual components are verified, start the NAP backend and exercise everything through the real code:

```bash
sudo systemctl start nap-backend.service
journalctl -u nap-backend.service -f
```

Open `http://nap.local:8000/` in a browser. The Web UI should show the current source and respond to API calls. The LCD should show the root page. Rotating the encoder should navigate the LCD menu.

---

## 12. Common Issues

### LCD shows nothing / shows solid blocks

| Symptom | Cause | Fix |
|---|---|---|
| Solid filled rectangles on row 1 | Contrast too high | Turn trimmer counter-clockwise |
| Completely blank | No power or wrong contrast | Check 5 V and GND; turn trimmer fully CW then slowly CCW |
| `i2cdetect` shows nothing | SDA/SCL swapped, or I2C not enabled | Swap pins 3 and 5; run `sudo raspi-config nonint do_i2c 0` |
| `i2cdetect` shows `3f` not `27` | PCF8574A variant | Change address in `lcd_ui.py`: `CharLCD('PCF8574', 0x3F)` |
| I2C address correct but display garbled | 5 V on SDA/SCL | Check backpack VCC source; SDA/SCL must be 3.3 V logic |

### Encoder misbehaves

| Symptom | Cause | Fix |
|---|---|---|
| Rotation direction reversed | CLK/DT swapped | Swap wires or swap `encoder_a`/`encoder_b` in `PinConfig` |
| Skipping steps | No RC filter; software debounce too loose | Add 10 kΩ + 100 nF per channel |
| Double steps per click | Both RISING and FALLING caught unintentionally | Verify `edge = BOTH` in `_setup_gpio` and check RC filter values |
| Encoder button not registering | Missing GND or pin conflict | Verify SW → GPIO27, GND → pin 14 |

### Buttons triggering randomly

| Symptom | Cause | Fix |
|---|---|---|
| Phantom presses with no contact | Floating input (pull-up not active) | Confirm `pull_up_down=GPIO.PUD_UP` in code |
| Jitter when pressed | Long wire acting as antenna | Add 100 nF between GPIO and GND near the Pi pin; shorten wires |
| Short press detected as long | Debounce too short | Increase `PinConfig.debounce_ms` from 30 to 50 |

### IR receiver not working

| Symptom | Cause | Fix |
|---|---|---|
| `/dev/input/ir-keys` missing | gpio-ir overlay not loaded | Add `dtoverlay=gpio-ir,gpio_pin=16` to `/boot/firmware/config.txt` and reboot |
| Device exists but no events | Wrong pin, or VS1838B pin order misread | Check module datasheet; VS1838B pin order differs from TSOP4838 |
| Events appear but wrong key codes | Different remote protocol | Run `evtest` to find actual codes; update `IR_KEY_MAP` in `hardware_input.py` |
| 5 V IR module on 3.3 V GPIO | Overvoltage on GPIO | Add 1 kΩ series resistor + 3.3 V zener on data line; or switch to 3.3 V module |

### Power button triggers on boot

| Symptom | Cause | Fix |
|---|---|---|
| Shutdown triggered at startup | Pin floating briefly during boot | Verify 100 nF capacitor between GPIO22 and GND is in place |
| Long-press detected unintentionally | Software starts checking immediately | The 600 ms threshold in `_ButtonTracker` should prevent this; check for stuck button |

### GPIO conflict with I2S DAC

| Symptom | Cause | Fix |
|---|---|---|
| Encoder B (GPIO18) stops working after DAC HAT installed | I2S kernel driver claims GPIO18 | Move encoder DT to GPIO20; update `PinConfig.encoder_b = 20` |

---

## 13. Optional Improvements

### Prototype → Permanent: Stripboard or Custom PCB

For a permanent installation, transfer the breadboard circuit to stripboard (Veroboard) or design a simple PCB. A PCB eliminates unreliable jumper wire connections and is the single biggest reliability improvement for a production build.

Free tools: [KiCad](https://www.kicad.org/) (open source), [EasyEDA](https://easyeda.com/) (web-based, free PCB fabrication integration).

### Noise Reduction

- **Decoupling capacitors:** Place a 100 nF ceramic capacitor between VCC and GND at each IC's power pin (PCF8574, IR receiver). Mount them as close to the chip as possible.
- **Star grounding:** Run a single heavy wire from Pi GND to a central ground point; connect all component GNDs there rather than daisy-chaining.
- **Short wires:** Keep signal wires (encoder, buttons) under 20 cm. Longer wires act as antennas for 50/60 Hz mains interference.
- **Twisted pairs:** For encoder CLK/DT, twist the signal and GND wires together to reduce differential noise pickup.

### Power Supply

- Use the **official Raspberry Pi 4 USB-C PSU** (5.1 V / 3 A). Under-powered Pis show the lightning bolt undervoltage icon and exhibit random reboots, audio glitches, and I2C errors.
- If powering from a custom supply, ensure it can deliver at least 3 A continuously. Add a 470 µF electrolytic capacitor across the Pi's 5 V supply near the USB-C port to absorb transient load spikes from audio bursts.
- For mobile / car use, add a car-grade DC-DC buck converter (e.g. Pololu D24V25F5) rather than a USB car charger.

### Enclosure

A well-designed enclosure:
- Prevents shorts from loose wires touching the Pi board
- Keeps the IR receiver aimed at the room (not blocked by a panel)
- Provides strain relief on cables going to external buttons
- Includes ventilation slots — the Pi 4 under audio load reaches ~60–70 °C without airflow

Suggested minimum internal dimensions: **150 × 100 × 50 mm** to fit the Pi 4, a small stripboard, and panel-mount connectors.

---

*For GPIO pin assignments in machine-readable form, see [config/wiring.conf](../config/wiring.conf).*  
*For the complete ASCII circuit diagram, see [config/wiring_diagram.txt](../config/wiring_diagram.txt).*  
*For software installation, see [docs/INSTALL.md](INSTALL.md).*
