# coding=utf-8
"""
Local, OctoPrint-free test harness for the MCP2221A filament motion sensor
reading layer (MotionSensorUSBThread).

It runs the *real* thread class from the plugin with stub replacements for the
OctoPrint logger and the shared data object, so you can verify the jam-detection
callback logic end to end without installing OctoPrint.

Two modes:

  real   -> reads an actual MCP2221A pin through Blinka. Toggle the pin by hand
            (short G0 to 3V3 / GND) and watch MOVING / STOPPED callbacks.
            Requires: adafruit-blinka installed and the MCP2221A plugged in.

  fake   -> no hardware needed. Monkeypatches the pin read with a simulated
            signal so you can confirm the logic: it toggles for a while
            (=> MOVING), then goes quiet (=> STOPPED after max_not_moving).

Usage:
    # with hardware (toggle pin by hand):
    BLINKA_MCP2221=1 python3 test_sensor_local.py real --pin 0 --timeout 3

    # without hardware (pure logic test):
    python3 test_sensor_local.py fake --timeout 3
"""

import argparse
import sys
import time

sys.path.insert(0, "octoprint_filamentmotionsensor_usb")


# ---- stubs standing in for OctoPrint-provided objects -----------------------

class StubLogger:
    def info(self, *a):
        print("[INFO]", *a)

    def debug(self, *a):
        print("[DEBUG]", *a)

    def warning(self, *a):
        print("[WARN]", *a)

    def warn(self, *a):
        print("[WARN]", *a)

    def error(self, *a):
        print("[ERROR]", *a)


class StubData:
    """Mimics FilamentMotionSensorDetectionData's attributes used by the thread."""
    def __init__(self):
        self.last_motion_detected = time.time()
        self.filament_moving = False


# ---- callback that prints what the plugin would act on ----------------------

class CallbackRecorder:
    def __init__(self):
        self.moving_events = 0
        self.stopped_events = 0
        self._last_state = None

    def __call__(self, moving):
        # The thread calls callback(True) on each detected transition, and
        # callback(False) repeatedly while no motion exceeds the window.
        if moving:
            self.moving_events += 1
            if self._last_state != True:
                print(">>> CALLBACK: MOVING (transition detected)")
            self._last_state = True
        else:
            self.stopped_events += 1
            if self._last_state != False:
                print(">>> CALLBACK: STOPPED (no motion past timeout)")
            self._last_state = False


def run_real(pin, timeout, duration):
    import SensorUSBThread as s

    if not s._BLINKA_AVAILABLE:
        print("Blinka not available:", s._BLINKA_IMPORT_ERROR)
        print("Install it with: pip install adafruit-blinka, and plug in the MCP2221A.")
        return 1

    if not s.plugin_check_mcp2221():
        print("MCP2221A not reachable through Blinka. Is it plugged in? (lsusb | grep -i 2221)")
        return 1

    logger = StubLogger()
    data = StubData()
    cb = CallbackRecorder()

    thread = s.MotionSensorUSBThread(
        1, "LocalTest", pin, timeout, logger, data, pCallback=cb)
    print("Reading MCP2221A pin G%s. Toggle it by hand. Running for %ds..."
          % (pin, duration))
    thread.start()
    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        pass
    thread.keepRunning = False
    thread.join(timeout=2)
    print("---")
    print("MOVING callbacks:", cb.moving_events, "| STOPPED callbacks:", cb.stopped_events)
    return 0


def run_fake(timeout, duration):
    """Logic-only test: patch the thread's pin read with a simulated signal."""
    import SensorUSBThread as s

    logger = StubLogger()
    data = StubData()
    cb = CallbackRecorder()

    # Simulated signal: toggles every ~50ms for the first `move_phase` seconds
    # (=> transitions => MOVING), then holds steady (=> STOPPED after timeout).
    move_phase = max(1.0, duration * 0.4)
    start = time.time()
    state = {"v": False, "last_toggle": start}

    class FakeDio:
        direction = None

        @property
        def value(self):
            now = time.time()
            if (now - start) < move_phase:
                # toggle at ~20 Hz to generate transitions
                if now - state["last_toggle"] >= 0.05:
                    state["v"] = not state["v"]
                    state["last_toggle"] = now
            # after move_phase: value stays constant -> no transitions
            return state["v"]

        def deinit(self):
            pass

    # Build the thread but bypass hardware: inject FakeDio and skip _open_pin.
    thread = s.MotionSensorUSBThread(
        1, "LocalFakeTest", 0, timeout, logger, data, pCallback=cb)
    thread._open_pin = lambda: FakeDio()

    print("FAKE mode: simulating a moving sensor for ~%.1fs, then silence."
          % move_phase)
    print("Expect MOVING during motion, then STOPPED ~%ds after it goes quiet."
          % timeout)
    thread.start()
    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        pass
    thread.keepRunning = False
    thread.join(timeout=2)

    print("---")
    print("MOVING callbacks:", cb.moving_events, "| STOPPED callbacks:", cb.stopped_events)

    ok = cb.moving_events > 0 and cb.stopped_events > 0
    print("RESULT:", "PASS" if ok else "FAIL",
          "(expected both MOVING>0 and STOPPED>0)")
    return 0 if ok else 2


