"""Windows Raw Input reader for mouse and keyboard.

Captures raw mouse deltas and keyboard events without affecting the cursor.
Uses Win32 RawInput API via ctypes for minimum latency input capture.

This is the foundation for mouse/keyboard → controller translation (XIM style).

Architecture:
    [Mouse/Keyboard HID] → [Win32 RawInput API] → [ctypes] → callbacks

How it works:
    - Creates a hidden message-only window to receive WM_INPUT messages
    - Registers for raw mouse and keyboard input
    - Mouse provides delta X/Y (relative movement, not cursor position)
    - Keyboard provides key up/down with scan codes
    - Runs in a dedicated thread pumping the Windows message queue

Requirements:
    - Windows 7+ (RawInput API)
    - No external dependencies (pure ctypes)
"""

from __future__ import annotations
import ctypes
import ctypes.wintypes
import threading
import logging
import time
from typing import Optional, Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# ─── Win32 Constants ──────────────────────────────────────────────────────────

WM_INPUT = 0x00FF
WM_QUIT = 0x0012

RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1

# RAWINPUTDEVICE flags
RIDEV_INPUTSINK = 0x00000100
RIDEV_NOLEGACY = 0x00000030

# Mouse flags
MOUSE_MOVE_RELATIVE = 0x00
RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
RI_MOUSE_LEFT_BUTTON_UP = 0x0002
RI_MOUSE_RIGHT_BUTTON_DOWN = 0x0004
RI_MOUSE_RIGHT_BUTTON_UP = 0x0008
RI_MOUSE_MIDDLE_BUTTON_DOWN = 0x0010
RI_MOUSE_MIDDLE_BUTTON_UP = 0x0020
RI_MOUSE_BUTTON_4_DOWN = 0x0040
RI_MOUSE_BUTTON_4_UP = 0x0080
RI_MOUSE_BUTTON_5_DOWN = 0x0100
RI_MOUSE_BUTTON_5_UP = 0x0200
RI_MOUSE_WHEEL = 0x0400

# Keyboard flags
RI_KEY_MAKE = 0x00  # Key down
RI_KEY_BREAK = 0x01  # Key up
RI_KEY_E0 = 0x02

# Usage pages
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_MOUSE = 0x02
HID_USAGE_GENERIC_KEYBOARD = 0x06

# Window styles
HWND_MESSAGE = ctypes.wintypes.HWND(-3)  # Message-only window

# ─── ctypes Structures ────────────────────────────────────────────────────────

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.wintypes.USHORT),
        ("usUsage", ctypes.wintypes.USHORT),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("hwndTarget", ctypes.wintypes.HWND),
    ]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [
        ("usFlags", ctypes.wintypes.USHORT),
        ("_padding", ctypes.wintypes.USHORT),
        ("usButtonFlags", ctypes.wintypes.USHORT),
        ("usButtonData", ctypes.c_short),
        ("ulRawButtons", ctypes.wintypes.ULONG),
        ("lLastX", ctypes.c_long),
        ("lLastY", ctypes.c_long),
        ("ulExtraInformation", ctypes.wintypes.ULONG),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", ctypes.wintypes.USHORT),
        ("Flags", ctypes.wintypes.USHORT),
        ("Reserved", ctypes.wintypes.USHORT),
        ("VKey", ctypes.wintypes.USHORT),
        ("Message", ctypes.wintypes.UINT),
        ("ExtraInformation", ctypes.wintypes.ULONG),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", ctypes.wintypes.DWORD),
        ("dwSize", ctypes.wintypes.DWORD),
        ("hDevice", ctypes.wintypes.HANDLE),
        ("wParam", ctypes.wintypes.WPARAM),
    ]


class RAWINPUT_MOUSE(ctypes.Structure):
    """RAWINPUT with mouse data."""
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("mouse", RAWMOUSE),
    ]


