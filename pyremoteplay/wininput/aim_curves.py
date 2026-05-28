"""Ballistic Aim Curves for Mouse → Stick Translation.

Defines how mouse movement speed translates to stick deflection.
Different curves provide different "feel" for aiming:

- LINEAR: Direct 1:1 mapping. Simple but can feel twitchy.
- EXPONENTIAL: Slow at small movements, fast at large. Good for precision.
- S_CURVE: Slow at center, fast in middle, slow at max. Natural feel.
- BALLISTIC: Velocity-dependent. Slow = precision, fast = quick turns.
- SNIPER: Very slow response, maximum precision for long-range.
- AGGRESSIVE: Very fast response, for close-range combat.
- APEX: Mimics XIM APEX default curve. Balanced for FPS.

How ballistic curves work:
    Instead of a simple exponent (value^curve), ballistic curves use
    the SPEED of mouse movement to dynamically adjust the response.

    Slow mouse → small stick deflection (precision aiming)
    Fast mouse → large stick deflection (quick flick/turn)

    This is what makes XIM Matrix feel natural compared to simple
    sensitivity scaling.

Usage:
    from pyremoteplay.wininput.aim_curves import AimCurve, PRESETS

    curve = AimCurve.from_preset("ballistic")
    stick_value = curve.apply(mouse_delta, sensitivity=15.0)
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class CurvePreset(str, Enum):
    """Available curve presets."""
    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    S_CURVE = "s_curve"
    BALLISTIC = "ballistic"
    SNIPER = "sniper"
    AGGRESSIVE = "aggressive"
    APEX = "apex"


@dataclass
class CurveConfig:
    """Configuration for an aim curve.

    :param exponent: Power curve exponent (1.0 = linear, 2.0 = quadratic)
    :param midpoint: S-curve midpoint (0.0-1.0, where the curve inflects)
    :param steepness: S-curve steepness (higher = sharper transition)
    :param velocity_scale: How much velocity affects output (ballistic)
    :param velocity_decay: How fast velocity decays between samples (0.0-1.0)
    :param min_output: Minimum stick output (prevents dead movement)
    :param max_output: Maximum stick output cap
    :param acceleration: Additional acceleration at high speeds (0.0 = none)
    :param smoothing_samples: Number of samples for velocity averaging
    """
    exponent: float = 1.0
    midpoint: float = 0.5
    steepness: float = 6.0
    velocity_scale: float = 1.0
    velocity_decay: float = 0.8
    min_output: float = 0.0
    max_output: float = 1.0
    acceleration: float = 0.0
    smoothing_samples: int = 3


# ─── Preset Configurations ────────────────────────────────────────────────────

PRESET_CONFIGS = {
    CurvePreset.LINEAR: CurveConfig(
        exponent=1.0,
        velocity_scale=0.0,
        acceleration=0.0,
    ),
    CurvePreset.EXPONENTIAL: CurveConfig(
        exponent=2.0,
        velocity_scale=0.0,
        acceleration=0.0,
    ),
    CurvePreset.S_CURVE: CurveConfig(
        exponent=1.0,
        midpoint=0.5,
        steepness=6.0,
        velocity_scale=0.0,
    ),
    CurvePreset.BALLISTIC: CurveConfig(
        exponent=1.3,
        velocity_scale=0.6,
        velocity_decay=0.75,
        acceleration=0.3,
        smoothing_samples=4,
    ),
    CurvePreset.SNIPER: CurveConfig(
        exponent=2.5,
        velocity_scale=0.3,
        velocity_decay=0.9,
        max_output=0.6,
        acceleration=0.0,
        smoothing_samples=5,
    ),
    CurvePreset.AGGRESSIVE: CurveConfig(
        exponent=0.7,
        velocity_scale=0.8,
        velocity_decay=0.5,
        acceleration=0.6,
        smoothing_samples=2,
    ),
    CurvePreset.APEX: CurveConfig(
        exponent=1.5,
        midpoint=0.4,
        steepness=5.0,
        velocity_scale=0.4,
        velocity_decay=0.7,
        acceleration=0.2,
        smoothing_samples=3,
    ),
}


# ─── Aim Curve Class ──────────────────────────────────────────────────────────

class AimCurve:
    """Ballistic aim curve for mouse → stick translation.

    Applies a response curve to mouse input that considers both
    the magnitude and velocity of movement for natural-feeling aim.

    Usage:
        curve = AimCurve.from_preset("ballistic")
        # or
        curve = AimCurve(config=CurveConfig(exponent=1.5, velocity_scale=0.4))

        # Apply to mouse delta
        stick_x = curve.apply(mouse_dx, sensitivity=15.0)
        stick_y = curve.apply(mouse_dy, sensitivity=15.0)
    """

    def __init__(self, config: CurveConfig = None, preset: str = None):
        """Create aim curve.

        :param config: Custom curve configuration
        :param preset: Preset name (overrides config if both given)
        """
        if preset:
            config = PRESET_CONFIGS.get(CurvePreset(preset))
            if not config:
                raise ValueError(f"Unknown preset: {preset}")
        self._config = config or CurveConfig()
        self._velocity_history: list[float] = []
        self._last_velocity: float = 0.0

    @classmethod
    def from_preset(cls, preset: str) -> AimCurve:
        """Create aim curve from preset name.

        :param preset: One of: linear, exponential, s_curve, ballistic, sniper, aggressive, apex
        """
        return cls(preset=preset)

    @classmethod
    def presets(cls) -> list[str]:
        """Return list of available preset names."""
        return [p.value for p in CurvePreset]

    def apply(self, delta: float, sensitivity: float = 15.0) -> float:
        """Apply curve to a mouse delta value.

        :param delta: Raw mouse delta (pixels moved)
        :param sensitivity: Sensitivity multiplier
        :returns: Stick value clamped to [-1.0, 1.0]
        """
        if delta == 0:
            return 0.0

        cfg = self._config
        sign = 1.0 if delta > 0 else -1.0
        raw = abs(delta) * sensitivity / 100.0

        # 1. Apply base power curve
        if raw <= 1.0:
            curved = raw ** cfg.exponent
        else:
            # For values > 1.0, apply curve to the fractional part
            curved = raw ** cfg.exponent

        # 2. Apply S-curve if configured (midpoint != 0.5 or steepness != 0)
        if cfg.midpoint != 0.5 or cfg.steepness != 6.0:
            curved = self._apply_s_curve(curved, cfg.midpoint, cfg.steepness)

        # 3. Apply velocity-based scaling (ballistic component)
        if cfg.velocity_scale > 0:
            velocity = self._calculate_velocity(abs(delta))
            velocity_factor = 1.0 + (velocity * cfg.velocity_scale)
            curved *= velocity_factor

        # 4. Apply acceleration at high speeds
        if cfg.acceleration > 0 and raw > 0.5:
            accel_factor = 1.0 + ((raw - 0.5) * cfg.acceleration)
            curved *= accel_factor

        # 5. Apply output limits
        if cfg.min_output > 0 and curved > 0 and curved < cfg.min_output:
            curved = cfg.min_output

        curved = min(curved, cfg.max_output)

        # 6. Final clamp to [-1.0, 1.0]
        return sign * min(1.0, curved)

    def apply_pair(self, dx: float, dy: float, sensitivity: float = 15.0) -> tuple[float, float]:
        """Apply curve to both X and Y deltas simultaneously.

        Uses the combined magnitude for velocity calculation,
        which gives more natural diagonal movement.

        :param dx: Mouse delta X
        :param dy: Mouse delta Y
        :param sensitivity: Sensitivity multiplier
        :returns: Tuple (stick_x, stick_y) each in [-1.0, 1.0]
        """
        # Calculate combined velocity from both axes
        magnitude = math.sqrt(dx * dx + dy * dy)
        if magnitude == 0:
            return (0.0, 0.0)

        # Update velocity based on combined magnitude
        if self._config.velocity_scale > 0:
            self._calculate_velocity(magnitude)

        # Apply curve to each axis independently
        stick_x = self.apply(dx, sensitivity)
        stick_y = self.apply(dy, sensitivity)

        return (stick_x, stick_y)

    def reset(self):
        """Reset velocity history (call when mouse stops or session resets)."""
        self._velocity_history.clear()
        self._last_velocity = 0.0

    def _apply_s_curve(self, value: float, midpoint: float, steepness: float) -> float:
        """Apply sigmoid/S-curve transformation.

        Creates a smooth curve that's slow at the start, fast in the middle,
        and slow again at the end. The midpoint controls where the fast
        section starts, and steepness controls how sharp the transition is.
        """
        if value <= 0:
            return 0.0
        if value >= 1.0:
            return 1.0

        # Logistic sigmoid centered at midpoint
        x = (value - midpoint) * steepness
        sigmoid = 1.0 / (1.0 + math.exp(-x))

        # Normalize so output is 0-1 for input 0-1
        min_sig = 1.0 / (1.0 + math.exp(midpoint * steepness))
        max_sig = 1.0 / (1.0 + math.exp(-(1.0 - midpoint) * steepness))
        normalized = (sigmoid - min_sig) / (max_sig - min_sig)

        return max(0.0, min(1.0, normalized))

    def _calculate_velocity(self, current_delta: float) -> float:
        """Calculate smoothed velocity from recent deltas.

        Returns a normalized velocity value (0.0 = stationary, 1.0+ = fast).
        Uses exponential moving average for smooth velocity tracking.
        """
        cfg = self._config

        # Add to history
        self._velocity_history.append(current_delta)
        if len(self._velocity_history) > cfg.smoothing_samples:
            self._velocity_history.pop(0)

        # Calculate average velocity
        if not self._velocity_history:
            return 0.0

        avg_velocity = sum(self._velocity_history) / len(self._velocity_history)

        # Normalize (10 pixels/sample = velocity 1.0)
        normalized = avg_velocity / 10.0

        # Apply decay
        self._last_velocity = (
            self._last_velocity * cfg.velocity_decay +
            normalized * (1.0 - cfg.velocity_decay)
        )

        return self._last_velocity

    @property
    def config(self) -> CurveConfig:
        """Return current curve config."""
        return self._config

    @config.setter
    def config(self, value: CurveConfig):
        """Set curve config and reset state."""
        self._config = value
        self.reset()

    @property
    def velocity(self) -> float:
        """Return current calculated velocity."""
        return self._last_velocity


# ─── Convenience functions ────────────────────────────────────────────────────

def list_presets() -> dict[str, str]:
    """Return dict of preset names and descriptions."""
    return {
        "linear": "Direct 1:1 mapping. Simple, can feel twitchy at high sens.",
        "exponential": "Slow at small movements, fast at large. Good precision.",
        "s_curve": "Slow→fast→slow response. Natural feel for most games.",
        "ballistic": "Velocity-dependent. Slow=precision, fast=quick turns. XIM-style.",
        "sniper": "Very slow, maximum precision. For long-range/scoped.",
        "aggressive": "Very fast response. For close-range, shotgun play.",
        "apex": "Mimics XIM APEX default. Balanced for competitive FPS.",
    }
