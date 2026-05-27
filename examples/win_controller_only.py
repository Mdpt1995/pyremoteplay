"""Windows Kernel-Mode Controller-Only Example.

The fastest possible input path on Windows:
- Reads DS4/DualSense/Xbox directly via XInput (xinput1_4.dll) - NO SDL/pygame
- Polls at 1000Hz (matches DS4 HIDUSBF overclock)
- Sends inputs via Remote Play protocol - NO audio/video processing
- Skips network test - connects instantly

Total added latency: ~1-2ms (XInput read ~0.2ms + poll interval 1ms + UDP send ~0.5ms)

Setup for DS4 at 1000Hz:
    1. Install HIDUSBF driver: https://github.com/LordOfMice/hidusbf
       - This overclocks the DS4 USB polling rate from 250Hz to 1000Hz
       - Reduces input polling interval from 4ms to 1ms
    2. Install DS4Windows: https://ds4windows.io/
       - Exposes DS4 as XInput device (required for xinput1_4.dll to see it)
       - Set DS4Windows polling to 1ms (match HIDUSBF)
    3. Run this script

Setup for Xbox/DualSense:
    - Xbox controllers work natively with XInput (no extra software)
    - DualSense: use Steam or DS4Windows to expose as XInput
    - For 1000Hz on Xbox: use Xbox Accessories app (Elite controller)

Requirements:
    - Windows 7+ (xinput1_4.dll included)
    - Python 3.8+
    - pyremoteplay (pip install pyremoteplay)
    - For DS4: DS4Windows running
    - For 1000Hz: HIDUSBF driver installed

Usage:
    python win_controller_only.py
    python win_controller_only.py --host 192.168.1.100 --rate 1000
    python win_controller_only.py --host 192.168.1.100 --index 0 --rate 500
"""

import asyncio
import argparse
import logging
import sys
import time

from pyremoteplay.session import Session
from pyremoteplay.controller import Controller
from pyremoteplay.profile import UserProfile
from pyremoteplay.wininput import XInputGamepad, find_controllers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
_LOGGER = logging.getLogger(__name__)


def list_controllers():
    """List all connected XInput controllers."""
    print("\n=== Detected XInput Controllers ===\n")
    connected = find_controllers()
    if not connected:
        print("  No controllers found!")
        print()
        print("  For DS4/DualSense:")
        print("    - Install DS4Windows: https://ds4windows.io/")
        print("    - Make sure DS4Windows is running and controller is connected")
        print()
        print("  For Xbox controllers:")
        print("    - Should work automatically via USB or Xbox Wireless Adapter")
        print()
        return None

    for idx in connected:
        # Get a quick state read to confirm it's working
        gamepad = XInputGamepad(user_index=idx, poll_rate=60)
        state = gamepad.get_state()
        status = "CONNECTED" if state.connected else "ERROR"
        print(f"  [Index {idx}] {status}")

    print()
    print(f"  Total: {len(connected)} controller(s)")
    print()
    return connected


async def main(host: str, user_index: int = 0, poll_rate: int = 1000, profile_path: str = None):
    """Run Windows kernel-mode controller-only session."""

    # ─── 1. Verify controller ─────────────────────────────────────────────────
    _LOGGER.info("Checking XInput controller at index %d...", user_index)
    gamepad = XInputGamepad(
        user_index=user_index,
        poll_rate=poll_rate,
        deadzone=0.05,
        trigger_threshold=0.10,
    )
    state = gamepad.get_state()
    if not state.connected:
        _LOGGER.error("No controller at index %d!", user_index)
        _LOGGER.error("For DS4: make sure DS4Windows is running")
        list_controllers()
        return

    _LOGGER.info("Controller found at index %d", user_index)

    # ─── 2. Load profile ──────────────────────────────────────────────────────
    profile = UserProfile.load(profile_path)
    if not profile or not profile.get("hosts"):
        _LOGGER.error("No profile found. Register your device first.")
        _LOGGER.error("See: https://pyremoteplay.readthedocs.io")
        return

    # ─── 3. Create session (controller-only = zero AV) ────────────────────────
    session = Session(
        host=host,
        profile=profile,
        controller_only=True,
    )

    _LOGGER.info("Connecting to %s (controller-only, XInput %dHz)...", host, poll_rate)

    success = await session.start()
    if not success:
        _LOGGER.error("Session failed: %s", session.error)
        return

    ready = await session.async_wait(timeout=10)
    if not ready:
        _LOGGER.error("Session not ready: %s", session.error)
        session.stop()
        return

    _LOGGER.info("Session READY!")

    # ─── 4. Connect controller + start XInput polling ─────────────────────────
    controller = Controller()
    controller.connect(session)
    controller.start()

    gamepad.connect(controller)
    gamepad.start()

    _LOGGER.info("=" * 50)
    _LOGGER.info("  WINDOWS KERNEL MODE ACTIVE")
    _LOGGER.info("  XInput index: %d", user_index)
    _LOGGER.info("  Poll rate: %d Hz (interval: %.1fms)", poll_rate, 1000.0 / poll_rate)
    _LOGGER.info("  Deadzone: %.0f%%", gamepad.deadzone * 100)
    _LOGGER.info("  Path: xinput1_4.dll -> Controller -> UDP -> PS")
    _LOGGER.info("=" * 50)
    _LOGGER.info("Press Ctrl+C to stop")
    print()

    # ─── 5. Run until stopped ─────────────────────────────────────────────────
    try:
        while not session.is_stopped:
            await asyncio.sleep(3.0)
            if gamepad.running:
                _LOGGER.info(
                    "Stats: actual_rate=%dHz | polls=%d | changes=%d",
                    gamepad.actual_poll_rate,
                    gamepad.total_polls,
                    gamepad.state_changes,
                )
    except KeyboardInterrupt:
        _LOGGER.info("Ctrl+C received")
    finally:
        _LOGGER.info("Shutting down...")
        gamepad.stop()
        controller.disconnect()
        session.stop()
        _LOGGER.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Windows kernel-mode controller-only Remote Play (XIM Matrix style)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
DS4 at 1000Hz Setup:
  1. HIDUSBF driver: overclock DS4 USB polling (250Hz → 1000Hz)
  2. DS4Windows: expose DS4 as XInput device
  3. This script: read XInput at 1000Hz, send via Remote Play

Examples:
  %(prog)s --host 192.168.1.100
  %(prog)s --host 192.168.1.100 --rate 1000 --index 0
  %(prog)s --list
""",
    )
    parser.add_argument(
        "--host", "-H",
        default="192.168.1.100",
        help="IP address of PS4/PS5 (default: 192.168.1.100)",
    )
    parser.add_argument(
        "--index", "-i",
        type=int,
        default=0,
        help="XInput controller index 0-3 (default: 0)",
    )
    parser.add_argument(
        "--rate", "-r",
        type=int,
        default=1000,
        help="Polling rate in Hz (default: 1000 for DS4 HIDUSBF)",
    )
    parser.add_argument(
        "--profile", "-p",
        default=None,
        help="Path to .pyremoteplay profile directory",
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List connected XInput controllers and exit",
    )
    parser.add_argument(
        "--deadzone", "-d",
        type=float,
        default=0.05,
        help="Stick deadzone 0.0-1.0 (default: 0.05)",
    )

    args = parser.parse_args()

    if args.list:
        list_controllers()
    else:
        asyncio.run(main(args.host, args.index, args.rate, args.profile))
