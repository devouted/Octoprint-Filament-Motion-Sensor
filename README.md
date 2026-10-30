# Octoprint-Filament-Motion-Sensor-USB (MCP2221A)

An [OctoPrint](http://octoprint.org/) plugin for a filament motion sensor connected through an **MCP2221A USB-to-GPIO adapter**, read via [Adafruit Blinka](https://pypi.org/project/Adafruit-Blinka/). This makes filament-motion / jam detection work on any host that can run OctoPrint and expose a USB port — not just a Raspberry Pi with onboard GPIO.

This is a fork of [tabahi/Octoprint-Filament-Motion-Sensor](https://github.com/tabahi/Octoprint-Filament-Motion-Sensor) (which uses the Raspberry Pi's onboard GPIO via libgpiod). The jam-detection logic is unchanged; only the hardware-reading layer was rewritten to talk to an MCP2221A over USB.

Based on earlier work: [Octoprint-Filament-Reloaded](https://github.com/kontakt/Octoprint-Filament-Reloaded) by kontakt, [Octoprint-Filament-Revolutions](https://github.com/RomRider/Octoprint-Filament-Revolutions) by RomRider, and [Octoprint-Smart-Filament-Sensor](https://github.com/Royrdan/Octoprint-Smart-Filament-Sensor) by Royrdan.

## How it differs from the GPIO version

| | Raspberry Pi GPIO version | This USB version |
|---|---|---|
| Hardware | RPi onboard GPIO | MCP2221A USB-to-GPIO adapter |
| Library | `libgpiod` (`gpiod`) | `adafruit-blinka` (`board` / `digitalio`) |
| Reading method | Hardware edge interrupts | USB polling (~100–200 Hz) |
| Pin reference | BCM GPIO number | MCP2221A GP pin (`G0`–`G3`) |

The MCP2221A has no interrupt line usable from Blinka, so the plugin polls the pin and detects state transitions in software. For jam detection (no filament movement for X seconds) this is fully sufficient.

## Required sensor

A filament sensor that outputs a toggling digital signal (0‑1‑0…) while the filament moves (motion), and optionally a second switch signal for filament presence (runout).
- A ready-made sensor such as the **BIGTREETECH SFS V2.0** or BTT Smart Filament Sensor. The SFS V2.0 exposes **two** signals: motion and a runout switch.
- Or a DIY slotted-wheel sensor (LED + photodiode) for motion only. Example model by [WindoAC on Thingiverse](https://www.thingiverse.com/thing:3067904).

![BTT smart sensor wiring](./BTT_smart_sensor.jpg)

![Slotted wheel, IR diode and IR sensor prototype](slotted_wheel_IR_diode_animation.gif)

## Wiring (MCP2221A)

The BIGTREETECH SFS V2.0 has a 4-wire connector:

| Wire | Meaning | Connect to |
|---|---|---|
| Black | GND | MCP2221A GND |
| Red | VCC 3.3V | MCP2221A 3V3 |
| Green | Motion signal (toggles while filament moves) | a GP pin, e.g. `G0` |
| Blue | Switch signal (filament present / absent) | a GP pin, e.g. `G1` |

Recommended default: **green → G0 (motion)**, **blue → G1 (runout switch)**. You can pick any `G0`–`G3` per signal in the plugin settings.

Notes:
- The MCP2221A GP pins are 3.3 V logic — do not feed 5 V into them.
- The SFS V2.0 signals expect a pull-up. The MCP2221A does **not** provide a usable internal pull-up through Blinka, so you must use an **external pull-up resistor** on each signal line (the SFS V2.0 modules are commonly used with pull-ups already fitted).

The MCP2221A then connects to the OctoPrint host over USB.

## Features
* **Motion detection** — detects filament jams, clogs and spool tangles (no filament movement) and pauses the print automatically.
* **Runout switch** — optional second signal that pauses the print the moment filament is absent (end of spool, snapped filament).
* Independent pin selection for motion and runout (each can be enabled/disabled separately).
* Responds within a couple of seconds of no filament motion.
* Configurable extrusion-distance and timeout limits before pausing.
* Custom GCode on pause.
* Heater timeout after pause.

## Installation

* Manual install via Plugin Manager → "Get more" → "...from URL":
  `https://github.com/devouted/Octoprint-Filament-Motion-Sensor/archive/master.zip`

After installation, restart OctoPrint.

### Installation Requirements
- Python ≥ 3.7 (tested with 3.12.3)
- [adafruit-blinka](https://pypi.org/project/Adafruit-Blinka/) installed in the OctoPrint environment.
- An MCP2221A adapter plugged into the host via USB.

Blinka targets the MCP2221A when the environment variable `BLINKA_MCP2221=1` is set. The plugin sets this automatically at import time, but if you run the standalone test manually you must set it yourself (see below).

Verify the adapter is enumerated:

```bash
lsusb | grep -i 2221       # expect: Microchip ... MCP2221 USB-I2C/UART
```

On Linux you usually need udev permissions so OctoPrint (and the test tools) can open the MCP2221A without root.

Note: Adafruit Blinka talks to the MCP2221A through hidapi's **libusb** backend, which opens the USB device node (not `/dev/hidraw*`). So the udev rule must target the USB device. Create `/etc/udev/rules.d/99-mcp2221.rules`:

```
SUBSYSTEM=="usb", ATTR{idVendor}=="04d8", ATTR{idProduct}=="00dd", MODE="0660", GROUP="plugdev", TAG+="uaccess"
```

Then reload and re-plug the adapter:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger --attr-match=idVendor=04d8
# unplug and replug the MCP2221A
```

Make sure the OctoPrint user is in the `plugdev` group. A rule targeting `hidraw` alone is **not** enough with the libusb backend.

## Configuration

### Sensor pins (motion + runout)
In the plugin settings you select, independently, which MCP2221A GP pin each signal is wired to:
- **Motion Sensor Pin** (`G0`–`G3` or `Disabled`) — the signal that toggles while filament moves (SFS V2.0 **green** wire).
- **Runout Switch Pin** (`G0`–`G3` or `Disabled`) — the switch that reports filament present/absent (SFS V2.0 **blue** wire). Enable it with the **Enable Runout Switch** checkbox.

Recommended default: motion → `G0`, runout → `G1`. The two pins must be different.

Use the **Test sensor** button and move the filament by hand to confirm the motion signal toggles. To identify which pin is which, see the command-line test below.

### Runout switch polarity
Per the BIGTREETECH SFS V2.0 manual the switch output is **LOW (0) when there is no filament** (with a pull-up on the line). The default setting (*"No filament when signal is LOW"*) matches this. Uncheck it only if your wiring is inverted. A small **debounce** (≈0.2 s) prevents false triggers from contact chatter.

### Pausing Limits (motion)
A print can be paused on two conditions when the filament is not moving:
1. **Extrusion distance limit after jam** — best between 5 mm (slow prints) and 20 mm (fast prints). A grace period can override this for a few seconds to tolerate retractions.
2. **Timeout after jam** — seconds of no motion before pausing. ~5 s for very fast prints, ~120 s for very slow prints.

The runout switch pauses immediately (after the debounce) when filament is absent, independent of these motion limits.

### OctoPrint – Firmware & Protocol
If not printing from SD card, the OctoPrint `@pause` command works for most setups. There is also a custom-GCode option (with or without OctoPrint's pause) if you want to send commands such as `M600` for a filament change. The default custom GCode is just a beep tune.

### OctoPrint – GCode Scripts
The custom GCode managed by the plugin only runs when a print is paused due to a suspected jam. You can also add OctoPrint's own "After print job is paused" / "Before print job is resumed" GCode scripts — just avoid overlapping them with the plugin's custom GCode.

## Testing the sensor from the command line

A standalone test script is included. Run it with the OctoPrint environment's Python, with the MCP2221A plugged in:

```bash
BLINKA_MCP2221=1 /home/pi/oprint/bin/python3 \
  /home/pi/oprint/lib/python3.12/site-packages/octoprint_filamentmotionsensor_usb/sensor_usb_check.py
```

Adjust the path to your environment. Edit `SENSOR_PIN` inside the script to match the GP pin you wired. Move the filament through the sensor and watch the transition counter increase.

### Identifying which pin is motion vs switch

The repository also includes `test_sensor_local.py` with a **dual** mode that reads two pins at once, so you can tell the motion signal from the runout switch:

```bash
BLINKA_MCP2221=1 python3 test_sensor_local.py dual --pin-a 0 --pin-b 1 --duration 20
```

While it runs: **move the filament** through the sensor — the **motion** pin's transition counter climbs quickly. Then **remove/insert the filament** — the **switch** pin flips once and stays. Note which GP pin is which (and the switch's level when filament is absent), then set them in the plugin settings.

## Troubleshooting
- **Plugin disabled / not detecting hardware**: confirm `adafruit-blinka` is installed in the OctoPrint venv and `lsusb` shows the MCP2221A.
- **Permission denied opening the device**: fix USB/HID permissions (udev rule or group membership) for the OctoPrint user.
- **No transitions during the test**: check wiring (signal/VCC/GND), 3.3 V logic level, and that the sensor actually toggles while filament moves.

## Disclaimer
* Neither I nor the previous authors are responsible for any damage to your print or printer. Use sensibly.
* The MCP2221A GP pins are 3.3 V — do not apply 5 V.
* Use custom GCode commands at your own risk.

## Contact
* [Issues](https://github.com/devouted/Octoprint-Filament-Motion-Sensor/issues)
* [OctoPrint Discord](https://discord.octoprint.org/)
