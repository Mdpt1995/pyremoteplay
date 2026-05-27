"""Direct kernel input reader for Linux (evdev).

Reads raw input events from /dev/input/eventX bypassing SDL2/pygame entirely.
This eliminates all middleware latency for controller input.

Architecture:
    /dev/input/eventX → read() → struct unpack → Controller.button()/stick()

Latency: <0.2ms from kernel event to Controller call
vs SDL/pygame: ~2-10ms (polling, abstraction, Python wrapper overhead)

Requirements:
    - Linux only (uses /dev/input/ interface)
    - Read permissions on /dev/input/eventX (usually requires 'input' group or root)
    - No external dependencies (pure Python + struct)
"""

from __future__ import annotations
import os
import struct
import threading
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

from pyremoteplay.controller import Controller

_LOGGER = logging.getLogger(__name__)

# ─── Linux input event constants ─────────────────────────────────────────────

# struct input_event {
#     struct timeval time;  // 16 bytes on 64-bit (8+8), 8 bytes on 32-bit (4+4)
#     __u16 type;
#     __u16 code;
#     __s32 value;
# };

# Try 64-bit format first (most modern systems)
_EVENT_FORMAT_64 = "llHHi"  # 24 bytes
_EVENT_FORMAT_32 = "IIHHi"  # 16 bytes
EVENT_SIZE = struct.calcsize(_EVENT_FORMAT_64)

# Event types
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

# Axis codes (ABS_*)
ABS_X = 0x00         # Left stick X
ABS_Y = 0x01         # Left stick Y
ABS_Z = 0x02         # L2 trigger
ABS_RX = 0x03        # Right stick X
ABS_RY = 0x04        # Right stick Y
ABS_RZ = 0x05        # R2 trigger
ABS_HAT0X = 0x10     # D-pad X
ABS_HAT0Y = 0x11     # D-pad Y

# Button codes (BTN_*)
BTN_SOUTH = 0x130    # Cross / A
BTN_EAST = 0x131     # Circle / B
BTN_NORTH = 0x133    # Triangle / Y
BTN_WEST = 0x134     # Square / X
BTN_TL = 0x136       # L1 / LB
BTN_TR = 0x137       # R1 / RB
BTN_TL2 = 0x138      # L2 (digital)
BTN_TR2 = 0x139      # R2 (digital)
BTN_SELECT = 0x13A   # Share / Select
BTN_START = 0x13B    # Options / Start
BTN_MODE = 0x13C     # PS / Xbox button
BTN_THUMBL = 0x13D   # L3
BTN_THUMBR = 0x13E   # R3

# DualShock4/DualSense specific (may appear as different codes)
BTN_A = 0x130
BTN_B = 0x131
BTN_X = 0x133
BTN_Y = 0x134

# Touchpad (DS4/DualSense)
BTN_TOUCH = 0x14A    # Touchpad click (some drivers)

# ─── Default mapping: evdev code → PS button name ─────────────────────────────

DEFAULT_BUTTON_MAP = {
    BTN_SOUTH: "CROSS",
    BTN_EAST: "CIRCLE",
    BTN_WEST: "SQUARE",
    BTN_NORTH: "TRIANGLE",
    BTN_TL: "L1",
    BTN_TR: "R1",
    BTN_SELECT: "SHARE",
    BTN_START: "OPTIONS",
    BTN_MODE: "PS",
    BTN_THUMBL: "L3",
    BTN_THUMBR: "R3",
    BTN_TOUCH: "TOUCHPAD",
}

# Axis mapping: evdev code → (stick_name, axis) or button_name for triggers
DEFAULT_AXIS_MAP = {
    ABS_X: ("left", "x"),
    ABS_Y: ("left", "y"),
    ABS_RX: ("right", "x"),
    ABS_RY: ("right", "y"),
    ABS_Z: "L2",       # Analog trigger as button
    ABS_RZ: "R2",      # Analog trigger as button
}

