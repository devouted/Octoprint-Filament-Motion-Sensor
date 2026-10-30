# coding=utf-8
#
# Filament motion sensor reader for an MCP2221A USB-to-GPIO adapter
# accessed through Adafruit Blinka (board / digitalio).
#
# Unlike the original Raspberry Pi version (which used libgpiod hardware
# edge-detection interrupts), the MCP2221A is polled over USB: there is no
# interrupt line available through Blinka, so we read the pin value in a loop
# and detect state transitions (0->1->0...) in software.
#
# For filament-jam detection this is more than enough: the sensor toggles its
# output while the filament moves, so any observed transition means "moving",
# and the absence of any transition for `max_not_moving_time` means "stopped".

import threading
import time

# Blinka must know which board to emulate. Setting BLINKA_MCP2221 before
# importing `board` makes Adafruit Blinka target the MCP2221A USB adapter.
import os
os.environ.setdefault("BLINKA_MCP2221", "1")
# Avoid Blinka attempting an initial reset handshake that can slow down import
# or fail on some hosts; the adapter is already enumerated by the OS.
os.environ.setdefault("BLINKA_MCP2221_RESET_DELAY", "-1")

# Blinka is imported defensively: if it is not installed, or no MCP2221A is
# attached, importing `board` raises. We must not let that crash the whole
# plugin import — __plugin_check__ / plugin_check_mcp2221() handle the
# "hardware unavailable" case gracefully instead.
try:
    import board
    import digitalio
    _BLINKA_AVAILABLE = True
    _BLINKA_IMPORT_ERROR = None
except Exception as _ex:  # ImportError, or Blinka's own runtime errors
    board = None
    digitalio = None
    _BLINKA_AVAILABLE = False
    _BLINKA_IMPORT_ERROR = _ex


# Default polling interval. Each read is a USB HID transaction (~a few ms),
# so ~5 ms gives roughly 100-200 Hz effective sampling which is plenty to
# catch a filament-motion toggle while keeping USB/CPU load low.
POLL_INTERVAL_SECONDS = 0.005


def resolve_board_pin(pin_name):
    """
    Translate a user-facing pin name (e.g. "G0", "G1", "G2", "G3") into the
    corresponding `board` attribute exposed by Blinka for the MCP2221A.

    Returns the board pin object, or raises AttributeError if the name is
    unknown for the detected board.
    """
    if not _BLINKA_AVAILABLE:
        raise RuntimeError("Adafruit Blinka not available: %s" % _BLINKA_IMPORT_ERROR)
    name = str(pin_name).strip().upper()
    return getattr(board, name)


def plugin_check_mcp2221():
    """
    Verify that an MCP2221A is reachable through Blinka.

    Returns True if a digital pin can be opened, False otherwise. This is used
    by the OctoPrint plugin's __plugin_check__ to decide whether the hardware
    backend is available.
    """
    if not _BLINKA_AVAILABLE:
        return False
    try:
        pin = resolve_board_pin("G0")
        dio = digitalio.DigitalInOut(pin)
        dio.direction = digitalio.Direction.INPUT
        dio.deinit()
        return True
    except Exception:
        return False


