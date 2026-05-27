"""Mouse/Keyboard to Controller Translator (XIM Matrix style).

Translates raw mouse movement into right stick (aiming) and keyboard
keys into left stick (movement) + buttons (actions).

Core concepts:
- Mouse delta X/Y → Right Stick X/Y with configurable sensitivity
- WASD keys → Left Stick with full analog simulation
- Other keys → PS buttons (Cross, Circle, L1, R1, etc)
- Mouse buttons → Triggers (L2/R2) and other buttons

Sensitivity:
    The sensitivity value controls how much mouse movement translates
    to stick deflection. Higher = faster aim, Lower = more precise.
    XIM Matrix typically uses values between 5-30.

    stick_value = clamp(mouse_delta * sensitivity / 100, -1.0, 1.0)

Smoothing:
    Optional exponential smoothing to reduce jitter.
    0.0 = no smoothing (raw input), 1.0 = maximum smoothing (sluggish)
    Recommended: 0.2-0.4 for FPS games

Aim curve:
    Controls the response curve for mouse → stick translation.
    - Linear (1.0): direct 1:1 mapping
    - Exponential (>1.0): slow at center, fast at edges (precision aiming)
    - Square root (<1.0): fast at center, slow at edges
"""

from __future__ import annotations
import logging
import threading
import time
from typing import Optional, Callable
from dataclasses import dataclass, field

from pyremoteplay.controller import Controller
from .rawinput import (
    RawInputReader, MouseEvent, KeyEvent,
    RI_MOUSE_LEFT_BUTTON_DOWN, RI_MOUSE_LEFT_BUTTON_UP,
    RI_MOUSE_RIGHT_BUTTON_DOWN, RI_MOUSE_RIGHT_BUTTON_UP,
    RI_MOUSE_MIDDLE_BUTTON_DOWN, RI_MOUSE_MIDDLE_BUTTON_UP,
    RI_MOUSE_BUTTON_4_DOWN, RI_MOUSE_BUTTON_4_UP,
    RI_MOUSE_BUTTON_5_DOWN, RI_MOUSE_BUTTON_5_UP,
    VK_W, VK_A, VK_S, VK_D, VK_SPACE, VK_R, VK_E, VK_Q, VK_F, VK_G, VK_C, VK_V, VK_X, VK_Z,
    VK_SHIFT, VK_LSHIFT, VK_CONTROL, VK_LCONTROL, VK_TAB, VK_ESCAPE,
    VK_1, VK_2, VK_3, VK_4,
    VK_F1, VK_F2,
)

_LOGGER = logging.getLogger(__name__)


# ─── Default FPS Key Mapping ─────────────────────────────────────────────────

DEFAULT_KEY_MAP = {
    # Movement (WASD) - handled specially as stick
    VK_W: "STICK_LEFT_UP",
    VK_S: "STICK_LEFT_DOWN",
    VK_A: "STICK_LEFT_LEFT",
    VK_D: "STICK_LEFT_RIGHT",

    # Actions
    VK_SPACE: "CROSS",         # Jump
    VK_R: "SQUARE",            # Reload
    VK_E: "TRIANGLE",          # Interact
    VK_Q: "L1",                # Tactical/Grenade
    VK_F: "R3",                # Melee
    VK_C: "CIRCLE",            # Crouch
    VK_G: "UP",                # D-pad up
    VK_Z: "DOWN",              # D-pad down
    VK_V: "R1",                # Ability

    # Shift/Ctrl
    VK_SHIFT: "L3",            # Sprint
    VK_LSHIFT: "L3",           # Sprint
    VK_CONTROL: "CIRCLE",      # Crouch/Prone
    VK_LCONTROL: "CIRCLE",     # Crouch/Prone

    # Numbers
    VK_1: "UP",                # D-pad up
    VK_2: "RIGHT",             # D-pad right
    VK_3: "DOWN",              # D-pad down
    VK_4: "LEFT",              # D-pad left

    # Utility
    VK_TAB: "TOUCHPAD",        # Touchpad/Map
    VK_ESCAPE: "OPTIONS",      # Pause

    # F keys
    VK_F1: "PS",               # PS button
    VK_F2: "SHARE",            # Share/Create
}

