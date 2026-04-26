# Unified Social Media Tool

An OSINT collection and analysis platform for Facebook, Instagram, Twitter/X, YouTube, Telegram, and TikTok. The backend is FastAPI, the frontend is React/Vite, browser-based platforms use Playwright, and YouTube/Telegram use official API-backed flows where available.

## Project Status

This repository is organized as a local production-style application, not a Python package. The main entrypoint is `home.py`, which can run the API server, build the frontend, run cron jobs, or launch interactive login sessions.

Supported Python versions:

- Python 3.10
- Python 3.11 recommended
- Python 3.12
- Python 3.13
- Python 3.14

Runtime policy is enforced in `backend/core/runtime.py`.

## Repository Layout

```text
backend/
  api/          FastAPI app, routes, WebSocket progress stream
  core/         configuration, jobs, database, health, runtime helpers
  platforms/   platform-specific discovery and analysis implementations
  stealth/     Playwright browser, fingerprint, human-behavior utilities
frontend/
  src/          React application
  public/       static frontend assets
docs/           architecture and operations notes
home.py         main local entrypoint
requirements.txt
Dockerfile
cron_jobs.json
```

Runtime/generated folders such as `sessions/`, `logs/`, `exports/`, `__pycache__/`, and `frontend/dist/` are intentionally ignored by git.

## Quick Start

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python home.py --build
python home.py
```

Open `http://localhost:9000`.

## Configuration

Create `.env` from `.env.example` and fill in only the values you need. Do not commit `.env` or `sessions/`; they contain credentials and login state.

Important settings:

- `MONGO_URI`: MongoDB connection string.
- `YOUTUBE_API_KEY`: YouTube Data API key.
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`: Telegram API login values.
- `ANALYSIS_CONCURRENT_TABS`: analysis parallelism for browser-scraped platforms.
- `ANALYSIS_API_CONCURRENT_TABS`: higher parallelism for YouTube and Telegram analysis.

## Common Commands

```powershell
python home.py
python home.py --build
python home.py --login instagram
python home.py --login facebook
python home.py --login twitter
python home.py --cron
python home.py --port 8080
```

## Docker

Default image uses Python 3.11:

```powershell
docker build -t unified-social-tool .
```

Python 3.14 build:

```powershell
docker build --build-arg PYTHON_IMAGE=python:3.14-slim -t unified-social-tool:py314 .
```

## More Docs

- `SETUP.md`: laptop setup guide.
- `docs/ARCHITECTURE.md`: codebase structure and runtime flow.
- `docs/OPERATIONS.md`: production hygiene, shutdown, and troubleshooting notes.
