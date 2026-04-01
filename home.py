"""
Application Execution Root.
Handles CLI parameter parsing and orchestrates control flow to various subsystems:
  - ASGI API Gateway (`run_server`)
  - Background Task Daemon (`run_cron`)
  - CI/CD Artifact Compilation (`build_frontend`)
  - Interactive Browser Authentication Shell (`run_login`)
"""

import argparse
import asyncio
import os
import subprocess
import sys
import signal

# add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)


def main():
    parser = argparse.ArgumentParser(
        description="Unified Social Media Tool v2 — OSINT Intelligence Platform",
    )
    parser.add_argument("--cron", action="store_true", help="Run the cron scheduler")
    parser.add_argument("--build", action="store_true", help="Build the React frontend")
    parser.add_argument("--login", type=str, metavar="PLATFORM", help="Open browser for manual login (facebook, instagram, twitter, youtube, telegram)")
    parser.add_argument("--port", type=int, default=9000, help="API server port (default: 9000)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="API server host (default: 0.0.0.0)")

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
    """Kill any stale process occupying the given port (Windows-only auto-cleanup)."""
    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid == os.getpid():
                    continue  # Don't kill ourselves
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                )
                import time
                time.sleep(1)
                break
    except Exception:
        pass


def run_server(host: str, port: int):
    """
    Bootstraps the Uvicorn ASGI server and injects the FastAPI dependency graph.
    """
    import uvicorn
    from backend.core.logger import get_logger

    logger = get_logger("home")

    # Auto-free the port if a stale process is holding it (common on Windows)
    _free_port(port)

    logger.info(f"Starting Unified Social Media Tool API on {host}:{port}")

    print(f"""
+--------------------------------------------------+
|     Unified Social Media Tool v2                 |
|     OSINT Intelligence Platform                  |
+--------------------------------------------------+
|  API Server:  http://localhost:{port:<14}|
|  API Docs:    http://localhost:{port}/docs{' ' * max(0, 8 - len(str(port)))}|
|  Health:      http://localhost:{port}/health{' ' * max(0, 6 - len(str(port)))}|
+--------------------------------------------------+
    """)

    uvicorn.run(
        "backend.api.router:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
        access_log=False,
    )


def run_cron():
    """
    Initializes the APScheduler daemon.
    Acquires and locks the main thread into an explicit asyncio event loop for background periodic tasks.
    """
    from backend.core.cron import CronScheduler

    print("Starting cron scheduler...")
    scheduler = CronScheduler()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # handle shutdown signals
    def shutdown():
        loop.stop()

    try:
        if sys.platform != "win32":
            loop.add_signal_handler(signal.SIGINT, shutdown)
            loop.add_signal_handler(signal.SIGTERM, shutdown)
    except NotImplementedError:
        pass

    try:
        loop.run_until_complete(scheduler.run_forever())
    except KeyboardInterrupt:
        print("\nCron scheduler stopped.")
    finally:
        loop.close()


def run_login(platform: str):
    """
    Orchestrates an un-managed, interactive Playwright browser shell.
    Designed exclusively to capture and serialize volatile authentication tokens directly to the filesystem.
    """
    from backend.stealth.browser import create_visible_login_browser
    from backend.api.routes.sessions import PLATFORM_LOGIN_CONFIG

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
            print(f"\n✅ {platform.title()} session saved successfully!")
        else:
            print(f"\n❌ Login timed out for {platform.title()}")
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
    finally:
        loop.close()


def build_frontend():
    """
    CI/CD pipeline hook. 
    Triggers Node.js sub-processes to compile the Vite React SPA bundle for static mounting.
    """
    frontend_dir = os.path.join(PROJECT_ROOT, "frontend")

    if not os.path.exists(os.path.join(frontend_dir, "package.json")):
        print("Frontend package.json not found!")
        return

    print("Installing frontend dependencies...")
    result = subprocess.run(["npm", "install"], cwd=frontend_dir, shell=True)
    if result.returncode != 0:
        print("npm install failed!")
        return

    print("Building frontend...")
    result = subprocess.run(["npm", "run", "build"], cwd=frontend_dir, shell=True)
    if result.returncode != 0:
        print("Build failed!")
        return

    print("\n✅ Frontend built successfully!")
    print("Run 'python home.py' to start the server with the built frontend.")


if __name__ == "__main__":
    main()