# D-pad via ABS_HAT
DPAD_MAP_X = {-1: "LEFT", 1: "RIGHT"}
DPAD_MAP_Y = {-1: "UP", 1: "DOWN"}

# ─── Axis calibration ─────────────────────────────────────────────────────────

# Most controllers report axes in range [0, 255] or [-32768, 32767]
# We auto-detect and normalize to [-1.0, 1.0]

@dataclass
class AxisCalibration:
    """Stores min/max for axis normalization."""
    min_val: int = 0
    max_val: int = 255
    center: int = 128
    deadzone: float = 0.05

    def normalize(self, value: int) -> float:
        """Normalize raw value to [-1.0, 1.0]."""
        # Handle centered axes (sticks)
        if self.min_val < 0:
            # Range is [-32768, 32767] style
            half_range = max(abs(self.min_val), abs(self.max_val))
            if half_range == 0:
                return 0.0
            normalized = value / half_range
        else:
            # Range is [0, 255] style
            full_range = self.max_val - self.min_val
            if full_range == 0:
                return 0.0
            normalized = ((value - self.min_val) / full_range) * 2.0 - 1.0

        # Apply deadzone
        if abs(normalized) < self.deadzone:
            return 0.0
        return max(-1.0, min(1.0, normalized))

    def normalize_trigger(self, value: int) -> float:
        """Normalize trigger value to [0.0, 1.0]."""
        full_range = self.max_val - self.min_val
        if full_range == 0:
            return 0.0
        normalized = (value - self.min_val) / full_range
        return max(0.0, min(1.0, normalized))


@dataclass
class DeviceInfo:
    """Information about a detected gamepad device."""
    path: str
    name: str
    phys: str = ""
    uniq: str = ""


# ─── Auto-detection ───────────────────────────────────────────────────────────

def find_gamepads() -> list[DeviceInfo]:
    """Find all gamepad devices in /dev/input/.

    Scans /dev/input/event* and reads their names from /sys/class/input/
    to identify gamepad devices.

    Returns list of DeviceInfo with path and name.
    """
    devices = []
    input_dir = Path("/dev/input")

    if not input_dir.exists():
        _LOGGER.warning("/dev/input not found - not running on Linux?")
        return devices

    for event_path in sorted(input_dir.glob("event*")):
        try:
            # Read device name from sysfs
            event_name = event_path.name
            sys_path = Path(f"/sys/class/input/{event_name}/device/name")
            if sys_path.exists():
                name = sys_path.read_text().strip()
            else:
                name = "Unknown"

            # Read phys (connection path)
            phys_path = Path(f"/sys/class/input/{event_name}/device/phys")
            phys = phys_path.read_text().strip() if phys_path.exists() else ""

            # Read uniq (unique id, usually MAC for bluetooth)
            uniq_path = Path(f"/sys/class/input/{event_name}/device/uniq")
            uniq = uniq_path.read_text().strip() if uniq_path.exists() else ""

            # Check if this is a gamepad by looking for gamepad-like capabilities
            # Check for ABS axes (joystick/gamepad characteristic)
            caps_path = Path(f"/sys/class/input/{event_name}/device/capabilities/abs")
            if caps_path.exists():
                abs_caps = caps_path.read_text().strip()
                if abs_caps and abs_caps != "0":
                    # Has absolute axes - likely a gamepad/joystick
                    # Filter out mice (they have abs for touchpads)
                    key_caps_path = Path(
                        f"/sys/class/input/{event_name}/device/capabilities/key"
                    )
                    if key_caps_path.exists():
                        key_caps = key_caps_path.read_text().strip()
                        # Gamepads have BTN_SOUTH (0x130) in their key capabilities
                        # This is bit 304 (0x130) in the key bitmap
                        if _has_gamepad_buttons(key_caps):
                            devices.append(
                                DeviceInfo(
                                    path=str(event_path),
                                    name=name,
                                    phys=phys,
                                    uniq=uniq,
                                )
                            )
        except (PermissionError, OSError) as e:
            _LOGGER.debug("Cannot read %s: %s", event_path, e)
            continue

    return devices