class MotionSensorUSBThread(threading.Thread):
    """
    Polls a single MCP2221A GPIO pin through Blinka and reports filament motion
    via the supplied callback.

    The callback contract matches the original libgpiod implementation:
      - callback(True)  -> motion detected (pin state changed since last read)
      - callback(False) -> no motion for longer than `max_not_moving_time`
    """

    def __init__(self, threadID, threadName, pUsedPin, pMaxNotMovingTime,
                 pLogger, pData, pCallback=None):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = threadName
        self.callback = pCallback
        self._logger = pLogger
        self._data = pData
        self.max_not_moving_time = pMaxNotMovingTime
        self.keepRunning = True

        # pUsedPin is the MCP2221A pin name ("G0".."G3"). The original plugin
        # passed a BCM pin number; here we accept either a name or a number and
        # normalise to a "G<n>" name.
        self.pin_name = self._normalise_pin_name(pUsedPin)
        self._dio = None
        self._data.last_motion_detected = time.time()

    @staticmethod
    def _normalise_pin_name(pin):
        """Accept 'G0', 'g1', 0, '2' etc. and return a 'G<n>' style name."""
        s = str(pin).strip().upper()
        if s.startswith("G"):
            return s
        # Plain number -> GPIO name on the MCP2221A
        return "G" + s

    def _open_pin(self):
        board_pin = resolve_board_pin(self.pin_name)
        dio = digitalio.DigitalInOut(board_pin)
        dio.direction = digitalio.Direction.INPUT
        # MCP2221A GP pins have no configurable internal pull via Blinka's
        # digitalio on all firmware revisions; the slotted-wheel / BTT sensors
        # drive the line actively, so a plain input is sufficient. If your
        # sensor needs a pull-up, add it externally.
        self._dio = dio
        return dio

    # Override run method of threading
    def run(self):
        try:
            dio = self._open_pin()
        except Exception as ex:
            self._logger.error(
                "Could not open MCP2221A pin '%s': %s" % (self.pin_name, ex))
            self.used_pin = -1
            return

        self._logger.info(
            "MCP2221A motion sensor polling started on pin %s (thread %s)"
            % (self.pin_name, self.name))

        self._data.last_motion_detected = time.time()
        last_value = None

        # Diagnostics: count transitions per ~1s window and log them so we can
        # see, live, how much movement the thread actually detects.
        _diag_transitions = 0
        _diag_window_start = time.time()
        _diag_reads = 0

        try:
            while self.keepRunning:
                try:
                    value = dio.value
                except Exception as ex:
                    # A transient USB read error should not kill the thread;
                    # log once at debug level and retry after a short pause.
                    self._logger.debug("MCP2221A read error: %s" % ex)
                    time.sleep(0.05)
                    continue

                now = time.time()
                _diag_reads += 1

                # Detect a state transition -> filament moved.
                if last_value is not None and value != last_value:
                    self._data.last_motion_detected = now
                    _diag_transitions += 1
                    if self.callback is not None:
                        self.callback(True)
                last_value = value

                # No transition for longer than the allowed window -> stopped.
                timespan = now - self._data.last_motion_detected
                if timespan > self.max_not_moving_time:
                    if self.callback is not None:
                        self.callback(False)

                # Emit a diagnostic line roughly once per second.
                if (now - _diag_window_start) >= 1.0:
                    self._logger.debug(
                        "[motion-diag] pin %s: %d transitions, %d reads in %.2fs "
                        "(last_value=%s, since_last_motion=%.2fs)"
                        % (self.pin_name, _diag_transitions, _diag_reads,
                           now - _diag_window_start, last_value, timespan))
                    _diag_transitions = 0
                    _diag_reads = 0
                    _diag_window_start = now

                time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            try:
                if self._dio is not None:
                    self._dio.deinit()
            except Exception:
                pass
            self._logger.info(
                "MCP2221A motion sensor polling stopped on pin %s"
                % self.pin_name)


