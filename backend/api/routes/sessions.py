"""
Session management API routes.
Handles browser session login for each platform.
"""

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from backend.core.config import ENV_FILE_PATH, settings
from backend.core.fs import atomic_write_json, atomic_write_text
from backend.core.health import HealthManager
from backend.core.logger import get_logger
from backend.core.session_validator import SessionValidator

router = APIRouter(tags=["sessions"])
logger = get_logger("api.sessions")

# platform-specific login URLs and verification cookies
PLATFORM_LOGIN_CONFIG = {
    "facebook": {
        "url": "https://www.facebook.com/login",
        "verify_cookie": "c_user",
    },
    "instagram": {
        "url": "https://www.instagram.com/accounts/login/",
        "verify_cookie": "sessionid",
    },
    "twitter": {
        "url": "https://twitter.com/i/flow/login",
        "verify_cookie": "auth_token",
    },
    "youtube": {
        "url": "https://accounts.google.com/signin",
        "verify_cookie": "SID",
    },
    "telegram": {
        "url": None,  # Telegram uses API-based auth, not browser login
        "verify_cookie": None,
    },
    "tiktok": {
        "url": "https://www.tiktok.com/login",
        "verify_cookie": None,  # TikTok uses various session cookies; accept any imported cookies
    },
}

# track active login sessions (in-progress login attempts)
_active_logins: dict[str, dict] = {}
_ENV_FILE_LOCK = threading.Lock()


def _load_env_lines() -> list[str]:
    env_file = Path(ENV_FILE_PATH)
    if not env_file.exists():
        return []
    with env_file.open("r", encoding="utf-8") as handle:
        return handle.readlines()


def _write_env_lines(lines: list[str]) -> None:
    atomic_write_text(ENV_FILE_PATH, "".join(lines))


def _set_env_key(key: str, value: str) -> None:
    """Upsert a key in the project .env file using an atomic replace."""
    with _ENV_FILE_LOCK:
        lines = _load_env_lines()
        found = False
        updated_lines: list[str] = []

        for line in lines:
            if line.startswith(f"{key}="):
                updated_lines.append(f"{key}={value}\n")
                found = True
            else:
                updated_lines.append(line)

        if not found:
            updated_lines.append(f"{key}={value}\n")

        _write_env_lines(updated_lines)


def _remove_env_key(key: str) -> None:
    """Blank out a key in the project .env file so it won't persist across restarts."""
    with _ENV_FILE_LOCK:
        lines = _load_env_lines()
        if not lines:
            return

        updated_lines = []
        for line in lines:
            if line.startswith(f"{key}="):
                updated_lines.append(f"{key}=\n")
            else:
                updated_lines.append(line)

        _write_env_lines(updated_lines)


