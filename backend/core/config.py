"""
Configuration management for the Unified Social Media Tool.

Loads settings from environment variables with sensible defaults and typed validation.
"""

import os

from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.runtime import PROJECT_ROOT

ENV_FILE_PATH = PROJECT_ROOT / ".env"


def _project_path(*parts: str) -> str:
    return str(PROJECT_ROOT.joinpath(*parts))


class Settings(BaseSettings):
    """Central configuration loaded from `.env`."""

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 9000
    DEBUG: bool = False
    ALLOWED_CORS_ORIGINS: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:9000,http://127.0.0.1:9000"
    )

    # MongoDB
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB_PREFIX: str = "unified_tool"

    # Platform API keys
    YOUTUBE_API_KEY: str | None = None
    TELEGRAM_API_ID: str | None = None
    TELEGRAM_API_HASH: str | None = None
    TELEGRAM_PHONE: str | None = None

    # Scraping behavior
    MAX_CONCURRENT_PROFILES: int = 3
    MAX_CONCURRENT_JOBS: int = 5
    DEFAULT_HEADLESS: bool = True
    DEFAULT_MAX_RESULTS: int = 50
    REQUEST_TIMEOUT_SEC: int = 30
    PAGE_NAVIGATION_TIMEOUT_MS: int = 60000
    ELEMENT_WAIT_TIMEOUT_MS: int = 15000

    # Discovery speed mode: "stealth" (safest), "balanced" (recommended), "aggressive" (fastest)
    # stealth   = full human simulation, conservative delays (original behavior)
    # balanced  = reduced delays, keep core anti-detection (2-3x faster)
    # aggressive = minimum viable delays, skip jitter/breaks (4-6x faster, higher risk)
    DISCOVERY_SPEED_MODE: str = "balanced"

    # Optional browser proxy, for example: http://user:pass@proxy:8080
    PROXY_URL: str | None = None

    # Multiple proxies for rotation (comma-separated).
    # Example: "socks5://p1:1080,http://user:pass@p2:8080,socks5://p3:1080"
    # When set, overrides PROXY_URL with round-robin rotation.
    PROXY_URLS: str | None = None

    # Rate limits per hour per platform
    RATE_LIMIT_FACEBOOK: int = 60
    RATE_LIMIT_INSTAGRAM: int = 100
    RATE_LIMIT_TWITTER: int = 80
    RATE_LIMIT_YOUTUBE: int = 100
    RATE_LIMIT_TELEGRAM: int = 80
    RATE_LIMIT_TIKTOK: int = 80

    # Analysis performance tuning
    ANALYSIS_CONCURRENT_TABS: int = 3
    ANALYSIS_INTER_PROFILE_DELAY: float = 1.5
    ANALYSIS_PAGE_TIMEOUT_MS: int = 20000
    ANALYSIS_SKIP_MOBILE_FALLBACK: bool = True
    ANALYSIS_API_CONCURRENT_TABS: int = 6
    ANALYSIS_API_INTER_PROFILE_DELAY: float = 0.0

    # Health thresholds
    HEALTH_DEGRADED_THRESHOLD: float = 0.7
    HEALTH_CRITICAL_THRESHOLD: float = 0.4
    HEALTH_SUSPENSION_THRESHOLD: float = 0.2

    # Cron scheduling
    CRON_ENABLED: bool = False
    CRON_JOBS_FILE: str = _project_path("cron_jobs.json")

    # Paths resolved against the project root
    SESSION_PATH: str = _project_path("sessions")
    LOG_PATH: str = _project_path("logs")

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def get_rate_limit(self, platform: str) -> int:
        """Return the hourly rate limit for a given platform name."""
        limits = {
            "facebook": self.RATE_LIMIT_FACEBOOK,
            "instagram": self.RATE_LIMIT_INSTAGRAM,
            "twitter": self.RATE_LIMIT_TWITTER,
            "youtube": self.RATE_LIMIT_YOUTUBE,
            "telegram": self.RATE_LIMIT_TELEGRAM,
            "tiktok": self.RATE_LIMIT_TIKTOK,
        }
        return limits.get(platform.lower(), 50)

    def get_db_name(self, platform: str) -> str:
        """Return the MongoDB database name for a given platform."""
        return f"{self.MONGO_DB_PREFIX}_{platform.lower()}"

    def ensure_directories(self):
        """Create required directories if they do not exist."""
        for path in [self.SESSION_PATH, self.LOG_PATH]:
            os.makedirs(path, exist_ok=True)

    def get_proxy_rotator(self):
        """Lazily create and return a ProxyRotator from PROXY_URLS.
        Returns None if PROXY_URLS is not configured."""
        if not hasattr(self, '_proxy_rotator'):
            self._proxy_rotator = None
            if self.PROXY_URLS:
                try:
                    from backend.stealth.proxy import ProxyRotator
                    proxies = [p.strip() for p in self.PROXY_URLS.split(",") if p.strip()]
                    if proxies:
                        self._proxy_rotator = ProxyRotator(proxies)
                except Exception as e:
                    import logging
                    logging.getLogger("core.config").warning(f"Failed to create ProxyRotator: {e}")
        return self._proxy_rotator


settings = Settings()
