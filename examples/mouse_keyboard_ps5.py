"""Mouse & Keyboard → PS5 Controller (XIM Matrix style).

Captures your mouse and keyboard and translates them into PS5 controller
inputs via Remote Play. Play FPS games on PS5 with mouse aim!

Default mapping (FPS):
    Mouse Move      → Right Stick (aim)
    Left Click      → R2 (shoot)
    Right Click     → L2 (aim down sights)
    Middle Click    → R3 (melee)
    Side Button 4   → L1 (tactical)
    Side Button 5   → R1 (ability)

    W/A/S/D         → Left Stick (move)
    Space           → Cross (jump)
    Shift           → L3 (sprint)
    R               → Square (reload)
    E               → Triangle (interact)
    Q               → L1 (tactical)
    C               → Circle (crouch)
    F               → R3 (melee)
    Tab             → Touchpad
    Esc             → Options

    1/2/3/4         → D-pad Up/Right/Down/Left
    F1              → PS button
    F2              → Share

Usage:
    python mouse_keyboard_ps5.py --host 192.168.100.5
    python mouse_keyboard_ps5.py --host 192.168.100.5 --sens 20
    python mouse_keyboard_ps5.py --host 192.168.100.5 --sens 12 --curve 1.5

Tips:
    - Alt+Tab to switch away (raw input stops capturing when window loses focus)
    - Adjust sensitivity with --sens (default 15, higher = faster aim)
    - Use --curve 1.5 for exponential aim (precision at slow, fast at flick)
    - Press F1 to simulate PS button (useful for PS5 menu)

Requirements:
    - Windows 7+
    - Python 3.8+
    - pyremoteplay registered with PS5
    - PS5 on same network with Remote Play enabled
"""

import asyncio
import argparse
import logging
import signal

from pyremoteplay.session import Session
from pyremoteplay.controller import Controller
from pyremoteplay.profile import Profiles
from pyremoteplay.wininput.mouse_translator import MouseTranslator, TranslatorConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
_LOGGER = logging.getLogger(__name__)


async def main(host: str, sensitivity: float, curve: float, smoothing: float, invert_y: bool):
    """Run mouse/keyboard → PS5 session."""

    # ─── 1. Load profile ──────────────────────────────────────────────────────
    profiles = Profiles.load("")
    if not profiles:
        _LOGGER.error("No profiles found. Register first: python -m pyremoteplay -r <ip>")
        return

    profile_name = list(profiles.keys())[0]
    profile = profiles[profile_name]
    _LOGGER.info("Using profile: %s", profile_name)

    # ─── 2. Create session ────────────────────────────────────────────────────
    session = Session(
        host=host,
        profile=profile,
        controller_only=True,
    )

    controller = Controller()
    controller.connect(session)

    _LOGGER.info("Connecting to %s...", host)

    success = await session.start()
    if not success:
        _LOGGER.error("Session failed: %s", session.error)
        return

    ready = await session.async_wait(timeout=15)
    if not ready:
        _LOGGER.error("Session not ready: %s", session.error)
        session.stop()
        return

    _LOGGER.info("Session READY!")
    controller.start()

    # ─── 3. Setup mouse/keyboard translator ───────────────────────────────────
    config = TranslatorConfig(
        sensitivity_x=sensitivity,
        sensitivity_y=sensitivity,
        aim_curve=curve,
        smoothing=smoothing,
        invert_y=invert_y,
    )

    translator = MouseTranslator(config=config)
    translator.connect(controller)
    translator.start()

    _LOGGER.info("=" * 55)
    _LOGGER.info("  MOUSE & KEYBOARD → PS5 ACTIVE")
    _LOGGER.info("")
    _LOGGER.info("  Sensitivity: %.1f", sensitivity)
    _LOGGER.info("  Aim Curve:   %.1f (1.0=linear, >1=exponential)", curve)
    _LOGGER.info("  Smoothing:   %.1f", smoothing)
    _LOGGER.info("  Invert Y:    %s", invert_y)
    _LOGGER.info("")
    _LOGGER.info("  Mouse   → Right Stick (aim)")
    _LOGGER.info("  WASD    → Left Stick (move)")
    _LOGGER.info("  LClick  → R2 (shoot)")
    _LOGGER.info("  RClick  → L2 (ADS)")
    _LOGGER.info("  Space   → X (jump)")
    _LOGGER.info("  Shift   → L3 (sprint)")
    _LOGGER.info("")
    _LOGGER.info("  Press Ctrl+C to stop")
    _LOGGER.info("=" * 55)
    print()

    # ─── 4. Run until stopped ─────────────────────────────────────────────────
    try:
        while not session.is_stopped:
            await asyncio.sleep(5.0)
            _LOGGER.info(
                "Stats: mouse_events=%d | key_events=%d",
                translator.total_mouse_events,
                translator.total_key_events,
            )
    except KeyboardInterrupt:
        _LOGGER.info("Ctrl+C - stopping...")
    finally:
        translator.stop()
        controller.disconnect()
        session.stop()
        _LOGGER.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Mouse & Keyboard → PS5 Controller (XIM Matrix style)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --host 192.168.100.5
  %(prog)s --host 192.168.100.5 --sens 20
  %(prog)s --host 192.168.100.5 --sens 12 --curve 1.5 --smooth 0.3

Sensitivity Guide:
  5-10  : Very slow (sniper precision)
  10-15 : Medium (balanced)
  15-25 : Fast (aggressive play)
  25+   : Very fast (high DPI mouse)
""",
    )
    parser.add_argument(
        "--host", "-H", required=True,
        help="IP address of PS5",
    )
    parser.add_argument(
        "--sens", "-s", type=float, default=15.0,
        help="Mouse sensitivity (default: 15.0)",
    )
    parser.add_argument(
        "--curve", "-c", type=float, default=1.0,
        help="Aim curve exponent (1.0=linear, 1.5=exponential, default: 1.0)",
    )
    parser.add_argument(
        "--smooth", type=float, default=0.0,
        help="Smoothing factor 0.0-0.9 (default: 0.0 = no smoothing)",
    )
    parser.add_argument(
        "--invert-y", action="store_true",
        help="Invert Y axis for mouse",
    )

    args = parser.parse_args()
    asyncio.run(main(args.host, args.sens, args.curve, args.smooth, args.invert_y))