class RAWINPUT_KEYBOARD(ctypes.Structure):
    """RAWINPUT with keyboard data."""
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("keyboard", RAWKEYBOARD),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hWnd", ctypes.wintypes.HWND),
        ("message", ctypes.wintypes.UINT),
        ("wParam", ctypes.wintypes.WPARAM),
        ("lParam", ctypes.wintypes.LPARAM),
        ("time", ctypes.wintypes.DWORD),
        ("pt", ctypes.wintypes.POINT),
    ]


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("style", ctypes.wintypes.UINT),
        ("lpfnWndProc", ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.wintypes.HWND, ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
        )),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.wintypes.HINSTANCE),
        ("hIcon", ctypes.wintypes.HANDLE),
        ("hCursor", ctypes.wintypes.HANDLE),
        ("hbrBackground", ctypes.wintypes.HANDLE),
        ("lpszMenuName", ctypes.wintypes.LPCWSTR),
        ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ("hIconSm", ctypes.wintypes.HANDLE),
    ]


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class MouseEvent:
    """Mouse movement/button event."""
    dx: int = 0  # Relative X movement (pixels)
    dy: int = 0  # Relative Y movement (pixels)
    button: int = 0  # Button flags (RI_MOUSE_*)
    wheel: int = 0  # Wheel delta
    timestamp: float = 0.0


@dataclass
class KeyEvent:
    """Keyboard event."""
    vkey: int = 0  # Virtual key code
    scan_code: int = 0  # Scan code
    is_down: bool = False  # True = pressed, False = released
    is_e0: bool = False  # Extended key flag
    timestamp: float = 0.0


# ─── Virtual Key Codes (common ones for gaming) ──────────────────────────────

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3

# Letters (A-Z = 0x41-0x5A)
VK_A = 0x41
VK_B = 0x42
VK_C = 0x43
VK_D = 0x44
VK_E = 0x45
VK_F = 0x46
VK_G = 0x47
VK_H = 0x48
VK_I = 0x49
VK_J = 0x4A
VK_K = 0x4B
VK_L = 0x4C
VK_M = 0x4D
VK_N = 0x4E
VK_O = 0x4F
VK_P = 0x50
VK_Q = 0x51
VK_R = 0x52
VK_S = 0x53
VK_T = 0x54
VK_U = 0x55
VK_V = 0x56
VK_W = 0x57
VK_X = 0x58
VK_Y = 0x59
VK_Z = 0x5A

# Numbers (0-9 = 0x30-0x39)
VK_0 = 0x30
VK_1 = 0x31
VK_2 = 0x32
VK_3 = 0x33
VK_4 = 0x34
VK_5 = 0x35
VK_6 = 0x36
VK_7 = 0x37
VK_8 = 0x38
VK_9 = 0x39

# F keys
VK_F1 = 0x70
VK_F2 = 0x71
VK_F3 = 0x72
VK_F4 = 0x73
VK_F5 = 0x74


# ─── RawInput Reader Class ────────────────────────────────────────────────────

