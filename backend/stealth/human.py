"""
Human behavior simulation engine for the Unified Social Media Tool.
All delays use statistical distributions to avoid detectable patterns.
Supports context-aware pacing, fatigue simulation, circadian rhythm,
and realistic mouse/typing/scrolling actions.

Speed-mode-aware: delay parameters, fatigue caps, and break thresholds
adjust automatically based on `DISCOVERY_SPEED_MODE` in config.
"""

import asyncio
import math
import random
import time
from datetime import datetime

from backend.core.logger import get_logger

logger = get_logger("stealth.human")


# ─── Speed-Mode Delay Profiles ────────────────────────────────────────────────
# Each profile defines (mu, sigma) for log-normal delay distributions.
# "stealth" = original conservative behavior (safest, slowest)
# "balanced" = reduced delays, still maintains anti-pattern variability
# "aggressive" = minimum viable delays for maximum speed (higher ban risk)

_DELAY_PROFILES = {
    "stealth": {
        "search": (2.1, 0.6),
        "scroll": (0.8, 0.3),
        "read": (3.5, 1.2),
        "type_char": (0.15, 0.05),
        "click": (0.4, 0.15),
        "default": (1.5, 0.5),
        "page_load": (2.0, 0.8),
        "between_profiles": (4.0, 1.5),
    },
    "balanced": {
        "search": (1.0, 0.3),
        "scroll": (0.4, 0.15),
        "read": (1.5, 0.5),
        "type_char": (0.10, 0.03),
        "click": (0.2, 0.08),
        "default": (0.7, 0.25),
        "page_load": (1.0, 0.4),
        "between_profiles": (1.5, 0.5),
    },
    "aggressive": {
        "search": (0.5, 0.15),
        "scroll": (0.2, 0.1),
        "read": (0.5, 0.2),
        "type_char": (0.06, 0.02),
        "click": (0.1, 0.05),
        "default": (0.3, 0.1),
        "page_load": (0.5, 0.2),
        "between_profiles": (0.5, 0.2),
    },
}

# Speed-mode caps for fatigue and circadian multipliers
_FATIGUE_CAPS = {
    "stealth": 2.8,
    "balanced": 1.3,
    "aggressive": 1.0,  # disabled
}

_CIRCADIAN_RANGES = {
    "stealth": {
        "peak": (0.8, 1.0),       # 10am-3pm
        "evening": (1.0, 1.2),    # 4pm-10pm
        "night": (1.2, 1.5),      # 11pm-9am
    },
    "balanced": {
        "peak": (0.9, 1.0),
        "evening": (0.95, 1.05),
        "night": (1.0, 1.1),
    },
    "aggressive": {
        "peak": (1.0, 1.0),       # disabled — always 1.0
        "evening": (1.0, 1.0),
        "night": (1.0, 1.0),
    },
}

# Break thresholds per speed mode
_BREAK_CONFIG = {
    "stealth": {
        "time_break_minutes": 90,       # mandatory break after 90 min
        "time_break_secs": (180, 480),  # 3-8 min break
        "action_break_count": 200,      # break after 200 actions
        "action_break_min_minutes": 30, # ...only if session > 30 min
        "action_break_secs": (60, 180), # 1-3 min break
    },
    "balanced": {
        # No mandatory breaks in balanced mode — we rely on inter-scroll
        # delays and health monitoring instead.
        "time_break_minutes": 9999,
        "time_break_secs": (0, 0),
        "action_break_count": 9999,
        "action_break_min_minutes": 9999,
        "action_break_secs": (0, 0),
    },
    "aggressive": {
        "time_break_minutes": 9999,
        "time_break_secs": (0, 0),
        "action_break_count": 9999,
        "action_break_min_minutes": 9999,
        "action_break_secs": (0, 0),
    },
}

