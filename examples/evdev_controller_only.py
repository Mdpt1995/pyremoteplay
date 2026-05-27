"""Kernel-Mode Controller-Only Example.

The fastest possible input path on Linux:
- Reads controller directly from kernel (/dev/input/eventX) - NO SDL/pygame
- Sends inputs via Remote Play protocol - NO audio/video processing
- Skips network test - connects instantly

Total added latency: <1ms (kernel read ~0.1ms + UDP send ~0.5ms)

This is the pyremoteplay equivalent of what XIM Matrix does in hardware.

Requirements:
    - Linux (uses /dev/input/ evdev interface)
    - User in 'input' group (or run as root): sudo usermod -aG input $USER
    - PS4/PS5 on same network, registered profile

Usage:
    python evdev_controller_only.py
    python evdev_controller_only.py --host 192.168.1.100 --device /dev/input/event5
"""

import asyncio
import argparse
import logging
import signal

from pyremoteplay.session import Session
from pyremoteplay.controller import Controller
from pyremoteplay.profile import UserProfile
from pyremoteplay.evdev import EvdevGamepad, find_gamepads

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
_LOGGER = logging.getLogger(__name__)


def list_devices():
    """List all detected gamepad devices."""
    print("\n=== Detected Gamepads ===\n")
    devices = find_gamepads()
    if not devices:
        print("  No gamepads found.")
        print("  Make sure your controller is connected and you have")
        print("  read permissions on /dev/input/event*")
        print("  Try: sudo usermod -aG input $USER (then re-login)")
        return None

    for i, dev in enumerate(devices):
        print(f"  [{i}] {dev.name}")
        print(f"      Path: {dev.path}")
        if dev.uniq:
            print(f"      MAC:  {dev.uniq}")
        print()

    return devices


async def main(host: str, device_path: str = None, profile_path: str = None):
    """Run kernel-mode controller-only session."""

    # ─── 1. Find gamepad device ───────────────────────────────────────────────
    if not device_path:
        devices = list_devices()
        if not devices:
            return
        if len(devices) == 1:
            device_path = devices[0].path
            _LOGGER.info("Auto-selected: %s (%s)", devices[0].name, device_path)
        else:
            try:
                idx = int(input("Select device number: "))
                device_path = devices[idx].path
            except (ValueError, IndexError):
                _LOGGER.error("Invalid selection")
                return

    # ─── 2. Load profile ──────────────────────────────────────────────────────
    profile = UserProfile.load(profile_path)
    if not profile or not profile.get("hosts"):
        _LOGGER.error("No profile found. Register first.")
        return

    # ─── 3. Create session (controller-only = no AV) ──────────────────────────
    session = Session(
        host=host,
        profile=profile,
        controller_only=True,
    )

    _LOGGER.info("Connecting to %s (controller-only, kernel input)...", host)

    success = await session.start()
    if not success:
        _LOGGER.error("Session failed: %s", session.error)
        return

    ready = await session.async_wait(timeout=10)
    if not ready:
        _LOGGER.error("Session not ready: %s", session.error)
        session.stop()
        return

    _LOGGER.info("Session READY")

    # ─── 4. Setup controller + evdev gamepad ──────────────────────────────────
    controller = Controller()
    controller.connect(session)
    controller.start()

    gamepad = EvdevGamepad(device_path, deadzone=0.08)
    gamepad.connect(controller)

    # Optional: log raw events for debugging
    # gamepad.on_raw_event = lambda t, c, v: print(f"  raw: type={t} code=0x{c:x} val={v}")

    gamepad.start()

    _LOGGER.info("=== KERNEL MODE ACTIVE ===")
    _LOGGER.info("Device: %s", device_path)
    _LOGGER.info("Latency path: kernel → Controller → UDP → PS")
    _LOGGER.info("Press Ctrl+C to stop")
    print()

    # ─── 5. Run until stopped ─────────────────────────────────────────────────
    stop_event = asyncio.Event()

    def _signal_handler():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        # Print stats every 5 seconds
        while not stop_event.is_set() and not session.is_stopped:
            await asyncio.sleep(5.0)
            if gamepad.running:
                _LOGGER.debug(
                    "Events processed: %d", gamepad.events_processed
                )
    except asyncio.CancelledError:
        pass
    finally:
        _LOGGER.info("Shutting down...")
        gamepad.stop()
        controller.disconnect()
        session.stop()
        _LOGGER.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Kernel-mode controller-only Remote Play (XIM Matrix style)"
    )
    parser.add_argument(
        "--host", "-H",
        default="192.168.1.100",
        help="IP address of PS4/PS5 (default: 192.168.1.100)",
    )
    parser.add_argument(
        "--device", "-d",
        default=None,
        help="Input device path (e.g., /dev/input/event5). Auto-detects if not specified.",
    )
    parser.add_argument(
        "--profile", "-p",
        default=None,
        help="Path to .pyremoteplay profile directory",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List detected gamepads and exit",
    )
    args = parser.parse_args()

    if args.list:
        list_devices()
    else:
        asyncio.run(main(args.host, args.device, args.profile))