def _delete_file_if_exists(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(f"Failed to delete {path}: {exc}")
        return False


@router.post("/sessions/{platform}/launch")
async def launch_login(platform: str):
    """
    Open a visible browser for the user to log in to a platform.
    For Telegram, this is handled differently (API-based auth).
    """
    if platform not in PLATFORM_LOGIN_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    config = PLATFORM_LOGIN_CONFIG[platform]

    if platform == "telegram":
        raise HTTPException(
            status_code=400,
            detail="Telegram uses API-based auth. Configure TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_PHONE in .env",
        )

    if platform in _active_logins and _active_logins[platform].get("in_progress"):
        return {
            "platform": platform,
            "status": "already_in_progress",
            "message": "A login browser is already open for this platform",
        }

    _active_logins[platform] = {
        "in_progress": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    # launch the login browser in a background task
    asyncio.create_task(_run_login(platform, config))

    return {
        "platform": platform,
        "status": "launched",
        "message": "Login browser opened. Please log in and wait for confirmation.",
    }


async def _run_login(platform: str, config: dict):
    """Background task: run the visible login browser and save session on success."""
    from backend.stealth.browser import create_visible_login_browser

    try:
        success = await create_visible_login_browser(
            platform=platform,
            start_url=config["url"],
            verify_cookie=config.get("verify_cookie"),
            timeout_seconds=600,
        )

        _active_logins[platform] = {
            "in_progress": False,
            "success": success,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

        if success:
            # clear health suspension for this platform
            HealthManager().clear_suspension(platform)
            logger.info(f"Session login successful for {platform}")
        else:
            logger.warning(f"Session login timed out for {platform}")

    except Exception as exc:
        _active_logins[platform] = {
            "in_progress": False,
            "success": False,
            "error": str(exc),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.error(f"Session login failed for {platform}: {exc}")


@router.get("/sessions/{platform}/status")
async def get_session_status(platform: str):
    """Check the status of a platform session (logged in, age, etc.)."""
    if platform not in PLATFORM_LOGIN_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    login_progress = _active_logins.get(platform, {})

    # Check API keys + session file for Telegram
    if platform == "telegram":
        has_keys = bool(settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH)
        session_file = os.path.join(settings.SESSION_PATH, "telegram.session")
        has_session = os.path.exists(session_file)
        return {
            "platform": platform,
            "logged_in": has_keys and has_session,
            "last_login": None,
            "age_hours": None,
            "login_in_progress": login_progress.get("in_progress", False),
        }

    logged_in = False
    last_login = None
    age_hours = None

    # YouTube can use BOTH session cookies and API Key (API key is preferred for Data API)
    # We will mark it as logged_in if API key is present OR valid session file exists
    session_file = os.path.join(settings.SESSION_PATH, f"{platform}.json")
    if os.path.exists(session_file):
        try:
            with open(session_file, encoding="utf-8") as f:
                data = json.load(f)

            required_cookie = PLATFORM_LOGIN_CONFIG[platform].get("verify_cookie")
            has_valid_cookie = True

            if required_cookie:
                has_valid_cookie = False
                cookies = data.get("cookies", [])
                current_time = datetime.now(timezone.utc).timestamp()

                for c in cookies:
                    if c.get("name") == required_cookie:
                        expires = c.get("expires", -1)
                        if expires == -1 or expires > current_time:
                            has_valid_cookie = True
                        break

            if has_valid_cookie:
                logged_in = True
                mtime = os.path.getmtime(session_file)
                last_login = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                age_hours = round(
                    (datetime.now(timezone.utc).timestamp() - mtime) / 3600, 1
                )
            else:
                os.remove(session_file)
                logger.info(f"Deleted invalid or expired session file for {platform}")
        except Exception as e:
            logger.warning(
                f"Error parsing session file for {platform}: {e}. Deleting corrupted file."
            )
            try:
                os.remove(session_file)
            except Exception:
                pass

    if platform == "youtube" and settings.YOUTUBE_API_KEY:
        logged_in = True

    # Active session validation: verify session is still valid server-side
    session_expired = False
    session_uncertain = False
    expired_reason = None
    validation_warning = None
    validation_checked_at = None

    if logged_in and not login_progress.get("in_progress", False):
        validator = SessionValidator()
        validation = await validator.validate(platform)
        validation_checked_at = validation.get("checked_at")
        session_uncertain = validation.get("uncertain", False)
        if session_uncertain:
            validation_warning = validation.get("reason", "validation_error")
            logger.debug(
                f"Session validation uncertain for {platform}: {validation_warning}"
            )
        elif not validation["valid"]:
            session_expired = True
            expired_reason = validation.get("reason", "unknown")
            logger.info(f"Session expired for {platform}: {expired_reason}")

    return {
        "platform": platform,
        "logged_in": logged_in and not session_expired,
        "last_login": last_login,
        "age_hours": age_hours,
        "login_in_progress": login_progress.get("in_progress", False),
        "session_expired": session_expired,
        "session_uncertain": session_uncertain,
        "expired_reason": expired_reason,
        "validation_warning": validation_warning,
        "last_validated": validation_checked_at,
    }


@router.post("/sessions/{platform}/cookies")
async def import_cookies(platform: str, cookies: list[dict] = Body(...)):
    """
    Import raw JSON cookies (e.g., from EditThisCookie extension) and save them as a Playwright session.
    """
    if platform not in PLATFORM_LOGIN_CONFIG:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    config = PLATFORM_LOGIN_CONFIG[platform]
    verify_cookie_name = config.get("verify_cookie")

    if platform == "telegram":
        raise HTTPException(
            status_code=400,
            detail="Telegram uses API-based auth. Manual cookies are not supported.",
        )

    # Validate that the required authentication cookie is present
    if verify_cookie_name:
        is_valid = any(c.get("name") == verify_cookie_name for c in cookies)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid session: Missing required '{verify_cookie_name}' cookie for {platform}.",
            )

    # Normalize cookies for Playwright (mostly to support Cookie-Editor)
    normalized_cookies = []
    for c in cookies:
        cookie = dict(c)  # copy

        # Cookie-Editor uses "expirationDate", Playwright expects "expires"
        if "expirationDate" in cookie:
            cookie["expires"] = cookie.pop("expirationDate")

        # Playwright does not use these extension-specific fields, remove them if present
        for key in ["hostOnly", "session", "storeId", "id"]:
            cookie.pop(key, None)

        # Sanitize sameSite for Playwright (must be Strict|Lax|None)
        if "sameSite" in cookie:
            val = str(cookie["sameSite"]).capitalize()
            if val not in ["Strict", "Lax", "None"]:
                cookie["sameSite"] = "None"
            else:
                cookie["sameSite"] = val

        normalized_cookies.append(cookie)

    # Format into Playwright's expected storage state format
    playwright_state = {"cookies": normalized_cookies, "origins": []}

    # Save to session file
    session_file = os.path.join(settings.SESSION_PATH, f"{platform}.json")

    # Ensure directory exists
    os.makedirs(os.path.dirname(session_file), exist_ok=True)

    atomic_write_json(session_file, playwright_state, indent=2)

    # Clear health suspension since we have a new session
    HealthManager().clear_suspension(platform)
    # Invalidate validation cache so next check uses fresh cookies
    SessionValidator().invalidate(platform)
    logger.info(f"Manual session cookies imported successfully for {platform}")

    return {
        "platform": platform,
        "status": "imported",
        "message": "Cookies imported and session saved successfully.",
    }


@router.delete("/sessions/{platform}")
async def clear_session(platform: str):
    """Delete the saved session for a platform."""
    cleared = False
    # Invalidate validation cache
    SessionValidator().invalidate(platform)

    # Standard browser session file
    session_file = os.path.join(settings.SESSION_PATH, f"{platform}.json")
    if _delete_file_if_exists(session_file):
        cleared = True

    # Telegram uses Telethon .session file
    if platform == "telegram":
        tg_session = os.path.join(settings.SESSION_PATH, "telegram.session")
        if _delete_file_if_exists(tg_session):
            cleared = True
        # Also clear in-memory credentials so status shows Inactive
        _remove_env_key("TELEGRAM_API_ID")
        _remove_env_key("TELEGRAM_API_HASH")
        _remove_env_key("TELEGRAM_PHONE")
        settings.TELEGRAM_API_ID = None
        settings.TELEGRAM_API_HASH = None
        settings.TELEGRAM_PHONE = None
        cleared = True  # Always succeed for Telegram (clearing keys counts)

    # YouTube: clear API key from BOTH memory and .env
    if platform == "youtube":
        if settings.YOUTUBE_API_KEY:
            cleared = True  # Had an API key to clear
            # Remove from .env so it doesn't come back on restart
            _remove_env_key("YOUTUBE_API_KEY")
        settings.YOUTUBE_API_KEY = None

    if cleared:
        logger.info(f"Session cleared for {platform}")
        return {"platform": platform, "status": "cleared"}
    else:
        raise HTTPException(status_code=404, detail=f"No session found for {platform}")


@router.get("/sessions/{platform}/credentials")
async def get_credentials(platform: str):
    """Get the status of API credentials for platforms like YouTube and Telegram."""
    if platform == "youtube":
        return {
            "has_credentials": bool(settings.YOUTUBE_API_KEY),
            "keys": {"api_key": "***" if settings.YOUTUBE_API_KEY else ""},
        }
    elif platform == "telegram":
        return {
            "has_credentials": bool(
                settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH
            ),
            "keys": {
                "api_id": str(settings.TELEGRAM_API_ID)
                if settings.TELEGRAM_API_ID
                else "",
                "api_hash": "***" if settings.TELEGRAM_API_HASH else "",
                "phone": settings.TELEGRAM_PHONE or "",
            },
        }
    else:
        raise HTTPException(
            status_code=400, detail="Only YouTube and Telegram support API credentials."
        )


@router.post("/sessions/{platform}/credentials")
async def save_credentials(platform: str, payload: dict = Body(...)):
    """Save API credentials to the .env file and update the settings singleton in memory."""
    if platform == "youtube":
        api_key = payload.get("api_key")
        if not api_key:
            raise HTTPException(status_code=400, detail="api_key is required")

        if api_key != "***":
            settings.YOUTUBE_API_KEY = api_key
            _set_env_key("YOUTUBE_API_KEY", api_key)
        # Invalidate validation cache so status updates immediately
        SessionValidator().invalidate("youtube")
        return {"status": "success", "message": "YouTube API Key saved."}

    elif platform == "telegram":
        api_id = payload.get("api_id")
        api_hash = payload.get("api_hash")
        phone = payload.get("phone")

        if not api_id or not api_hash:
            raise HTTPException(
                status_code=400, detail="api_id and api_hash are required"
            )

        try:
            settings.TELEGRAM_API_ID = str(int(api_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="api_id must be an integer")

        if api_hash != "***":
            settings.TELEGRAM_API_HASH = api_hash
            _set_env_key("TELEGRAM_API_HASH", api_hash)

        settings.TELEGRAM_PHONE = phone or ""

        _set_env_key("TELEGRAM_API_ID", str(api_id))
        if phone:
            _set_env_key("TELEGRAM_PHONE", phone)

        return {"status": "success", "message": "Telegram credentials saved."}

    else:
        raise HTTPException(
            status_code=400, detail="Only YouTube and Telegram support API credentials."
        )


# ---- Interactive Telegram Authentication Flow ----

_telegram_auth_clients = {}


@router.post("/sessions/telegram/auth/send_code")
async def telegram_send_code(payload: dict = Body(...)):
    phone = payload.get("phone", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number is required")

    # Normalize: Telethon requires international format with +
    phone = phone.replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone

    try:
        from telethon import TelegramClient
    except ImportError:
        raise HTTPException(status_code=500, detail="Telethon library is not installed")

    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        raise HTTPException(status_code=400, detail="Save your API ID and Hash first")

    # Ensure clear session
    session_path = os.path.join(settings.SESSION_PATH, "telegram")

    try:
        api_id = int(settings.TELEGRAM_API_ID)
    except ValueError:
        raise HTTPException(status_code=400, detail="API ID must be an integer")

    client = TelegramClient(session_path, api_id, settings.TELEGRAM_API_HASH)
    await client.connect()

    try:
        res = await client.send_code_request(phone)
        # Store securely in memory for the next verification step
        _telegram_auth_clients[phone] = {
            "client": client,
            "phone_code_hash": res.phone_code_hash,
        }
        return {"status": "success", "message": "Code sent to your Telegram app!"}
    except Exception as e:
        await client.disconnect()
        logger.error(f"Telegram send code failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/telegram/auth/verify_code")
async def telegram_verify_code(payload: dict = Body(...)):
    phone = payload.get("phone", "").strip()
    code = payload.get("code")

    if not phone or not code:
        raise HTTPException(status_code=400, detail="Phone and code are required")

    # Apply the SAME normalization as send_code so the lookup key matches
    phone = phone.replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone

    state = _telegram_auth_clients.get(phone)
    if not state:
        raise HTTPException(
            status_code=400,
            detail="Session expired or code not requested. Try sending code again.",
        )

    client = state["client"]
    phone_code_hash = state["phone_code_hash"]

    from telethon.errors import SessionPasswordNeededError

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        await client.disconnect()
        del _telegram_auth_clients[phone]
        return {"status": "success", "message": "Logged in successfully!"}
    except SessionPasswordNeededError:
        # State remains in memory because they need to submit the password next
        return {"status": "needs_password", "message": "2FA Password required."}
    except Exception as e:
        await client.disconnect()
        del _telegram_auth_clients[phone]
        logger.error(f"Telegram verify code failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/telegram/auth/verify_password")
async def telegram_verify_password(payload: dict = Body(...)):
    phone = payload.get("phone", "").strip()
    password = payload.get("password")

    if not phone or not password:
        raise HTTPException(status_code=400, detail="Phone and password are required")

    # Apply the SAME normalization as send_code so the lookup key matches
    phone = phone.replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone

    state = _telegram_auth_clients.get(phone)
    if not state:
        raise HTTPException(
            status_code=400, detail="Session expired. Try the process again."
        )

    client = state["client"]

    try:
        await client.sign_in(password=password)
        await client.disconnect()
        del _telegram_auth_clients[phone]
        return {"status": "success", "message": "Logged in successfully with 2FA!"}
    except Exception as e:
        await client.disconnect()
        del _telegram_auth_clients[phone]
        logger.error(f"Telegram verify password failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
