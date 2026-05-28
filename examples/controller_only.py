"""Controller-Only Mode Example for pyremoteplay.

This example demonstrates how to use pyremoteplay in "controller-only" mode,
similar to how XIM Matrix works - sending controller inputs to the console
without processing any audio or video streams.

How it works:
- Authenticates with the PS4/PS5 via Remote Play protocol
- Establishes the UDP stream channel (required for sending inputs)
- Skips the network test (RTT/MTU) entirely
- Discards ALL incoming AV packets without processing
- Never starts the AV decoder worker thread
- Only sends controller feedback packets (buttons + sticks)

Benefits:
- Minimal input lag (no AV decoding, no network test delay)
- Very low CPU/memory usage (zero video/audio processing)
- No dependency on pyav/ffmpeg
- Ideal for input adapters, automation, or accessibility tools

Requirements:
- A registered PSN profile (see registration docs)
- PS4 or PS5 on the same network
"""

import asyncio
import logging
from pyremoteplay.session import Session
from pyremoteplay.controller import Controller
from pyremoteplay.profile import UserProfile

# Set to DEBUG to see connection details
logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger(__name__)

# --- Configuration ---
HOST = "192.168.1.100"  # IP of your PS4/PS5
PROFILE_PATH = None  # Path to .pyremoteplay profile, or None to use default


async def main():
    """Run controller-only session."""

    # Load user profile
    profile = UserProfile.load(PROFILE_PATH)
    if not profile or not profile.get("hosts"):
        _LOGGER.error(
            "No profile found. Register your device first. "
            "See: https://pyremoteplay.readthedocs.io"
        )
        return

    # Create session in controller-only mode.
    # This is ALL you need - no resolution, fps, codec, or receiver config required.
    # The session will only authenticate and open the control channel.
    session = Session(
        host=HOST,
        profile=profile,
        controller_only=True,  # <-- Only auth + controller, zero AV
    )

    _LOGGER.info("Starting controller-only session to %s...", HOST)

    # Start the session
    # In controller_only mode this will:
    # 1. Discover host via DDP
    # 2. Authenticate (HTTP handshake)
    # 3. Get session ID
    # 4. Open UDP stream (skip network test) -> handshake -> ready
    success = await session.start()
    if not success:
        _LOGGER.error("Failed to start session: %s", session.error)
        return

    # Wait for session to be fully ready
    ready = await session.async_wait(timeout=10)
    if not ready:
        _LOGGER.error("Session did not become ready: %s", session.error)
        session.stop()
        return

    _LOGGER.info("Session ready! Controller-only mode active.")

    # Create and connect controller
    controller = Controller()
    controller.connect(session)
    controller.start()

    try:
        # Example: Press X button
        _LOGGER.info("Pressing X button...")
        await controller.async_button("CROSS", "tap", delay=0.1)
        await asyncio.sleep(1)

        # Example: Move left stick
        _LOGGER.info("Moving left stick up...")
        controller.stick("left", axis="y", value=-1.0)
        await asyncio.sleep(0.5)

        # Release stick
        controller.stick("left", axis="y", value=0.0)
        await asyncio.sleep(0.5)

        # Example: Press multiple buttons
        _LOGGER.info("Pressing R1...")
        await controller.async_button("R1", "tap", delay=0.1)
        await asyncio.sleep(1)

        # Keep session alive - in a real use case you'd have your input loop here
        _LOGGER.info("Controller-only session running. Press Ctrl+C to stop.")
        while not session.is_stopped:
            await asyncio.sleep(0.1)

    except KeyboardInterrupt:
        _LOGGER.info("Stopping...")
    finally:
        controller.disconnect()
        session.stop()
        _LOGGER.info("Session ended.")


if __name__ == "__main__":
    asyncio.run(main())
