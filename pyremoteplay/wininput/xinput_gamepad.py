"""Windows XInput direct reader via ctypes.

Reads controller state directly from xinput1_4.dll without any middleware.
Supports polling rates up to 1000Hz for DS4 controllers with HIDUSBF driver.

Architecture:
    [Controller USB] → [xinput1_4.dll] → [ctypes] → Controller.button()/stick()

Latency breakdown:
    - XInputGetState call: ~0.1-0.3ms
    - Python ctypes overhead: ~0.01ms
    - Polling interval at 1000Hz: 1ms between reads
    - Total: ~1ms per input cycle at 1000Hz

For DS4 at 1000Hz:
    - Install HIDUSBF driver to overclock DS4 USB polling to 1000Hz
    - Use DS4Windows to expose DS4 as XInput device
    - Set poll_rate=1000 in XInputGamepad

Requirements:
    - Windows 7+ (xinput1_4.dll ships with Windows)
    - For DS4: DS4Windows or Steam (to emulate XInput)
    - For 1000Hz: HIDUSBF driver (overclock USB polling rate)
    - No external Python dependencies (pure ctypes)
"""

from __future__ import annotations
import ctypes
import ctypes.wintypes
import threading
import logging
import time
from dataclasses import dataclass
from typing import Optional, Callable

from pyremoteplay.controller import Controller

_LOGGER = logging.getLogger(__name__)

# ─── XInput Constants ─────────────────────────────────────────────────────────

XINPUT_MAX_CONTROLLERS = 4

# XInput button bitmasks
XINPUT_GAMEPAD_DPAD_UP = 0x0001
XINPUT_GAMEPAD_DPAD_DOWN = 0x0002
XINPUT_GAMEPAD_DPAD_LEFT = 0x0004
XINPUT_GAMEPAD_DPAD_RIGHT = 0x0008
XINPUT_GAMEPAD_START = 0x0010
XINPUT_GAMEPAD_BACK = 0x0020
XINPUT_GAMEPAD_LEFT_THUMB = 0x0040
XINPUT_GAMEPAD_RIGHT_THUMB = 0x0080
XINPUT_GAMEPAD_LEFT_SHOULDER = 0x0100
XINPUT_GAMEPAD_RIGHT_SHOULDER = 0x0200
XINPUT_GAMEPAD_GUIDE = 0x0400
XINPUT_GAMEPAD_A = 0x1000
XINPUT_GAMEPAD_B = 0x2000
XINPUT_GAMEPAD_X = 0x4000
XINPUT_GAMEPAD_Y = 0x8000

# Stick range
XINPUT_STICK_MAX = 32767
XINPUT_STICK_MIN = -32768

# Trigger range
XINPUT_TRIGGER_MAX = 255

# Error codes
ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167

# ─── Button mapping: XInput bitmask → PS button name ──────────────────────────

DEFAULT_BUTTON_MAP = {
    XINPUT_GAMEPAD_A: "CROSS",
    XINPUT_GAMEPAD_B: "CIRCLE",
    XINPUT_GAMEPAD_X: "SQUARE",
    XINPUT_GAMEPAD_Y: "TRIANGLE",
    XINPUT_GAMEPAD_LEFT_SHOULDER: "L1",
    XINPUT_GAMEPAD_RIGHT_SHOULDER: "R1",
    XINPUT_GAMEPAD_BACK: "SHARE",
    XINPUT_GAMEPAD_START: "OPTIONS",
    XINPUT_GAMEPAD_GUIDE: "PS",
    XINPUT_GAMEPAD_LEFT_THUMB: "L3",
    XINPUT_GAMEPAD_RIGHT_THUMB: "R3",
    XINPUT_GAMEPAD_DPAD_UP: "UP",
    XINPUT_GAMEPAD_DPAD_DOWN: "DOWN",
    XINPUT_GAMEPAD_DPAD_LEFT: "LEFT",
    XINPUT_GAMEPAD_DPAD_RIGHT: "RIGHT",
}