class RunoutSensorUSBThread(threading.Thread):
    """
    Polls a single MCP2221A GPIO pin connected to a filament *runout switch*
    (e.g. the SWITCH signal of a BIGTREETECH SFS V2.0) and reports a
    filament-out condition via the supplied callback.

    Unlike the motion sensor (which looks for level *transitions*), the runout
    switch is a steady level: filament present -> one level, filament absent ->
    the other. Per the BTT SFS V2.0 manual, the switch output is LOW when no
    filament is detected (with a pull-up on the line). That default is captured
    by `triggered_low=True`, but it is configurable in case of inverted wiring.

    A debounce window avoids pausing on brief contact chatter: the "absent"
    level must persist for `debounce_seconds` before the callback fires. The
    callback is called once per edge:
      - callback(False) -> filament ABSENT (runout) — caller should pause
      - callback(True)  -> filament PRESENT again (cleared)
    """

    def __init__(self, threadID, threadName, pUsedPin, pLogger, pData,
                 pCallback=None, triggered_low=True, debounce_seconds=0.2):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = threadName
        self.callback = pCallback
        self._logger = pLogger
        self._data = pData
        self.keepRunning = True

        self.triggered_low = bool(triggered_low)
        self.debounce_seconds = float(debounce_seconds)

        self.pin_name = MotionSensorUSBThread._normalise_pin_name(pUsedPin)
        self._dio = None

    def _open_pin(self):
        board_pin = resolve_board_pin(self.pin_name)
        dio = digitalio.DigitalInOut(board_pin)
        dio.direction = digitalio.Direction.INPUT
        # The MCP2221A exposes no usable internal pull through Blinka's
        # digitalio; the SFS V2.0 and similar switches are used with an
        # external pull-up resistor, so a plain input reads the real level.
        self._dio = dio
        return dio

    def _is_filament_present(self, value):
        """Map a raw pin level to 'filament present' according to polarity."""
        # triggered_low=True  -> absent when LOW  -> present when value is True
        # triggered_low=False -> absent when HIGH -> present when value is False
        if self.triggered_low:
            return bool(value)
        return not bool(value)

    def run(self):
        try:
            dio = self._open_pin()
        except Exception as ex:
            self._logger.error(
                "Could not open MCP2221A runout pin '%s': %s"
                % (self.pin_name, ex))
            return

        self._logger.info(
            "MCP2221A runout switch polling started on pin %s "
            "(triggered_low=%s, debounce=%.2fs)"
            % (self.pin_name, self.triggered_low, self.debounce_seconds))

        # Debounced state machine.
        reported_present = None          # last state we told the caller about
        candidate_present = None         # level we're currently debouncing
        candidate_since = 0.0

        try:
            while self.keepRunning:
                try:
                    raw = dio.value
                except Exception as ex:
                    self._logger.debug("MCP2221A runout read error: %s" % ex)
                    time.sleep(0.05)
                    continue

                present = self._is_filament_present(raw)
                now = time.time()

                if present != candidate_present:
                    # Level changed; start/refresh the debounce timer.
                    candidate_present = present
                    candidate_since = now
                elif (reported_present != candidate_present and
                      (now - candidate_since) >= self.debounce_seconds):
                    # Stable for long enough -> commit and notify.
                    reported_present = candidate_present
                    if self.callback is not None:
                        self.callback(reported_present)
                    self._logger.info(
                        "Runout switch: filament %s"
                        % ("PRESENT" if reported_present else "ABSENT"))

                time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            try:
                if self._dio is not None:
                    self._dio.deinit()
            except Exception:
                pass
            self._logger.info(
                "MCP2221A runout switch polling stopped on pin %s"
                % self.pin_name)