# Mouse button mapping
DEFAULT_MOUSE_MAP = {
    "left": "R2",              # Shoot (left click)
    "right": "L2",             # Aim/ADS (right click)
    "middle": "R3",            # Melee (middle click)
    "button4": "L1",           # Side button → tactical
    "button5": "R1",           # Side button → ability
}


# ─── Config ──────────────────────────────────────────────────────────────────

@dataclass
class TranslatorConfig:
    """Configuration for mouse/keyboard translator."""
    # Mouse sensitivity (higher = faster aiming)
    sensitivity_x: float = 15.0
    sensitivity_y: float = 15.0

    # Aim curve exponent (1.0 = linear, 2.0 = exponential, 0.5 = sqrt)
    aim_curve: float = 1.0

    # Smoothing (0.0 = none, 0.9 = very smooth/slow)
    smoothing: float = 0.0

    # Deadzone for stick output (prevents drift)
    stick_deadzone: float = 0.02

    # Maximum stick value (can limit to less than 1.0 for precision)
    stick_max: float = 1.0

    # Invert Y axis for mouse
    invert_y: bool = False

    # Key mapping (vkey → PS button name or STICK_LEFT_* for movement)
    key_map: dict = field(default_factory=lambda: dict(DEFAULT_KEY_MAP))

    # Mouse button mapping
    mouse_map: dict = field(default_factory=lambda: dict(DEFAULT_MOUSE_MAP))

    # How fast the stick returns to center when keys released (1.0 = instant)
    stick_return_speed: float = 1.0


# ─── Translator Class ─────────────────────────────────────────────────────────