# All button bitmasks for iteration
ALL_BUTTONS = (
    XINPUT_GAMEPAD_DPAD_UP,
    XINPUT_GAMEPAD_DPAD_DOWN,
    XINPUT_GAMEPAD_DPAD_LEFT,
    XINPUT_GAMEPAD_DPAD_RIGHT,
    XINPUT_GAMEPAD_START,
    XINPUT_GAMEPAD_BACK,
    XINPUT_GAMEPAD_LEFT_THUMB,
    XINPUT_GAMEPAD_RIGHT_THUMB,
    XINPUT_GAMEPAD_LEFT_SHOULDER,
    XINPUT_GAMEPAD_RIGHT_SHOULDER,
    XINPUT_GAMEPAD_GUIDE,
    XINPUT_GAMEPAD_A,
    XINPUT_GAMEPAD_B,
    XINPUT_GAMEPAD_X,
    XINPUT_GAMEPAD_Y,
)

# ─── ctypes Structures ────────────────────────────────────────────────────────


class XINPUT_GAMEPAD(ctypes.Structure):
    """XInput gamepad state structure."""
    _fields_ = [
        ("wButtons", ctypes.wintypes.WORD),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    """XInput state structure with packet number."""
    _fields_ = [
        ("dwPacketNumber", ctypes.wintypes.DWORD),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


# ─── XInput DLL loader ───────────────────────────────────────────────────────

def _load_xinput():
    """Load XInput DLL. Tries xinput1_4 first, then falls back."""
    dll_names = ["xinput1_4", "xinput1_3", "xinput9_1_0"]
    for name in dll_names:
        try:
            dll = ctypes.windll.LoadLibrary(f"{name}.dll")
            _LOGGER.info("Loaded %s.dll", name)
            return dll
        except OSError:
            continue
    return None


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class XInputState:
    """Parsed controller state snapshot."""
    connected: bool = False
    packet_number: int = 0
    buttons: int = 0
    left_trigger: float = 0.0
    right_trigger: float = 0.0
    left_stick_x: float = 0.0
    left_stick_y: float = 0.0
    right_stick_x: float = 0.0
    right_stick_y: float = 0.0


# ─── Helper functions ─────────────────────────────────────────────────────────

def find_controllers() -> list[int]:
    """Find all connected XInput controllers.

    Returns list of user indices (0-3) that have connected controllers.
    Works with DS4 (via DS4Windows), DualSense (via Steam), Xbox controllers.
    """
    xinput = _load_xinput()
    if not xinput:
        _LOGGER.error("XInput DLL not found - not on Windows?")
        return []

    connected = []
    state = XINPUT_STATE()

    for i in range(XINPUT_MAX_CONTROLLERS):
        result = xinput.XInputGetState(i, ctypes.byref(state))
        if result == ERROR_SUCCESS:
            connected.append(i)
            _LOGGER.info("Controller found at index %d", i)

    return connected


# ─── Main Class ───────────────────────────────────────────────────────────────

class XInputGamepad:
    """Direct XInput reader via ctypes. Zero middleware.

    Polls controller state at configurable rate (up to 1000Hz) and translates
    state changes to Controller button/stick calls with minimum latency.

    For DS4 at 1000Hz:
        1. Install HIDUSBF driver (overclock USB poll rate)
        2. Install DS4Windows (expose DS4 as XInput)
        3. Use poll_rate=1000

    :param user_index: XInput controller index (0-3)
    :param poll_rate: Polling frequency in Hz (default 1000 for DS4 overclock)
    :param deadzone: Stick deadzone threshold (0.0 to 1.0)
    :param trigger_threshold: Trigger activation threshold (0.0 to 1.0)
    :param button_map: Custom button mapping dict (XInput bitmask → PS name)
    """

    def __init__(
        self,
        user_index: int = 0,
        poll_rate: int = 1000,
        deadzone: float = 0.05,
        trigger_threshold: float = 0.10,
        button_map: dict = None,
    ):
        if user_index < 0 or user_index >= XINPUT_MAX_CONTROLLERS:
            raise ValueError(f"user_index must be 0-{XINPUT_MAX_CONTROLLERS - 1}")

        self._user_index = user_index
        self._poll_rate = poll_rate
        self._poll_interval = 1.0 / poll_rate
        self._deadzone = deadzone
        self._trigger_threshold = trigger_threshold
        self._button_map = button_map or dict(DEFAULT_BUTTON_MAP)

        self._controller: Optional[Controller] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._xinput = None

        # State tracking for delta detection
        self._prev_buttons: int = 0
        self._prev_left_trigger: float = 0.0
        self._prev_right_trigger: float = 0.0
        self._prev_lx: float = 0.0
        self._prev_ly: float = 0.0
        self._prev_rx: float = 0.0
        self._prev_ry: float = 0.0
        self._prev_packet: int = 0

        # Trigger button state
        self._l2_pressed = False
        self._r2_pressed = False

        # Performance stats
        self._polls_per_second = 0
        self._total_polls = 0
        self._state_changes = 0
        self._last_stats_time = 0.0

        # Optional callbacks
        self._on_state_change: Optional[Callable] = None
        self._on_disconnect: Optional[Callable] = None

    def connect(self, controller: Controller):
        """Connect to a pyremoteplay Controller.

        :param controller: The Controller instance to send inputs to
        """
        if not isinstance(controller, Controller):
            raise TypeError(f"Expected Controller, got {type(controller)}")
        self._controller = controller
        _LOGGER.info("XInputGamepad connected to Controller (index=%d)", self._user_index)

    def start(self):
        """Start polling the controller at the configured rate.

        Uses a tight polling loop with precise timing to maintain
        the target poll rate. At 1000Hz, each cycle is ~1ms.
        """
        if self._running:
            _LOGGER.warning("XInputGamepad already running")
            return

        self._xinput = _load_xinput()
        if not self._xinput:
            raise RuntimeError("Failed to load XInput DLL. Windows only.")

        # Verify controller is connected
        state = XINPUT_STATE()
        result = self._xinput.XInputGetState(self._user_index, ctypes.byref(state))
        if result != ERROR_SUCCESS:
            raise RuntimeError(
                f"Controller at index {self._user_index} not connected. "
                f"For DS4: make sure DS4Windows is running."
            )

        self._running = True
        self._last_stats_time = time.perf_counter()
        self._thread = threading.Thread(
            target=self._poll_loop, name="xinput-poll", daemon=True
        )
        self._thread.start()
        _LOGGER.info(
            "XInputGamepad started (index=%d, poll_rate=%dHz)",
            self._user_index, self._poll_rate
        )

    def stop(self):
        """Stop polling."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._xinput = None
        _LOGGER.info("XInputGamepad stopped")

    def get_state(self) -> XInputState:
        """Get current controller state (single read, non-blocking).

        Can be used for manual polling outside the automatic loop.
        """
        if not self._xinput:
            self._xinput = _load_xinput()
            if not self._xinput:
                return XInputState(connected=False)

        state = XINPUT_STATE()
        result = self._xinput.XInputGetState(self._user_index, ctypes.byref(state))

        if result != ERROR_SUCCESS:
            return XInputState(connected=False)

        gp = state.Gamepad
        return XInputState(
            connected=True,
            packet_number=state.dwPacketNumber,
            buttons=gp.wButtons,
            left_trigger=gp.bLeftTrigger / XINPUT_TRIGGER_MAX,
            right_trigger=gp.bRightTrigger / XINPUT_TRIGGER_MAX,
            left_stick_x=self._normalize_stick(gp.sThumbLX),
            left_stick_y=self._normalize_stick(-gp.sThumbLY),  # Invert Y
            right_stick_x=self._normalize_stick(gp.sThumbRX),
            right_stick_y=self._normalize_stick(-gp.sThumbRY),  # Invert Y
        )

    def _poll_loop(self):
        """High-frequency polling loop.

        Uses time.perf_counter() for precise timing and spin-waits
        to maintain exact poll rate. On Windows, perf_counter has
        sub-microsecond resolution.
        """
        state = XINPUT_STATE()
        xinput_get_state = self._xinput.XInputGetState
        user_index = self._user_index
        interval = self._poll_interval

        # Request high timer resolution on Windows (1ms instead of 15.6ms)
        try:
            winmm = ctypes.windll.winmm
            winmm.timeBeginPeriod(1)
            _LOGGER.debug("Set Windows timer resolution to 1ms")
        except (OSError, AttributeError):
            winmm = None

        polls_this_second = 0
        second_start = time.perf_counter()

        try:
            next_poll = time.perf_counter()

            while self._running:
                now = time.perf_counter()

                # Spin-wait for precise timing (busy wait for <1ms precision)
                if now < next_poll:
                    # For intervals > 2ms, use sleep to save CPU
                    remaining = next_poll - now
                    if remaining > 0.002:
                        time.sleep(remaining - 0.001)
                    # Spin for the last ~1ms for precision
                    while time.perf_counter() < next_poll:
                        pass

                next_poll += interval
                self._total_polls += 1
                polls_this_second += 1

                # Update stats every second
                if now - second_start >= 1.0:
                    self._polls_per_second = polls_this_second
                    polls_this_second = 0
                    second_start = now

                # Read state from XInput
                result = xinput_get_state(user_index, ctypes.byref(state))

                if result != ERROR_SUCCESS:
                    # Controller disconnected
                    if self._on_disconnect:
                        self._on_disconnect()
                    _LOGGER.warning("Controller disconnected (index=%d)", user_index)
                    # Wait before retrying
                    time.sleep(1.0)
                    next_poll = time.perf_counter()
                    continue

                # Only process if state changed (packet number differs)
                if state.dwPacketNumber == self._prev_packet:
                    continue

                self._prev_packet = state.dwPacketNumber
                self._state_changes += 1
                self._process_state(state.Gamepad)

        finally:
            if winmm:
                try:
                    winmm.timeEndPeriod(1)
                except (OSError, AttributeError):
                    pass

    def _process_state(self, gp: XINPUT_GAMEPAD):
        """Process gamepad state and send deltas to Controller.

        Only sends events for things that actually changed,
        minimizing USB/network traffic.
        """
        if not self._controller:
            return

        buttons = gp.wButtons

        # ─── Buttons (delta detection) ────────────────────────────────────────
        changed = buttons ^ self._prev_buttons
        if changed:
            for mask in ALL_BUTTONS:
                if changed & mask:
                    button_name = self._button_map.get(mask)
                    if button_name:
                        is_pressed = bool(buttons & mask)
                        action = "press" if is_pressed else "release"
                        self._controller.button(button_name, action)

        self._prev_buttons = buttons

        # ─── Triggers ─────────────────────────────────────────────────────────
        lt = gp.bLeftTrigger / XINPUT_TRIGGER_MAX
        rt = gp.bRightTrigger / XINPUT_TRIGGER_MAX

        # L2
        l2_pressed = lt > self._trigger_threshold
        if l2_pressed != self._l2_pressed:
            self._l2_pressed = l2_pressed
            action = "press" if l2_pressed else "release"
            self._controller.button("L2", action)

        # R2
        r2_pressed = rt > self._trigger_threshold
        if r2_pressed != self._r2_pressed:
            self._r2_pressed = r2_pressed
            action = "press" if r2_pressed else "release"
            self._controller.button("R2", action)

        # ─── Sticks (only send if changed beyond deadzone threshold) ──────────
        lx = self._normalize_stick(gp.sThumbLX)
        ly = self._normalize_stick(-gp.sThumbLY)  # Invert Y for PS convention
        rx = self._normalize_stick(gp.sThumbRX)
        ry = self._normalize_stick(-gp.sThumbRY)  # Invert Y for PS convention

        # Left stick
        if lx != self._prev_lx or ly != self._prev_ly:
            self._controller.stick("left", point=(lx, ly))
            self._prev_lx = lx
            self._prev_ly = ly

        # Right stick
        if rx != self._prev_rx or ry != self._prev_ry:
            self._controller.stick("right", point=(rx, ry))
            self._prev_rx = rx
            self._prev_ry = ry

        # Callback
        if self._on_state_change:
            self._on_state_change()

    def _normalize_stick(self, value: int) -> float:
        """Normalize stick value from [-32768, 32767] to [-1.0, 1.0] with deadzone."""
        # Normalize to [-1.0, 1.0]
        if value >= 0:
            normalized = value / XINPUT_STICK_MAX
        else:
            normalized = value / (-XINPUT_STICK_MIN)

        # Apply deadzone
        if abs(normalized) < self._deadzone:
            return 0.0

        # Rescale so deadzone edge = 0.0 and max = 1.0
        sign = 1.0 if normalized > 0 else -1.0
        rescaled = (abs(normalized) - self._deadzone) / (1.0 - self._deadzone)
        return sign * min(1.0, rescaled)

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """Return True if polling is active."""
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def user_index(self) -> int:
        """Return XInput user index (0-3)."""
        return self._user_index

    @property
    def poll_rate(self) -> int:
        """Return configured poll rate in Hz."""
        return self._poll_rate

    @poll_rate.setter
    def poll_rate(self, value: int):
        """Set poll rate. Takes effect immediately."""
        if value < 1 or value > 10000:
            raise ValueError("poll_rate must be between 1 and 10000")
        self._poll_rate = value
        self._poll_interval = 1.0 / value

    @property
    def actual_poll_rate(self) -> int:
        """Return measured polls per second (updated every second)."""
        return self._polls_per_second

    @property
    def deadzone(self) -> float:
        """Return stick deadzone."""
        return self._deadzone

    @deadzone.setter
    def deadzone(self, value: float):
        """Set stick deadzone (0.0 to 1.0)."""
        self._deadzone = max(0.0, min(0.99, abs(value)))

    @property
    def trigger_threshold(self) -> float:
        """Return trigger activation threshold."""
        return self._trigger_threshold

    @trigger_threshold.setter
    def trigger_threshold(self, value: float):
        """Set trigger threshold (0.0 to 1.0)."""
        self._trigger_threshold = max(0.0, min(1.0, value))

    @property
    def total_polls(self) -> int:
        """Return total polls performed."""
        return self._total_polls

    @property
    def state_changes(self) -> int:
        """Return total state changes detected."""
        return self._state_changes

    @property
    def controller(self) -> Optional[Controller]:
        """Return connected controller."""
        return self._controller

    @property
    def button_map(self) -> dict:
        """Return button mapping."""
        return dict(self._button_map)

    @button_map.setter
    def button_map(self, mapping: dict):
        """Set button mapping."""
        self._button_map = dict(mapping)

    @property
    def on_state_change(self) -> Optional[Callable]:
        """Return state change callback."""
        return self._on_state_change

    @on_state_change.setter
    def on_state_change(self, callback: Optional[Callable]):
        """Set state change callback (called on every input change)."""
        self._on_state_change = callback

    @property
    def on_disconnect(self) -> Optional[Callable]:
        """Return disconnect callback."""
        return self._on_disconnect

    @on_disconnect.setter
    def on_disconnect(self, callback: Optional[Callable]):
        """Set disconnect callback."""
        self._on_disconnect = callback
