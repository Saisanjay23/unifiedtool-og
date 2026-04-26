"""
Device fingerprint management for stealth browser sessions.
Maintains persistent device profiles per platform to ensure
consistent fingerprints across scraping sessions.
Also provides JavaScript injection scripts for canvas, WebGL, and audio spoofing.
"""

import json
import os
import random

from backend.core.config import settings
from backend.core.fs import atomic_write_json
from backend.core.logger import get_logger

logger = get_logger("stealth.fingerprint")


# Realistic device profiles sourced from common browser/OS combos
DEVICE_PROFILES = [
    {
        "name": "Win11_Chrome134",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/134.0.0.0 Safari/537.36"
        ),
        "screen_width": 1920,
        "screen_height": 1080,
        "device_memory": 8,
        "hardware_concurrency": 8,
        "gpu_vendor": "Google Inc. (Intel)",
        "gpu_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "platform": "Win32",
        "sec_ch_ua": '"Chromium";v="134", "Google Chrome";v="134", "Not:A-Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_mobile": "?0",
    },
    {
        "name": "Win10_Chrome133",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/133.0.0.0 Safari/537.36"
        ),
        "screen_width": 1366,
        "screen_height": 768,
        "device_memory": 4,
        "hardware_concurrency": 4,
        "gpu_vendor": "Google Inc. (Intel)",
        "gpu_renderer": "ANGLE (Intel, Intel(R) HD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "platform": "Win32",
        "sec_ch_ua": '"Chromium";v="133", "Google Chrome";v="133", "Not:A-Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_mobile": "?0",
    },
    {
        "name": "Win11_Chrome129",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Safari/537.36"
        ),
        "screen_width": 1536,
        "screen_height": 864,
        "device_memory": 16,
        "hardware_concurrency": 8,
        "gpu_vendor": "Google Inc. (NVIDIA)",
        "gpu_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "platform": "Win32",
        "sec_ch_ua": '"Chromium";v="129", "Google Chrome";v="129", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_mobile": "?0",
    },
    {
        "name": "Mac_Chrome131",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "screen_width": 2560,
        "screen_height": 1600,
        "device_memory": 16,
        "hardware_concurrency": 8,
        "gpu_vendor": "Google Inc. (Apple)",
        "gpu_renderer": "ANGLE (Apple, Apple M2, OpenGL 4.1)",
        "platform": "MacIntel",
        "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"macOS"',
        "sec_ch_ua_mobile": "?0",
    },
    {
        "name": "Win11_Chrome128",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "screen_width": 1440,
        "screen_height": 900,
        "device_memory": 8,
        "hardware_concurrency": 4,
        "gpu_vendor": "Google Inc. (AMD)",
        "gpu_renderer": "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "platform": "Win32",
        "sec_ch_ua": '"Chromium";v="128", "Google Chrome";v="128", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_mobile": "?0",
    },
    {
        "name": "Win10_Chrome127",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.0 Safari/537.36"
        ),
        "screen_width": 1680,
        "screen_height": 1050,
        "device_memory": 8,
        "hardware_concurrency": 8,
        "gpu_vendor": "Google Inc. (Intel)",
        "gpu_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "platform": "Win32",
        "sec_ch_ua": '"Chromium";v="127", "Google Chrome";v="127", "Not_A Brand";v="99"',
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_mobile": "?0",
    },
]