def run_dual(pin_a, pin_b, duration):
    """
    Read two MCP2221A pins at once to identify which is MOTION and which is the
    runout SWITCH. Move the filament by hand -> the MOTION pin toggles rapidly.
    Remove / insert the filament -> the SWITCH pin flips once and stays.
    """
    import os
    os.environ.setdefault("BLINKA_MCP2221", "1")
    os.environ.setdefault("BLINKA_MCP2221_RESET_DELAY", "-1")
    try:
        import board, digitalio
    except Exception as ex:
        print("Blinka import failed:", ex)
        return 1

    names = {pin_a: "G%d" % pin_a, pin_b: "G%d" % pin_b}
    dios = {}
    for n in (pin_a, pin_b):
        try:
            d = digitalio.DigitalInOut(getattr(board, names[n]))
            d.direction = digitalio.Direction.INPUT
            dios[n] = d
        except Exception as ex:
            print("Could not open %s: %s" % (names[n], ex))
            return 1

    print("Reading %s and %s for %ds." % (names[pin_a], names[pin_b], duration))
    print("Move filament -> MOTION pin toggles fast. Remove/insert filament -> SWITCH pin flips.")
    print("time    %-4s %-4s   transitions(A/B)" % (names[pin_a], names[pin_b]))

    last = {n: dios[n].value for n in dios}
    trans = {n: 0 for n in dios}
    start = time.time()
    last_print = 0.0
    try:
        while time.time() - start < duration:
            for n in dios:
                v = dios[n].value
                if v != last[n]:
                    trans[n] += 1
                    last[n] = v
            now = time.time() - start
            if now - last_print >= 0.25:
                print("%5.1fs   %-4s %-4s   %d / %d" % (
                    now, int(last[pin_a]), int(last[pin_b]),
                    trans[pin_a], trans[pin_b]))
                last_print = now
            time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        for d in dios.values():
            try:
                d.deinit()
            except Exception:
                pass

    print("---")
    print("Transitions: %s=%d, %s=%d"
          % (names[pin_a], trans[pin_a], names[pin_b], trans[pin_b]))
    hi = max(trans, key=lambda k: trans[k])
    lo = pin_b if hi == pin_a else pin_a
    if trans[hi] > trans[lo] and trans[hi] > 3:
        print("HINT: %s toggled more -> likely MOTION; %s -> likely SWITCH/runout."
              % (names[hi], names[lo]))
    else:
        print("HINT: not enough movement to tell them apart. "
              "Move the filament more for the MOTION pin.")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["real", "fake", "dual"])
    p.add_argument("--pin", type=int, default=0,
                   help="MCP2221A GP number for real mode (0..3). Default 0 (G0).")
    p.add_argument("--pin-a", type=int, default=0,
                   help="First pin for dual mode. Default 0 (G0).")
    p.add_argument("--pin-b", type=int, default=1,
                   help="Second pin for dual mode. Default 1 (G1).")
    p.add_argument("--timeout", type=float, default=3.0,
                   help="Seconds of no motion before STOPPED. Default 3.")
    p.add_argument("--duration", type=float, default=10.0,
                   help="How long the test runs. Default 10s.")
    args = p.parse_args()

    if args.mode == "real":
        return run_real(args.pin, args.timeout, args.duration)
    elif args.mode == "dual":
        return run_dual(args.pin_a, args.pin_b, args.duration)
    else:
        return run_fake(args.timeout, args.duration)


if __name__ == "__main__":
    sys.exit(main())
