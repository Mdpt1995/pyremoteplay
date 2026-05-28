"""Windows direct input for pyremoteplay.

Reads controller input via XInput API (xinput1_4.dll) using ctypes,
bypassing SDL/pygame entirely on Windows.

Also provides raw mouse/keyboard capture and translation to controller
inputs (XIM Matrix style mouse+keyboard → PS controller).

Usage:
    # Gamepad (DS4/Xbox via XInput)
    from pyremoteplay.wininput import XInputGamepad, find_controllers
    gamepad = XInputGamepad(user_index=0, poll_rate=1000)
    gamepad.connect(controller)
    gamepad.start()

    # Mouse/Keyboard → Controller
    from pyremoteplay.wininput import MouseTranslator, TranslatorConfig
    translator = MouseTranslator(config=TranslatorConfig(sensitivity_x=15))
    translator.connect(controller)
    translator.start()
"""

from .xinput_gamepad import XInputGamepad, find_controllers, XInputState
from .rawinput import RawInputReader, MouseEvent, KeyEvent
from .mouse_translator import MouseTranslator, TranslatorConfig

__all__ = [
    "XInputGamepad", "find_controllers", "XInputState",
    "RawInputReader", "MouseEvent", "KeyEvent",
    "MouseTranslator", "TranslatorConfig",
]