class DeviceProfileManager:
    """
    Manages persistent device fingerprints per platform.
    First run: picks a random device profile and saves it.
    Subsequent runs: loads the same profile for consistency.
    """

    def __init__(self, platform: str):
        self.platform = platform
        self._profile: dict | None = None
        self._profile_path = os.path.join(
            settings.SESSION_PATH, f"{platform}_device.json"
        )

    def get_profile(self) -> dict:
        """Load or create the device profile for this platform."""
        if self._profile is not None:
            return self._profile

        os.makedirs(settings.SESSION_PATH, exist_ok=True)

        if os.path.exists(self._profile_path):
            try:
                with open(self._profile_path, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("Profile is not a dict")
                self._profile = data
                logger.info(
                    f"{self.platform}: Loaded device profile '{self._profile.get('name', 'Unknown')}'"
                )
                return self._profile
            except Exception as exc:
                logger.warning(
                    f"{self.platform}: Corrupt device profile, regenerating: {exc}"
                )

        # pick a random profile and persist it
        self._profile = random.choice(DEVICE_PROFILES).copy()
        self._save_profile()
        logger.info(
            f"{self.platform}: Created new device profile '{self._profile['name']}'"
        )
        return self._profile

    def _save_profile(self):
        atomic_write_json(self._profile_path, self._profile, indent=2)

    def get_viewport(self) -> dict:
        """Return viewport dimensions from the device profile."""
        profile = self.get_profile()
        return {
            "width": profile["screen_width"],
            "height": profile["screen_height"],
        }

    def get_user_agent(self) -> str:
        return self.get_profile()["user_agent"]


def get_canvas_noise_script() -> str:
    """
    JavaScript that adds imperceptible noise to canvas toDataURL output.
    This beats canvas fingerprinting services like FingerprintJS.
    """
    return """
    (function() {
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type) {
            const ctx = this.getContext('2d');
            if (ctx) {
                const imageData = ctx.getImageData(0, 0, this.width, this.height);
                const data = imageData.data;
                // add tiny noise to a few random pixels
                for (let i = 0; i < Math.min(10, data.length / 4); i++) {
                    const idx = Math.floor(Math.random() * (data.length / 4)) * 4;
                    data[idx] = data[idx] ^ 1;     // flip least significant bit
                }
                ctx.putImageData(imageData, 0, 0);
            }
            return origToDataURL.apply(this, arguments);
        };
    })();
    """


def get_webgl_spoof_script(profile: dict) -> str:
    """
    JavaScript that overrides WebGL renderer/vendor to match the device profile.
    """
    vendor = profile.get("gpu_vendor", "Google Inc. (Intel)")
    renderer = profile.get("gpu_renderer", "ANGLE (Intel, Intel(R) UHD Graphics 630)")
    return f"""
    (function() {{
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {{
            if (param === 37445) return '{vendor}';   // UNMASKED_VENDOR_WEBGL
            if (param === 37446) return '{renderer}';  // UNMASKED_RENDERER_WEBGL
            return getParameter.apply(this, arguments);
        }};
        // also patch WebGL2
        if (typeof WebGL2RenderingContext !== 'undefined') {{
            const getParam2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {{
                if (param === 37445) return '{vendor}';
                if (param === 37446) return '{renderer}';
                return getParam2.apply(this, arguments);
            }};
        }}
    }})();
    """


def get_audio_noise_script() -> str:
    """
    JavaScript that adds imperceptible noise to AudioContext output.
    Defeats audio-based fingerprinting.
    """
    return """
    (function() {
        const origGetChannelData = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = function(channel) {
            const data = origGetChannelData.apply(this, arguments);
            // add negligible noise to the first 100 samples
            for (let i = 0; i < Math.min(100, data.length); i++) {
                data[i] += (Math.random() - 0.5) * 0.0001;
            }
            return data;
        };
    })();
    """


def get_navigator_override_script(profile: dict) -> str:
    """
    JavaScript that patches navigator properties to match the device profile.
    Also adds a believable window.chrome object for headless detection bypass.
    """
    memory = profile.get("device_memory", 8)
    cores = profile.get("hardware_concurrency", 8)
    plat = profile.get("platform", "Win32")
    return f"""
    (function() {{
        // remove webdriver flag
        Object.defineProperty(navigator, 'webdriver', {{
            get: () => undefined,
        }});

        // device specs
        Object.defineProperty(navigator, 'hardwareConcurrency', {{
            get: () => {cores},
        }});
        Object.defineProperty(navigator, 'deviceMemory', {{
            get: () => {memory},
        }});
        Object.defineProperty(navigator, 'platform', {{
            get: () => '{plat}',
        }});
        Object.defineProperty(navigator, 'languages', {{
            get: () => ['en-US', 'en'],
        }});

        // fake plugins (Chrome typically shows at least 3)
        Object.defineProperty(navigator, 'plugins', {{
            get: () => {{
                const arr = [
                    {{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
                    {{ name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' }},
                    {{ name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }},
                ];
                arr.length = 3;
                return arr;
            }},
        }});

        // fake chrome runtime object
        if (!window.chrome) {{
            window.chrome = {{}};
        }}
        window.chrome.runtime = {{
            connect: function() {{}},
            sendMessage: function() {{}},
        }};
    }})();
    """
