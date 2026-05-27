"""Windows direct input for pyremoteplay.

Reads controller input via XInput API (xinput1_4.dll) using ctypes,
bypassing SDL/pygame entirely on Windows.

For DS4/DualSense controllers, requires DS4Windows or Steam to expose
the controller as XInput device. With HIDUSBF driver, DS4 can run at 1000Hz.

Usage:
    from pyremoteplay.wininput import XInputGamepad, find_controllers

    controllers = find_controllers()
    gamepad = XInputGamepad(user_index=0, poll_rate=1000)
    gamepad.connect(controller)
    gamepad.start()
"""

from .xinput_gamepad import XInputGamepad, find_controllers, XInputState

__all__ = ["XInputGamepad", "find_controllers", "XInputState"]
