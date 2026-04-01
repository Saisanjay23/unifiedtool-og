"""
Configuration management for the Unified Social Media Tool.
Loads all settings from environment variables with sensible defaults.
Uses pydantic-settings for typed validation.
"""

from pydantic_settings import BaseSettings
from typing import Optional
import os


class Settings(BaseSettings):
    """
    Central configuration loaded from .env file.
    Every configurable value in the system flows through here.
    """

    # ---- Server ----
    HOST: str = "0.0.0.0"
    PORT: int = 9000
    DEBUG: bool = False
    ALLOWED_CORS_ORIGINS: str = (
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000"
    )

    # ---- MongoDB ----
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB_PREFIX: str = "unified_tool"

    # ---- Platform API Keys ----
    YOUTUBE_API_KEY: Optional[str] = None
    TELEGRAM_API_ID: Optional[str] = None
    TELEGRAM_API_HASH: Optional[str] = None
    TELEGRAM_PHONE: Optional[str] = None

    # ---- Scraping behavior ----
    MAX_CONCURRENT_PROFILES: int = 3
    MAX_CONCURRENT_JOBS: int = 5
    DEFAULT_HEADLESS: bool = True
    DEFAULT_MAX_RESULTS: int = 50
    REQUEST_TIMEOUT_SEC: int = 30
    PAGE_NAVIGATION_TIMEOUT_MS: int = 60000  # Max wait for page.goto()
    ELEMENT_WAIT_TIMEOUT_MS: int = 15000  # Max wait for element selectors

    # ---- Proxy (optional — routes browser traffic through VPN/proxy) ----
    PROXY_URL: Optional[str] = None  # e.g. "http://user:pass@proxy:8080" or "socks5://proxy:1080"

    # ---- Rate limits (per hour per platform) ----
    RATE_LIMIT_FACEBOOK: int = 30
    RATE_LIMIT_INSTAGRAM: int = 60
    RATE_LIMIT_TWITTER: int = 40
    RATE_LIMIT_YOUTUBE: int = 100
    RATE_LIMIT_TELEGRAM: int = 80
    RATE_LIMIT_TIKTOK: int = 60

    # ---- Health thresholds ----
    HEALTH_DEGRADED_THRESHOLD: float = 0.7
    HEALTH_CRITICAL_THRESHOLD: float = 0.4
    HEALTH_SUSPENSION_THRESHOLD: float = 0.2

    # ---- Cron scheduling ----
    CRON_ENABLED: bool = False
    CRON_JOBS_FILE: str = "cron_jobs.json"

    # ---- Paths (relative to project root) ----
    SESSION_PATH: str = "sessions"
    LOG_PATH: str = "logs"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

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
        """Create required directories if they don't exist."""
        for path in [self.SESSION_PATH, self.LOG_PATH]:
            os.makedirs(path, exist_ok=True)


# Module-level singleton — import this everywhere
settings = Settings()
