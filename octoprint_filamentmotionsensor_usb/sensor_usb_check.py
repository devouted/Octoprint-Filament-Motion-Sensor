# coding=utf-8
'''
Standalone test for a filament motion sensor connected through an
MCP2221A USB-to-GPIO adapter (read via Adafruit Blinka).

Run it with the OctoPrint environment's Python, for example:

    BLINKA_MCP2221=1 /home/pi/oprint/bin/python3 \
        /home/pi/oprint/lib/python3.12/site-packages/octoprint_filamentmotionsensor_usb/sensor_usb_check.py

(~oprint and the python version may differ on your system.)

Change SENSOR_PIN below to the MCP2221A pin your sensor is wired to
(one of "G0", "G1", "G2", "G3").

Error fixes:
  Install Blinka into the OctoPrint environment:
      /home/pi/oprint/bin/pip install adafruit-blinka
  Make sure the MCP2221A is plugged in and visible:
      lsusb | grep -i 2221       # expect: Microchip ... MCP2221 USB-I2C/UART
  On Linux you may need a udev rule / permissions for the HID device, or run
  the test as a user in the correct group (e.g. plugdev) rather than root.
'''

import os
import sys
import time

# Target the MCP2221A before importing board.
os.environ.setdefault("BLINKA_MCP2221", "1")
os.environ.setdefault("BLINKA_MCP2221_RESET_DELAY", "-1")

SENSOR_PIN = "G0"   # change to the pin your sensor uses


def main():
    try:
        import board
        import digitalio
    except Exception as ex:
        print("Could not import Blinka (board/digitalio):", ex)
        print("Install it with: pip install adafruit-blinka")
        sys.exit(1)

    pin_name = SENSOR_PIN.strip().upper()

    try:
        board_pin = getattr(board, pin_name)
    except AttributeError:
        print("Unknown pin '%s' for this board." % pin_name)
        print("Available pins:", [a for a in dir(board) if a.startswith("G")])
        sys.exit(1)

    try:
        dio = digitalio.DigitalInOut(board_pin)
        dio.direction = digitalio.Direction.INPUT
    except Exception as ex:
        print("Could not open MCP2221A pin %s: %s" % (pin_name, ex))
        print("Is the MCP2221A plugged in? Check: lsusb | grep -i 2221")
        sys.exit(1)

    print("MCP2221A reachable. Reading pin %s." % pin_name)
    print("Current pin value:", dio.value)
    print("Now polling... move the filament through the sensor. Ctrl+C to stop.")

    last_value = dio.value
    transitions = 0
    last_report = time.time()

    try:
        while True:
            value = dio.value
            if value != last_value:
                transitions += 1
                print("transition #%d -> %s" % (transitions, int(value)))
                last_value = value

            now = time.time()
            if now - last_report >= 2.0:
                print("... %d transitions so far (current=%d)"
                      % (transitions, int(value)))
                last_report = now

            time.sleep(0.005)
    except KeyboardInterrupt:
        print("\nStopped. Total transitions:", transitions)
    finally:
        try:
            dio.deinit()
        except Exception:
            pass


if __name__ == '__main__':
    main()
