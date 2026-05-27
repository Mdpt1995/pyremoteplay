"""Evdev direct kernel input for pyremoteplay.

Reads controller input directly from /dev/input/eventX, bypassing SDL/pygame entirely.
This provides the lowest possible input latency on Linux (~0.1ms vs ~2-10ms with SDL).

Usage:
    from pyremoteplay.evdev import EvdevGamepad

    gamepad = EvdevGamepad("/dev/input/event0")
    gamepad.connect(controller)
    gamepad.start()
"""

from .evdev_gamepad import EvdevGamepad, find_gamepads, DeviceInfo

__all__ = ["EvdevGamepad", "find_gamepads", "DeviceInfo"]