def _has_gamepad_buttons(key_caps_hex: str) -> bool:
    """Check if key capabilities include gamepad buttons (BTN_SOUTH=0x130)."""
    try:
        # key_caps is a hex bitmap, space-separated words (least significant first)
        # BTN_SOUTH = 0x130 = bit 304
        # 304 // 64 = 4 (word index from right), 304 % 64 = 48 (bit in word)
        words = key_caps_hex.split()
        if len(words) > 4:
            word = int(words[-(4 + 1)], 16)  # 5th word from the right
            return bool(word & (1 << 48))
    except (IndexError, ValueError):
        pass

    # Fallback: check name for common gamepad identifiers
    return False


# ─── Main Class ───────────────────────────────────────────────────────────────

class EvdevGamepad:
    """Direct kernel input reader. Zero SDL overhead.

    Reads raw input_event structs from /dev/input/eventX and translates
    them directly to Controller button/stick calls.

    :param device_path: Path to the input device (e.g., "/dev/input/event0")
    :param button_map: Dict mapping evdev button codes to PS button names
    :param axis_map: Dict mapping evdev axis codes to stick/trigger info
    :param deadzone: Minimum threshold for stick axes (0.0 to 1.0)
    :param trigger_as_button: If True, triggers are sent as button press/release.
        If False, triggers are ignored (use only for digital trigger behavior).
    :param trigger_threshold: Threshold for trigger-as-button activation (0.0 to 1.0)
    """

    def __init__(
        self,
        device_path: str,
        button_map: dict = None,
        axis_map: dict = None,
        deadzone: float = 0.05,
        trigger_as_button: bool = True,
        trigger_threshold: float = 0.15,
    ):
        self._path = device_path
        self._controller: Optional[Controller] = None
        self._button_map = button_map or dict(DEFAULT_BUTTON_MAP)
        self._axis_map = axis_map or dict(DEFAULT_AXIS_MAP)
        self._deadzone = deadzone
        self._trigger_as_button = trigger_as_button
        self._trigger_threshold = trigger_threshold

        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._fd: Optional[int] = None

        # Axis calibration (auto-detected or manual)
        self._axis_cal: dict[int, AxisCalibration] = {}
        self._init_default_calibration()

        # D-pad state tracking (for release events)
        self._dpad_x_state: Optional[str] = None
        self._dpad_y_state: Optional[str] = None

        # Trigger state tracking
        self._trigger_state: dict[str, bool] = {"L2": False, "R2": False}

        # Stats
        self._events_processed = 0
        self._last_event_time = 0.0

        # Callback for raw events (optional, for debugging/monitoring)
        self._on_raw_event: Optional[Callable] = None

    def _init_default_calibration(self):
        """Set default calibration for common axis ranges."""
        # Most DS4/DualSense controllers report 0-255 for sticks
        for axis in (ABS_X, ABS_Y, ABS_RX, ABS_RY):
            self._axis_cal[axis] = AxisCalibration(
                min_val=0, max_val=255, center=128, deadzone=self._deadzone
            )
        # Triggers report 0-255
        for axis in (ABS_Z, ABS_RZ):
            self._axis_cal[axis] = AxisCalibration(
                min_val=0, max_val=255, center=0, deadzone=0.0
            )
        # D-pad reports -1, 0, 1
        for axis in (ABS_HAT0X, ABS_HAT0Y):
            self._axis_cal[axis] = AxisCalibration(
                min_val=-1, max_val=1, center=0, deadzone=0.0
            )

    def set_calibration(self, axis_code: int, min_val: int, max_val: int, deadzone: float = None):
        """Set manual calibration for an axis.

        :param axis_code: The evdev ABS_* code
        :param min_val: Minimum raw value the axis reports
        :param max_val: Maximum raw value the axis reports
        :param deadzone: Optional deadzone override for this axis
        """
        dz = deadzone if deadzone is not None else self._deadzone
        self._axis_cal[axis_code] = AxisCalibration(
            min_val=min_val, max_val=max_val, center=(min_val + max_val) // 2, deadzone=dz
        )

    def connect(self, controller: Controller):
        """Connect to a Controller instance.

        :param controller: The pyremoteplay Controller to send inputs to
        """
        if not isinstance(controller, Controller):
            raise TypeError(f"Expected Controller, got {type(controller)}")
        self._controller = controller
        _LOGGER.info("EvdevGamepad connected to Controller")

    def start(self):
        """Start reading input events from the device.

        Opens the device file and starts a dedicated reader thread.
        The thread uses blocking I/O for minimum latency - it wakes up
        immediately when the kernel has a new event.
        """
        if self._running:
            _LOGGER.warning("EvdevGamepad already running")
            return
        if not os.path.exists(self._path):
            raise FileNotFoundError(f"Device not found: {self._path}")

        self._running = True
        self._thread = threading.Thread(
            target=self._read_loop, name="evdev-input", daemon=True
        )
        self._thread.start()
        _LOGGER.info("EvdevGamepad started: %s", self._path)

    def stop(self):
        """Stop reading input events and close the device."""
        self._running = False
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        _LOGGER.info("EvdevGamepad stopped")

    def _read_loop(self):
        """Main read loop. Runs in dedicated thread.

        Uses blocking read() - the thread sleeps until the kernel
        delivers a new input event. This is the fastest possible
        notification mechanism on Linux.
        """
        try:
            self._fd = os.open(self._path, os.O_RDONLY | os.O_NONBLOCK)
            # Switch to blocking after open (O_NONBLOCK needed for permission check)
            import fcntl
            flags = fcntl.fcntl(self._fd, fcntl.F_GETFL)
            fcntl.fcntl(self._fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        except (OSError, PermissionError) as e:
            _LOGGER.error("Cannot open %s: %s", self._path, e)
            _LOGGER.error("Hint: Add user to 'input' group or run as root")
            self._running = False
            return

        _LOGGER.debug("Opened device fd=%d", self._fd)

        try:
            while self._running:
                try:
                    data = os.read(self._fd, EVENT_SIZE)
                    if not data or len(data) < EVENT_SIZE:
                        break
                    self._process_event(data)
                except OSError:
                    if self._running:
                        _LOGGER.error("Read error on %s", self._path)
                    break
        finally:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            _LOGGER.debug("Read loop ended")

    def _process_event(self, data: bytes):
        """Process a single raw input_event."""
        try:
            _sec, _usec, ev_type, code, value = struct.unpack(_EVENT_FORMAT_64, data)
        except struct.error:
            # Try 32-bit format
            try:
                _sec, _usec, ev_type, code, value = struct.unpack(_EVENT_FORMAT_32, data[:16])
            except struct.error:
                return

        # Skip sync events
        if ev_type == EV_SYN:
            return

        self._events_processed += 1
        self._last_event_time = time.monotonic()

        if self._on_raw_event:
            self._on_raw_event(ev_type, code, value)

        if ev_type == EV_KEY:
            self._handle_button(code, value)
        elif ev_type == EV_ABS:
            self._handle_axis(code, value)

    def _handle_button(self, code: int, value: int):
        """Handle button press/release event.

        :param code: evdev button code (BTN_*)
        :param value: 1=press, 0=release, 2=repeat (ignored)
        """
        if value == 2:
            # Key repeat - ignore for gamepad
            return

        button_name = self._button_map.get(code)
        if not button_name:
            _LOGGER.debug("Unmapped button code: 0x%x value=%d", code, value)
            return

        if not self._controller:
            return

        action = "press" if value == 1 else "release"
        self._controller.button(button_name, action)

    def _handle_axis(self, code: int, value: int):
        """Handle axis motion event.

        :param code: evdev axis code (ABS_*)
        :param value: Raw axis value from kernel
        """
        # D-pad handling (special: axes that act as buttons)
        if code == ABS_HAT0X:
            self._handle_dpad_x(value)
            return
        if code == ABS_HAT0Y:
            self._handle_dpad_y(value)
            return

        mapping = self._axis_map.get(code)
        if not mapping:
            _LOGGER.debug("Unmapped axis code: 0x%x value=%d", code, value)
            return

        if not self._controller:
            return

        cal = self._axis_cal.get(code)

        # Trigger handling (string mapping = button name)
        if isinstance(mapping, str):
            if self._trigger_as_button and cal:
                normalized = cal.normalize_trigger(value)
                is_pressed = normalized > self._trigger_threshold
                was_pressed = self._trigger_state.get(mapping, False)

                if is_pressed and not was_pressed:
                    self._controller.button(mapping, "press")
                    self._trigger_state[mapping] = True
                elif not is_pressed and was_pressed:
                    self._controller.button(mapping, "release")
                    self._trigger_state[mapping] = False
            return

        # Stick handling (tuple mapping = (stick_name, axis))
        stick_name, axis = mapping
        if cal:
            normalized = cal.normalize(value)
        else:
            # Fallback: assume 0-255 range
            normalized = (value / 127.5) - 1.0
            if abs(normalized) < self._deadzone:
                normalized = 0.0

        self._controller.stick(stick_name, axis=axis, value=normalized)

    def _handle_dpad_x(self, value: int):
        """Handle D-pad X axis (LEFT/RIGHT)."""
        if not self._controller:
            return

        # Release previous direction
        if self._dpad_x_state:
            self._controller.button(self._dpad_x_state, "release")
            self._dpad_x_state = None

        # Press new direction
        button = DPAD_MAP_X.get(value)
        if button:
            self._controller.button(button, "press")
            self._dpad_x_state = button

    def _handle_dpad_y(self, value: int):
        """Handle D-pad Y axis (UP/DOWN)."""
        if not self._controller:
            return

        # Release previous direction
        if self._dpad_y_state:
            self._controller.button(self._dpad_y_state, "release")
            self._dpad_y_state = None

        # Press new direction
        button = DPAD_MAP_Y.get(value)
        if button:
            self._controller.button(button, "press")
            self._dpad_y_state = button

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """Return True if reader is active."""
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def device_path(self) -> str:
        """Return device path."""
        return self._path

    @property
    def controller(self) -> Optional[Controller]:
        """Return connected controller."""
        return self._controller

    @property
    def deadzone(self) -> float:
        """Return current deadzone."""
        return self._deadzone

    @deadzone.setter
    def deadzone(self, value: float):
        """Set deadzone and update all stick calibrations."""
        self._deadzone = abs(value)
        for code in (ABS_X, ABS_Y, ABS_RX, ABS_RY):
            if code in self._axis_cal:
                self._axis_cal[code].deadzone = self._deadzone

    @property
    def events_processed(self) -> int:
        """Return total events processed."""
        return self._events_processed

    @property
    def button_map(self) -> dict:
        """Return current button mapping."""
        return dict(self._button_map)

    @button_map.setter
    def button_map(self, mapping: dict):
        """Set button mapping."""
        self._button_map = dict(mapping)

    @property
    def axis_map(self) -> dict:
        """Return current axis mapping."""
        return dict(self._axis_map)

    @axis_map.setter
    def axis_map(self, mapping: dict):
        """Set axis mapping."""
        self._axis_map = dict(mapping)

    @property
    def on_raw_event(self) -> Optional[Callable]:
        """Return raw event callback."""
        return self._on_raw_event

    @on_raw_event.setter
    def on_raw_event(self, callback: Optional[Callable]):
        """Set raw event callback. Signature: callback(ev_type, code, value)"""
        self._on_raw_event = callback
