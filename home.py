"""
Application execution root.

Handles CLI parameter parsing and orchestrates control flow to:
- the ASGI API gateway (`run_server`)
- the background task daemon (`run_cron`)
- the frontend build pipeline (`build_frontend`)
- the interactive browser authentication shell (`run_login`)
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys

# Add project root to path.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from backend.core.runtime import configure_runtime, validate_python_runtime

configure_runtime()


def main():
    validate_python_runtime(raise_on_error=True)

    parser = argparse.ArgumentParser(
        description="Unified Social Media Tool v2 - OSINT Intelligence Platform",
    )
    parser.add_argument("--cron", action="store_true", help="Run the cron scheduler")
    parser.add_argument("--build", action="store_true", help="Build the React frontend")
    parser.add_argument(
        "--login",
        type=str,
        metavar="PLATFORM",
        help="Open browser for manual login (facebook, instagram, twitter, youtube, telegram)",
    )
    parser.add_argument("--port", type=int, default=9000, help="API server port")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="API server host")

    args = parser.parse_args()

    if args.build:
        build_frontend()
    elif args.cron:
        run_cron()
    elif args.login:
        run_login(args.login)
    else:
        run_server(args.host, args.port)


def _free_port(port: int):
    """Check if the chosen port is already in use and warn; never force-kill."""
    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid == os.getpid():
                    continue
                print(
                    f"\nWARNING: Port {port} is already in use by PID {pid}.\n"
                    f"Use --port <other> or stop that process manually.\n"
                )
                sys.exit(1)
    except Exception:
        pass


def run_server(host: str, port: int):
    """Boot the Uvicorn ASGI server."""
    import uvicorn

    from backend.core.logger import get_logger

    logger = get_logger("home")
    _free_port(port)

    logger.info(f"Starting Unified Social Media Tool API on {host}:{port}")

    print(
        f"""
+--------------------------------------------------+
|     Unified Social Media Tool v2                 |
|     OSINT Intelligence Platform                  |
+--------------------------------------------------+
|  API Server:  http://localhost:{port:<14}|
|  API Docs:    http://localhost:{port}/docs{' ' * max(0, 8 - len(str(port)))}|
|  Health:      http://localhost:{port}/health{' ' * max(0, 6 - len(str(port)))}|
+--------------------------------------------------+
"""
    )

    try:
        uvicorn.run(
            "backend.api.router:app",
            host=host,
            port=port,
            reload=False,
            log_level="info",
            access_log=False,
        )
    except KeyboardInterrupt:
        logger.info("Server stopped by user")


def run_cron():
    """Start the APScheduler daemon."""
    from backend.core.cron import CronScheduler

    print("Starting cron scheduler...")
    scheduler = CronScheduler()

    try:
        asyncio.run(scheduler.run_forever())
    except KeyboardInterrupt:
        print("\nCron scheduler stopped.")


def run_login(platform: str):
    """Open a visible Playwright browser for manual platform login."""
    from backend.api.routes.sessions import PLATFORM_LOGIN_CONFIG
    from backend.stealth.browser import create_visible_login_browser

    platform = platform.lower()
    config = PLATFORM_LOGIN_CONFIG.get(platform)
    if config is None:
        print(f"Unknown platform: {platform}. Choices: {', '.join(PLATFORM_LOGIN_CONFIG.keys())}")
        return

    if config["url"] is None:
        print(f"{platform.title()} uses API-based auth, not browser login.")
        if platform == "telegram":
            print("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env")
        return

    url = config["url"]
    cookie = config.get("verify_cookie")
    print(f"\nOpening {platform.title()} login browser...")
    print("Please log in manually. The session will be saved automatically.\n")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        success = loop.run_until_complete(
            create_visible_login_browser(
                platform=platform,
                start_url=url,
                verify_cookie=cookie,
                timeout_seconds=600,
            )
        )
        if success:
            print(f"\n{platform.title()} session saved successfully!")
        else:
            print(f"\nLogin timed out for {platform.title()}")
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
    finally:
        loop.close()


def build_frontend():
    """Install frontend dependencies and compile the Vite React app."""
    frontend_dir = os.path.join(PROJECT_ROOT, "frontend")

    if not os.path.exists(os.path.join(frontend_dir, "package.json")):
        print("Frontend package.json not found!")
        return

    npm_exe = shutil.which("npm.cmd") or shutil.which("npm")
    if npm_exe is None:
        print("npm was not found. Install Node.js LTS, then reopen PowerShell and retry.")
        return

    npm_cache_dir = os.path.join(PROJECT_ROOT, ".npm-cache")
    os.makedirs(npm_cache_dir, exist_ok=True)
    npm_env = os.environ.copy()
    npm_env["npm_config_cache"] = npm_cache_dir

    package_lock = os.path.join(frontend_dir, "package-lock.json")
    install_cmd = [npm_exe, "ci"] if os.path.exists(package_lock) else [npm_exe, "install"]

    print(f"Installing frontend dependencies with npm {install_cmd[1]}...")
    result = subprocess.run(install_cmd, cwd=frontend_dir, env=npm_env)
    if result.returncode != 0:
        print(f"npm {install_cmd[1]} failed!")
        return

    print("Building frontend...")
    result = subprocess.run([npm_exe, "run", "build"], cwd=frontend_dir, env=npm_env)
    if result.returncode != 0:
        print("Build failed!")
        return

    print("\nFrontend built successfully!")
    print("Run 'python home.py' to start the server with the built frontend.")


if __name__ == "__main__":
    main()