# Scroll behavior tuning per speed mode
_SCROLL_CONFIG = {
    "stealth": {
        "chunks_range": (2, 5),
        "chunk_pause": (0.1, 0.4),
        "reverse_probability": 0.15,
        "reverse_pause": (0.2, 0.6),
        "post_scroll_pause": (0.5, 1.5),
    },
    "balanced": {
        "chunks_range": (1, 3),
        "chunk_pause": (0.05, 0.15),
        "reverse_probability": 0.05,
        "reverse_pause": (0.1, 0.2),
        "post_scroll_pause": (0.15, 0.4),
    },
    "aggressive": {
        "chunks_range": (1, 2),
        "chunk_pause": (0.02, 0.08),
        "reverse_probability": 0.0,     # no reverse scrolls
        "reverse_pause": (0, 0),
        "post_scroll_pause": (0.05, 0.15),
    },
}

# Mouse jitter tuning per speed mode
_JITTER_CONFIG = {
    "stealth": {"move_pause": (0.15, 0.4), "steps_range": (5, 12)},
    "balanced": {"move_pause": (0.05, 0.15), "steps_range": (3, 6)},
    "aggressive": {"move_pause": (0.02, 0.05), "steps_range": (2, 4)},
}

# Typing: typo probability per speed mode
_TYPO_PROBABILITY = {
    "stealth": 0.03,
    "balanced": 0.0,      # no typos — faster typing
    "aggressive": 0.0,
}


def _get_speed_mode() -> str:
    """Read the current speed mode from config. Falls back to 'balanced'."""
    try:
        from backend.core.config import settings
        mode = settings.DISCOVERY_SPEED_MODE.lower()
        if mode in _DELAY_PROFILES:
            return mode
        logger.warning(f"Unknown speed mode '{mode}', falling back to 'balanced'")
    except Exception:
        pass
    return "balanced"