class MouseTranslator:
    """Translates mouse/keyboard input to PS controller commands.

    This is the XIM Matrix equivalent - captures raw mouse/keyboard
    and converts them to stick/button commands for the PS5.

    Usage:
        translator = MouseTranslator(config=TranslatorConfig(sensitivity_x=20))
        translator.connect(controller)
        translator.start()
        # Now mouse aims and keyboard controls movement

    :param config: Translation configuration (sensitivity, mappings, etc)
    """

    def __init__(self, config: TranslatorConfig = None):
        self._config = config or TranslatorConfig()
        self._controller: Optional[Controller] = None
        self._reader = RawInputReader()
        self._running = False

        # Stick state (WASD keys held)
        self._left_stick_x = 0.0
        self._left_stick_y = 0.0
        self._right_stick_x = 0.0
        self._right_stick_y = 0.0

        # Key states for movement
        self._keys_held: set = set()

        # Smoothing state
        self._smooth_x = 0.0
        self._smooth_y = 0.0

        # Stats
        self._total_mouse_events = 0
        self._total_key_events = 0

        # Stick update thread
        self._stick_thread: Optional[threading.Thread] = None

    def connect(self, controller: Controller):
        """Connect to a pyremoteplay Controller.

        :param controller: The Controller to send translated inputs to
        """
        if not isinstance(controller, Controller):
            raise TypeError(f"Expected Controller, got {type(controller)}")
        self._controller = controller
        _LOGGER.info("MouseTranslator connected to Controller")

    def start(self):
        """Start capturing mouse/keyboard and translating to controller."""
        if self._running:
            return
        if not self._controller:
            raise RuntimeError("No controller connected. Call connect() first.")

        self._running = True

        # Set up raw input callbacks
        self._reader.on_mouse = self._handle_mouse
        self._reader.on_key = self._handle_key
        self._reader.start()

        # Start stick update thread (sends stick state at fixed rate)
        self._stick_thread = threading.Thread(
            target=self._stick_update_loop, name="stick-update", daemon=True
        )
        self._stick_thread.start()

        _LOGGER.info(
            "MouseTranslator started (sensitivity=%.1f/%.1f, curve=%.1f)",
            self._config.sensitivity_x, self._config.sensitivity_y,
            self._config.aim_curve,
        )

    def stop(self):
        """Stop capturing and translating."""
        self._running = False
        self._reader.stop()
        if self._stick_thread:
            self._stick_thread.join(timeout=2.0)
            self._stick_thread = None
        _LOGGER.info("MouseTranslator stopped")

    def _handle_mouse(self, event: MouseEvent):
        """Handle raw mouse event → right stick / buttons."""
        self._total_mouse_events += 1

        # Movement → Right Stick (aiming)
        if event.dx != 0 or event.dy != 0:
            self._process_mouse_aim(event.dx, event.dy)

        # Buttons → PS buttons
        if event.button:
            self._process_mouse_buttons(event.button)

    def _process_mouse_aim(self, dx: int, dy: int):
        """Convert mouse delta to right stick value."""
        cfg = self._config

        # Apply sensitivity
        raw_x = dx * cfg.sensitivity_x / 100.0
        raw_y = dy * cfg.sensitivity_y / 100.0

        # Invert Y if configured
        if cfg.invert_y:
            raw_y = -raw_y

        # Apply aim curve
        if cfg.aim_curve != 1.0:
            sign_x = 1.0 if raw_x >= 0 else -1.0
            sign_y = 1.0 if raw_y >= 0 else -1.0
            raw_x = sign_x * (abs(raw_x) ** cfg.aim_curve)
            raw_y = sign_y * (abs(raw_y) ** cfg.aim_curve)

        # Apply smoothing
        if cfg.smoothing > 0:
            self._smooth_x = self._smooth_x * cfg.smoothing + raw_x * (1.0 - cfg.smoothing)
            self._smooth_y = self._smooth_y * cfg.smoothing + raw_y * (1.0 - cfg.smoothing)
            raw_x = self._smooth_x
            raw_y = self._smooth_y

        # Clamp to [-1.0, 1.0]
        stick_x = max(-cfg.stick_max, min(cfg.stick_max, raw_x))
        stick_y = max(-cfg.stick_max, min(cfg.stick_max, raw_y))

        # Apply deadzone
        if abs(stick_x) < cfg.stick_deadzone:
            stick_x = 0.0
        if abs(stick_y) < cfg.stick_deadzone:
            stick_y = 0.0

        self._right_stick_x = stick_x
        self._right_stick_y = stick_y

        # Send immediately for responsiveness
        if self._controller:
            self._controller.stick("right", point=(stick_x, stick_y))

    def _process_mouse_buttons(self, flags: int):
        """Convert mouse button events to PS buttons."""
        if not self._controller:
            return

        mouse_map = self._config.mouse_map

        # Left button
        if flags & RI_MOUSE_LEFT_BUTTON_DOWN:
            btn = mouse_map.get("left")
            if btn:
                self._controller.button(btn, "press")
        if flags & RI_MOUSE_LEFT_BUTTON_UP:
            btn = mouse_map.get("left")
            if btn:
                self._controller.button(btn, "release")

        # Right button
        if flags & RI_MOUSE_RIGHT_BUTTON_DOWN:
            btn = mouse_map.get("right")
            if btn:
                self._controller.button(btn, "press")
        if flags & RI_MOUSE_RIGHT_BUTTON_UP:
            btn = mouse_map.get("right")
            if btn:
                self._controller.button(btn, "release")

        # Middle button
        if flags & RI_MOUSE_MIDDLE_BUTTON_DOWN:
            btn = mouse_map.get("middle")
            if btn:
                self._controller.button(btn, "press")
        if flags & RI_MOUSE_MIDDLE_BUTTON_UP:
            btn = mouse_map.get("middle")
            if btn:
                self._controller.button(btn, "release")

        # Button 4 (side)
        if flags & RI_MOUSE_BUTTON_4_DOWN:
            btn = mouse_map.get("button4")
            if btn:
                self._controller.button(btn, "press")
        if flags & RI_MOUSE_BUTTON_4_UP:
            btn = mouse_map.get("button4")
            if btn:
                self._controller.button(btn, "release")

        # Button 5 (side)
        if flags & RI_MOUSE_BUTTON_5_DOWN:
            btn = mouse_map.get("button5")
            if btn:
                self._controller.button(btn, "press")
        if flags & RI_MOUSE_BUTTON_5_UP:
            btn = mouse_map.get("button5")
            if btn:
                self._controller.button(btn, "release")

    def _handle_key(self, event: KeyEvent):
        """Handle keyboard event → left stick / buttons."""
        self._total_key_events += 1

        if not self._controller:
            return

        key_map = self._config.key_map
        mapping = key_map.get(event.vkey)

        if not mapping:
            return

        # Movement keys (WASD → Left Stick)
        if mapping.startswith("STICK_LEFT_"):
            self._handle_movement_key(mapping, event.is_down)
        else:
            # Regular button mapping
            action = "press" if event.is_down else "release"
            self._controller.button(mapping, action)

    def _handle_movement_key(self, direction: str, is_down: bool):
        """Handle WASD movement keys → Left Stick."""
        if is_down:
            self._keys_held.add(direction)
        else:
            self._keys_held.discard(direction)

        # Calculate left stick from held keys
        x = 0.0
        y = 0.0

        if "STICK_LEFT_LEFT" in self._keys_held:
            x -= 1.0
        if "STICK_LEFT_RIGHT" in self._keys_held:
            x += 1.0
        if "STICK_LEFT_UP" in self._keys_held:
            y -= 1.0
        if "STICK_LEFT_DOWN" in self._keys_held:
            y += 1.0

        # Normalize diagonal (so diagonal isn't faster)
        if x != 0 and y != 0:
            x *= 0.7071  # 1/sqrt(2)
            y *= 0.7071

        self._left_stick_x = x
        self._left_stick_y = y

        # Send immediately
        if self._controller:
            self._controller.stick("left", point=(x, y))

    def _stick_update_loop(self):
        """Periodic loop to decay right stick back to center.

        The mouse doesn't have a "release" - we need to return the
        right stick to center when the mouse stops moving.
        """
        decay_interval = 1.0 / 120.0  # 120Hz update rate
        last_update = time.perf_counter()

        while self._running:
            time.sleep(decay_interval)
            now = time.perf_counter()
            dt = now - last_update
            last_update = now

            # Decay right stick toward center (simulate stick spring-back)
            if self._right_stick_x != 0 or self._right_stick_y != 0:
                # Fast decay - mouse stick should snap back quickly
                decay = min(1.0, dt * 30.0)  # Full decay in ~33ms
                self._right_stick_x *= (1.0 - decay)
                self._right_stick_y *= (1.0 - decay)

                # Snap to zero if very small
                if abs(self._right_stick_x) < 0.01:
                    self._right_stick_x = 0.0
                if abs(self._right_stick_y) < 0.01:
                    self._right_stick_y = 0.0

                if self._controller:
                    self._controller.stick(
                        "right",
                        point=(self._right_stick_x, self._right_stick_y)
                    )

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def config(self) -> TranslatorConfig:
        """Return current config."""
        return self._config

    @config.setter
    def config(self, value: TranslatorConfig):
        """Set config."""
        self._config = value

    @property
    def sensitivity(self) -> tuple[float, float]:
        """Return (sensitivity_x, sensitivity_y)."""
        return (self._config.sensitivity_x, self._config.sensitivity_y)

    @sensitivity.setter
    def sensitivity(self, value: float):
        """Set both X and Y sensitivity."""
        self._config.sensitivity_x = value
        self._config.sensitivity_y = value

    @property
    def running(self) -> bool:
        """Return True if running."""
        return self._running

    @property
    def total_mouse_events(self) -> int:
        """Return total mouse events processed."""
        return self._total_mouse_events

    @property
    def total_key_events(self) -> int:
        """Return total key events processed."""
        return self._total_key_events