class RawInputReader:
    """Reads raw mouse/keyboard input from Windows RawInput API.

    Creates a hidden message-only window and registers for raw input events.
    Mouse provides raw deltas (not screen coordinates) - perfect for FPS.
    Keyboard provides key press/release events with virtual key codes.

    Usage:
        reader = RawInputReader()
        reader.on_mouse = lambda event: print(f"dx={event.dx} dy={event.dy}")
        reader.on_key = lambda event: print(f"key={event.vkey} down={event.is_down}")
        reader.start()
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._hwnd = None
        self._thread_id = 0

        # Callbacks
        self.on_mouse: Optional[Callable[[MouseEvent], None]] = None
        self.on_key: Optional[Callable[[KeyEvent], None]] = None

        # Stats
        self._mouse_events = 0
        self._key_events = 0

        # WndProc reference (prevent garbage collection)
        self._wndproc = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.wintypes.HWND, ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
        )(self._window_proc)

    def start(self):
        """Start capturing raw input in a dedicated thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._message_loop, name="rawinput", daemon=True
        )
        self._thread.start()
        _LOGGER.info("RawInput reader started")

    def stop(self):
        """Stop capturing raw input."""
        if not self._running:
            return
        self._running = False
        # Post WM_QUIT to break the message loop
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, WM_QUIT, 0, 0
            )
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        _LOGGER.info("RawInput reader stopped")

    def _message_loop(self):
        """Windows message loop for receiving WM_INPUT messages."""
        self._thread_id = kernel32.GetCurrentThreadId()

        # Register window class
        class_name = "PyRemotePlayRawInput"
        hinstance = kernel32.GetModuleHandleW(None)

        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = class_name

        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            _LOGGER.error("Failed to register window class")
            return

        # Create message-only window (invisible, no taskbar)
        self._hwnd = user32.CreateWindowExW(
            0, class_name, "RawInput", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, hinstance, None
        )
        if not self._hwnd:
            _LOGGER.error("Failed to create message window")
            return

        # Register for raw input (mouse + keyboard)
        devices = (RAWINPUTDEVICE * 2)()

        # Mouse
        devices[0].usUsagePage = HID_USAGE_PAGE_GENERIC
        devices[0].usUsage = HID_USAGE_GENERIC_MOUSE
        devices[0].dwFlags = RIDEV_INPUTSINK
        devices[0].hwndTarget = self._hwnd

        # Keyboard
        devices[1].usUsagePage = HID_USAGE_PAGE_GENERIC
        devices[1].usUsage = HID_USAGE_GENERIC_KEYBOARD
        devices[1].dwFlags = RIDEV_INPUTSINK
        devices[1].hwndTarget = self._hwnd

        result = user32.RegisterRawInputDevices(
            devices, 2, ctypes.sizeof(RAWINPUTDEVICE)
        )
        if not result:
            _LOGGER.error("Failed to register raw input devices")
            return

        _LOGGER.info("RawInput registered (mouse + keyboard)")

        # Message pump
        msg = MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # Cleanup
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        user32.UnregisterClassW(class_name, hinstance)

    def _window_proc(self, hwnd, msg, wparam, lparam):
        """Window procedure for handling WM_INPUT."""
        if msg == WM_INPUT:
            self._handle_raw_input(lparam)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_raw_input(self, lparam):
        """Parse raw input data from WM_INPUT message."""
        # Get data size
        size = ctypes.wintypes.UINT(0)
        user32.GetRawInputData(
            lparam, RID_INPUT, None, ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER)
        )
        if size.value == 0:
            return

        # Read raw input header to determine type
        buf = ctypes.create_string_buffer(size.value)
        user32.GetRawInputData(
            lparam, RID_INPUT, buf, ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER)
        )

        # Check type from header
        header = RAWINPUTHEADER.from_buffer_copy(buf)

        if header.dwType == RIM_TYPEMOUSE:
            raw = RAWINPUT_MOUSE.from_buffer_copy(buf)
            self._handle_mouse(raw.mouse)
        elif header.dwType == RIM_TYPEKEYBOARD:
            raw = RAWINPUT_KEYBOARD.from_buffer_copy(buf)
            self._handle_keyboard(raw.keyboard)

    def _handle_mouse(self, mouse: RAWMOUSE):
        """Handle raw mouse data."""
        self._mouse_events += 1
        now = time.perf_counter()

        # Movement
        if mouse.lLastX != 0 or mouse.lLastY != 0:
            if self.on_mouse:
                event = MouseEvent(
                    dx=mouse.lLastX,
                    dy=mouse.lLastY,
                    button=0,
                    wheel=0,
                    timestamp=now,
                )
                self.on_mouse(event)

        # Buttons
        if mouse.usButtonFlags:
            if self.on_mouse:
                wheel = 0
                if mouse.usButtonFlags & RI_MOUSE_WHEEL:
                    wheel = mouse.usButtonData
                event = MouseEvent(
                    dx=0,
                    dy=0,
                    button=mouse.usButtonFlags,
                    wheel=wheel,
                    timestamp=now,
                )
                self.on_mouse(event)

    def _handle_keyboard(self, keyboard: RAWKEYBOARD):
        """Handle raw keyboard data."""
        self._key_events += 1

        if self.on_key:
            is_down = not bool(keyboard.Flags & RI_KEY_BREAK)
            is_e0 = bool(keyboard.Flags & RI_KEY_E0)
            event = KeyEvent(
                vkey=keyboard.VKey,
                scan_code=keyboard.MakeCode,
                is_down=is_down,
                is_e0=is_e0,
                timestamp=time.perf_counter(),
            )
            self.on_key(event)

    @property
    def running(self) -> bool:
        """Return True if capturing."""
        return self._running

    @property
    def mouse_events(self) -> int:
        """Return total mouse events captured."""
        return self._mouse_events

    @property
    def key_events(self) -> int:
        """Return total key events captured."""
        return self._key_events