class HumanBehavior:
    """
    Simulates human-like browser interactions.
    Each instance is bound to a platform and tracks session duration
    for fatigue-based timing adjustments.

    Speed-mode-aware: reads DISCOVERY_SPEED_MODE from config to adjust
    all delay parameters, fatigue/circadian multipliers, and break thresholds.
    """

    def __init__(self, platform: str, session_start: datetime | None = None):
        self.platform = platform
        self.session_start = session_start or datetime.now()
        self.actions_taken = 0
        self.last_break_at = time.time()

        # Resolve speed mode once at construction time
        self.speed_mode = _get_speed_mode()
        self.DELAY_PARAMS = _DELAY_PROFILES[self.speed_mode]

        logger.info(
            f"{platform}: HumanBehavior initialized in '{self.speed_mode}' mode"
        )

    async def pause(self, context: str = "default"):
        """
        Context-aware delay using log-normal distribution.
        The delay is further modulated by fatigue and circadian multipliers.
        """
        mu, sigma = self.DELAY_PARAMS.get(context, self.DELAY_PARAMS["default"])

        # draw from log-normal distribution
        raw_delay = random.lognormvariate(math.log(mu), sigma / mu)

        # apply fatigue and time-of-day adjustments
        delay = (
            raw_delay * self.get_fatigue_multiplier() * self.get_circadian_multiplier()
        )

        # clamp to reasonable bounds (lower bound varies by speed mode)
        min_delay = 0.1 if self.speed_mode == "aggressive" else 0.2 if self.speed_mode == "balanced" else 0.3
        delay = max(min_delay, min(delay, 60.0))

        self.actions_taken += 1
        await asyncio.sleep(delay)

    def get_fatigue_multiplier(self) -> float:
        """
        Returns 1.0–cap based on session duration and number of actions taken.
        Humans slow down over time. This is a logarithmic fatigue curve.
        Cap is determined by speed mode.
        """
        cap = _FATIGUE_CAPS[self.speed_mode]
        if cap <= 1.0:
            return 1.0  # aggressive: fatigue disabled

        elapsed_minutes = (datetime.now() - self.session_start).total_seconds() / 60.0

        # time-based fatigue (logarithmic)
        time_factor = 1.0 + min(1.0, 0.3 * math.log1p(elapsed_minutes / 15.0))

        # action-based fatigue (very gradual)
        action_factor = 1.0 + min(0.8, self.actions_taken * 0.002)

        return min(cap, time_factor * action_factor)

    def get_circadian_multiplier(self) -> float:
        """
        Returns a time-of-day multiplier for delay adjustment.
        People browse faster during peak hours (10am-3pm),
        slower in early morning and late night.
        Range is determined by speed mode.
        """
        ranges = _CIRCADIAN_RANGES[self.speed_mode]
        hour = datetime.now().hour

        # peak hours: 10am-3pm (fast browsing)
        if 10 <= hour <= 15:
            return random.uniform(*ranges["peak"])
        # evening: moderate
        elif 16 <= hour <= 22:
            return random.uniform(*ranges["evening"])
        # late night / early morning: slow
        else:
            return random.uniform(*ranges["night"])

    async def human_type(self, page, selector: str, text: str) -> bool:
        """
        Type text character by character with realistic speed variation.
        In stealth mode, occasionally makes a typo and corrects it.
        In balanced/aggressive mode, typos are disabled for speed.

        Returns True if typing succeeded.
        """
        typo_prob = _TYPO_PROBABILITY[self.speed_mode]

        try:
            element = page.locator(selector)
            await element.click()
            await asyncio.sleep(random.uniform(0.1, 0.3))

            for i, char in enumerate(text):
                # per-character delay (Gaussian around 80ms WPM equivalent)
                delay_ms = max(30, int(random.gauss(80, 25)))

                # typo + correction (only in stealth mode)
                if typo_prob > 0 and random.random() < typo_prob and char.isalpha():
                    # type wrong char, wait, backspace, then correct
                    wrong = random.choice("qwertyuiopasdfghjklzxcvbnm")
                    await element.type(wrong, delay=delay_ms)
                    await asyncio.sleep(random.uniform(0.15, 0.4))
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(random.uniform(0.1, 0.25))

                await element.type(char, delay=delay_ms)

                # occasional micro-pause (every 5-8 chars, simulates thinking)
                if i > 0 and i % random.randint(5, 8) == 0:
                    await asyncio.sleep(random.uniform(0.3, 0.8))

            return True

        except Exception as exc:
            logger.warning(f"human_type failed on '{selector}': {exc}")
            return False

    async def human_scroll(
        self, page, direction: str = "down", distance: int | None = None
    ):
        """
        Scroll with physics-based motion — multiple small chunks
        with occasional reverse micro-scrolls and variable pauses.
        Scroll speed and complexity vary by speed mode.
        """
        cfg = _SCROLL_CONFIG[self.speed_mode]

        try:
            viewport = page.viewport_size or {"width": 1920, "height": 1080}
            vh = viewport["height"]

            if distance is None:
                # random scroll distance: 40-80% of viewport
                distance = int(vh * random.uniform(0.4, 0.8))

            sign = 1 if direction == "down" else -1

            # break scroll into chunks (fewer in faster modes)
            chunks = random.randint(*cfg["chunks_range"])
            remaining = distance

            for i in range(chunks):
                if remaining <= 0:
                    break

                chunk = min(
                    remaining, int(distance / chunks * random.uniform(0.7, 1.3))
                )
                await page.mouse.wheel(0, chunk * sign)
                remaining -= chunk

                # small pause between chunks
                if cfg["chunk_pause"][1] > 0:
                    await asyncio.sleep(random.uniform(*cfg["chunk_pause"]))

                # occasional reverse micro-scroll (reading back up)
                if cfg["reverse_probability"] > 0 and random.random() < cfg["reverse_probability"]:
                    micro = int(vh * random.uniform(0.02, 0.06))
                    await page.mouse.wheel(0, micro * -sign)
                    if cfg["reverse_pause"][1] > 0:
                        await asyncio.sleep(random.uniform(*cfg["reverse_pause"]))

            # post-scroll reading pause
            if cfg["post_scroll_pause"][1] > 0:
                await asyncio.sleep(random.uniform(*cfg["post_scroll_pause"]))

        except Exception as exc:
            logger.warning(f"human_scroll failed: {exc}")

    async def human_click(self, page, selector: str) -> bool:
        """
        Click an element with coordinate jitter using a Gaussian distribution
        centered on the element's bounding box center.
        """
        try:
            element = page.locator(selector).first
            bbox = await element.bounding_box()
            if bbox is None:
                # fallback: normal click without jitter
                await element.click()
                return True

            # target center with Gaussian jitter (±8px)
            center_x = bbox["x"] + bbox["width"] / 2
            center_y = bbox["y"] + bbox["height"] / 2
            jitter_x = random.gauss(0, 3)
            jitter_y = random.gauss(0, 3)
            target_x = center_x + jitter_x
            target_y = center_y + jitter_y

            # clamp to bounding box
            target_x = max(bbox["x"] + 2, min(target_x, bbox["x"] + bbox["width"] - 2))
            target_y = max(bbox["y"] + 2, min(target_y, bbox["y"] + bbox["height"] - 2))

            # move then click
            await self.human_move(page, int(target_x), int(target_y))
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await page.mouse.click(target_x, target_y)
            return True

        except Exception as exc:
            logger.warning(f"human_click failed on '{selector}': {exc}")
            return False

    async def human_move(self, page, x: int, y: int):
        """
        Move the mouse along a gentle curved path using quadratic Bezier interpolation.
        Not a straight line — real humans don't move mice in straight lines.
        """
        try:
            # get approximate current position (center of viewport as default)
            viewport = page.viewport_size or {"width": 1920, "height": 1080}
            start_x = random.randint(
                int(viewport["width"] * 0.3), int(viewport["width"] * 0.7)
            )
            start_y = random.randint(
                int(viewport["height"] * 0.3), int(viewport["height"] * 0.7)
            )

            # control point for the bezier curve (offset to one side)
            ctrl_x = (start_x + x) / 2 + random.randint(-100, 100)
            ctrl_y = (start_y + y) / 2 + random.randint(-80, 80)

            # interpolate along the curve in 8-15 steps
            steps = random.randint(8, 15)
            for i in range(1, steps + 1):
                t = i / steps

                # quadratic bezier formula
                bx = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t**2 * x
                by = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t**2 * y

                await page.mouse.move(bx, by)
                await asyncio.sleep(random.uniform(0.01, 0.04))

        except Exception as exc:
            logger.debug(f"human_move interpolation failed: {exc}")

    async def mouse_jitter(self, page, count: int = 3):
        """
        Small random mouse movements to simulate idle reading.
        Movement speed varies by speed mode.
        """
        cfg = _JITTER_CONFIG[self.speed_mode]

        try:
            viewport = page.viewport_size
            if not viewport:
                return

            for _ in range(count):
                x = random.randint(100, viewport["width"] - 100)
                y = random.randint(100, viewport["height"] - 100)
                steps = random.randint(*cfg["steps_range"])
                await page.mouse.move(x, y, steps=steps)
                await asyncio.sleep(random.uniform(*cfg["move_pause"]))

        except Exception as exc:
            logger.debug(f"mouse_jitter failed: {exc}")

    async def maybe_take_break(self):
        """
        If the session has been active too long or too many actions taken,
        simulate a break.

        In balanced/aggressive mode, breaks are disabled — we rely on
        inter-scroll delays and health monitoring instead.
        """
        cfg = _BREAK_CONFIG[self.speed_mode]
        now = time.time()
        elapsed_minutes = (now - self.last_break_at) / 60.0

        needs_break = False
        break_duration = 0.0

        if elapsed_minutes > cfg["time_break_minutes"]:
            needs_break = True
            low, high = cfg["time_break_secs"]
            if high > 0:
                break_duration = random.uniform(low, high)
            logger.info(
                f"{self.platform}: Taking mandatory session break ({break_duration:.0f}s)"
            )
        elif (
            self.actions_taken > cfg["action_break_count"]
            and elapsed_minutes > cfg["action_break_min_minutes"]
        ):
            needs_break = True
            low, high = cfg["action_break_secs"]
            if high > 0:
                break_duration = random.uniform(low, high)
            logger.info(
                f"{self.platform}: Taking action-count break ({break_duration:.0f}s)"
            )

        if needs_break and break_duration > 0:
            await asyncio.sleep(break_duration)
            self.last_break_at = time.time()
            self.actions_taken = 0

    def reset_session(self):
        """Reset all session tracking (call when starting a new scraping run)."""
        self.session_start = datetime.now()
        self.actions_taken = 0
        self.last_break_at = time.time()