class CombinedSensorUSBThread(threading.Thread):
    """
    Reads BOTH the motion pin and the (optional) runout-switch pin of an
    MCP2221A from a SINGLE Blinka connection, in one polling loop.

    This exists because the MCP2221A only allows a single open HID session at
    a time. Running two independent threads (one per pin) made the second one
    fail with "MCP2221 HID session is out of sync", silently disabling one
    sensor. Reading both pins in one loop avoids that entirely.

    Callback contracts (unchanged from the single-pin classes):
      - motion_callback(True)  -> motion detected (motion pin changed state)
        motion_callback(False) -> no motion for longer than max_not_moving_time
      - runout_callback(True)  -> filament PRESENT
        runout_callback(False) -> filament ABSENT (runout) [debounced]

    Either pin can be disabled by passing a negative pin number; its callback
    is then never called and that pin is not opened.
    """

    def __init__(self, threadID, threadName, pLogger, pData,
                 motion_pin=-1, motion_callback=None, max_not_moving_time=1,
                 runout_pin=-1, runout_callback=None,
                 runout_triggered_low=True, runout_debounce_seconds=0.2):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = threadName
        self._logger = pLogger
        self._data = pData
        self.keepRunning = True

        self.motion_enabled = (motion_pin is not None and int(motion_pin) >= 0)
        self.runout_enabled = (runout_pin is not None and int(runout_pin) >= 0)

        self.motion_pin_name = (
            MotionSensorUSBThread._normalise_pin_name(motion_pin)
            if self.motion_enabled else None)
        self.runout_pin_name = (
            MotionSensorUSBThread._normalise_pin_name(runout_pin)
            if self.runout_enabled else None)

        self.motion_callback = motion_callback
        self.max_not_moving_time = max_not_moving_time
        self.runout_callback = runout_callback
        self.runout_triggered_low = bool(runout_triggered_low)
        self.runout_debounce_seconds = float(runout_debounce_seconds)

        self._motion_dio = None
        self._runout_dio = None

    def _open_input(self, pin_name):
        board_pin = resolve_board_pin(pin_name)
        dio = digitalio.DigitalInOut(board_pin)
        dio.direction = digitalio.Direction.INPUT
        return dio

    def _runout_is_present(self, value):
        if self.runout_triggered_low:
            return bool(value)
        return not bool(value)

    def run(self):
        # Open whichever pins are enabled. Both share the one MCP2221A HID
        # session that Blinka manages internally, so there is no second open.
        try:
            if self.motion_enabled:
                self._motion_dio = self._open_input(self.motion_pin_name)
            if self.runout_enabled:
                self._runout_dio = self._open_input(self.runout_pin_name)
        except Exception as ex:
            self._logger.error(
                "Could not open MCP2221A pins (motion=%s, runout=%s): %s"
                % (self.motion_pin_name, self.runout_pin_name, ex))
            self._cleanup()
            return

        self._logger.info(
            "MCP2221A combined sensor polling started "
            "(motion=%s, runout=%s, runout_triggered_low=%s)"
            % (self.motion_pin_name, self.runout_pin_name,
               self.runout_triggered_low))

        now = time.time()
        self._data.last_motion_detected = now

        # Motion state
        motion_last_value = None
        # Runout debounce state
        runout_reported_present = None
        runout_candidate_present = None
        runout_candidate_since = now

        # Diagnostics
        _diag_motion_transitions = 0
        _diag_reads = 0
        _diag_window_start = now

        try:
            while self.keepRunning:
                now = time.time()
                _diag_reads += 1

                # --- Motion pin ---
                if self.motion_enabled:
                    try:
                        mval = self._motion_dio.value
                    except Exception as ex:
                        self._logger.debug("MCP2221A motion read error: %s" % ex)
                        time.sleep(0.05)
                        continue
                    if motion_last_value is not None and mval != motion_last_value:
                        self._data.last_motion_detected = now
                        _diag_motion_transitions += 1
                        if self.motion_callback is not None:
                            self.motion_callback(True)
                    motion_last_value = mval

                    if (now - self._data.last_motion_detected) > self.max_not_moving_time:
                        if self.motion_callback is not None:
                            self.motion_callback(False)

                # --- Runout pin ---
                if self.runout_enabled:
                    try:
                        rval = self._runout_dio.value
                    except Exception as ex:
                        self._logger.debug("MCP2221A runout read error: %s" % ex)
                        time.sleep(0.05)
                        continue
                    present = self._runout_is_present(rval)
                    if present != runout_candidate_present:
                        runout_candidate_present = present
                        runout_candidate_since = now
                    elif (runout_reported_present != runout_candidate_present and
                          (now - runout_candidate_since) >= self.runout_debounce_seconds):
                        runout_reported_present = runout_candidate_present
                        if self.runout_callback is not None:
                            self.runout_callback(runout_reported_present)
                        self._logger.info(
                            "Runout switch: filament %s"
                            % ("PRESENT" if runout_reported_present else "ABSENT"))

                # --- Diagnostics once per second ---
                # Diagnostic line at most once per second, and only when there
                # was actual movement, so the log is not flooded when idle.
                if (now - _diag_window_start) >= 1.0:
                    if _diag_motion_transitions > 0:
                        self._logger.debug(
                            "[sensor-diag] motion(%s): %d transitions, %d reads in %.2fs"
                            % (self.motion_pin_name, _diag_motion_transitions,
                               _diag_reads, now - _diag_window_start))
                    _diag_motion_transitions = 0
                    _diag_reads = 0
                    _diag_window_start = now

                time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            self._cleanup()
            self._logger.info("MCP2221A combined sensor polling stopped")

    def _cleanup(self):
        for dio in (self._motion_dio, self._runout_dio):
            try:
                if dio is not None:
                    dio.deinit()
            except Exception:
                pass
        self._motion_dio = None
        self._runout_dio = None
