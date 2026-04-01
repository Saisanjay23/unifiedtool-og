"""
Human behavior simulation engine for the Unified Social Media Tool.
All delays use statistical distributions to avoid detectable patterns.
Supports context-aware pacing, fatigue simulation, circadian rhythm,
and realistic mouse/typing/scrolling actions.
"""

import asyncio
import math
import random
import time
from datetime import datetime
from typing import Optional

from backend.core.logger import get_logger

logger = get_logger("stealth.human")


class HumanBehavior:
    """
    Simulates human-like browser interactions.
    Each instance is bound to a platform and tracks session duration
    for fatigue-based timing adjustments.
    """

    # log-normal parameters by action context (mu, sigma in seconds)
    DELAY_PARAMS = {
        "search": (2.1, 0.6),
        "scroll": (0.8, 0.3),
        "read": (3.5, 1.2),
        "type_char": (0.15, 0.05),
        "click": (0.4, 0.15),
        "default": (1.5, 0.5),
        "page_load": (2.0, 0.8),
        "between_profiles": (4.0, 1.5),
    }

    def __init__(self, platform: str, session_start: Optional[datetime] = None):
        self.platform = platform
        self.session_start = session_start or datetime.now()
        self.actions_taken = 0
        self.last_break_at = time.time()

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

        # clamp to reasonable bounds
        delay = max(0.3, min(delay, 60.0))

        self.actions_taken += 1
        await asyncio.sleep(delay)

    def get_fatigue_multiplier(self) -> float:
        """
        Returns 1.0–2.8 based on session duration and number of actions taken.
        Humans slow down over time. This is a logarithmic fatigue curve.
        """
        elapsed_minutes = (datetime.now() - self.session_start).total_seconds() / 60.0

        # time-based fatigue (logarithmic)
        time_factor = 1.0 + min(1.0, 0.3 * math.log1p(elapsed_minutes / 15.0))

        # action-based fatigue (very gradual)
        action_factor = 1.0 + min(0.8, self.actions_taken * 0.002)

        return min(2.8, time_factor * action_factor)

    def get_circadian_multiplier(self) -> float:
        """
        Returns 0.8–1.5 based on local time (IST timezone).
        People browse faster during peak hours (10am-3pm),
        slower in early morning and late night.
        """
        hour = datetime.now().hour

        # peak hours: 10am-3pm (fast browsing)
        if 10 <= hour <= 15:
            return random.uniform(0.8, 1.0)
        # evening: moderate
        elif 16 <= hour <= 22:
            return random.uniform(1.0, 1.2)
        # late night / early morning: slow
        else:
            return random.uniform(1.2, 1.5)

    async def human_type(self, page, selector: str, text: str) -> bool:
        """
        Type text character by character with realistic speed variation.
        Occasionally makes a typo and corrects it.

        Returns True if typing succeeded.
        """
        try:
            element = page.locator(selector)
            await element.click()
            await asyncio.sleep(random.uniform(0.2, 0.5))

            for i, char in enumerate(text):
                # per-character delay (Gaussian around 80ms WPM equivalent)
                delay_ms = max(30, int(random.gauss(80, 25)))

                # 3% chance of typo + correction (only for alphabetic chars)
                if random.random() < 0.03 and char.isalpha():
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
        self, page, direction: str = "down", distance: Optional[int] = None
    ):
        """
        Scroll with physics-based motion — multiple small chunks
        with occasional reverse micro-scrolls and variable pauses.
        """
        try:
            viewport = page.viewport_size or {"width": 1920, "height": 1080}
            vh = viewport["height"]

            if distance is None:
                # random scroll distance: 40-80% of viewport
                distance = int(vh * random.uniform(0.4, 0.8))

            sign = 1 if direction == "down" else -1

            # break scroll into 2-5 chunks
            chunks = random.randint(2, 5)
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
                await asyncio.sleep(random.uniform(0.1, 0.4))

                # 15% chance of a tiny reverse micro-scroll (reading back up)
                if random.random() < 0.15:
                    micro = int(vh * random.uniform(0.02, 0.06))
                    await page.mouse.wheel(0, micro * -sign)
                    await asyncio.sleep(random.uniform(0.2, 0.6))

            # post-scroll reading pause
            await asyncio.sleep(random.uniform(0.5, 1.5))

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
        """
        try:
            viewport = page.viewport_size
            if not viewport:
                return

            for _ in range(count):
                x = random.randint(100, viewport["width"] - 100)
                y = random.randint(100, viewport["height"] - 100)
                steps = random.randint(5, 12)
                await page.mouse.move(x, y, steps=steps)
                await asyncio.sleep(random.uniform(0.15, 0.4))

        except Exception as exc:
            logger.debug(f"mouse_jitter failed: {exc}")

    async def maybe_take_break(self):
        """
        If the session has been active too long or too many actions taken,
        simulate a break.

        Thresholds:
        - >90 minutes session: mandatory 3-8 min break
        - >200 actions without break: 1-3 min break
        """
        now = time.time()
        elapsed_minutes = (now - self.last_break_at) / 60.0

        needs_break = False
        break_duration = 0.0

        if elapsed_minutes > 90:
            needs_break = True
            break_duration = random.uniform(180, 480)  # 3-8 min
            logger.info(
                f"{self.platform}: Taking mandatory session break ({break_duration:.0f}s)"
            )
        elif self.actions_taken > 200 and elapsed_minutes > 30:
            needs_break = True
            break_duration = random.uniform(60, 180)  # 1-3 min
            logger.info(
                f"{self.platform}: Taking action-count break ({break_duration:.0f}s)"
            )

        if needs_break:
            await asyncio.sleep(break_duration)
            self.last_break_at = time.time()
            self.actions_taken = 0

    def reset_session(self):
        """Reset all session tracking (call when starting a new scraping run)."""
        self.session_start = datetime.now()
        self.actions_taken = 0
        self.last_break_at = time.time()
