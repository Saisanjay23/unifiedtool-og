# Operations

## Supported Runtime

The application currently supports Python `>=3.10,<3.15`. Python 3.11 remains the recommended version for the most consistent Playwright/browser behavior.

Use `GET /health` to inspect the active Python version, project root, current working directory, worker count, and runtime warnings.

## Starting Locally

```powershell
python home.py --build
python home.py
```

The API and built frontend run at `http://localhost:9000` by default.

## Worker Count

Run the API with a single worker. Live job state, progress history, and login flow state are kept in process memory. Multiple workers can make one request see a different state than another request.

Avoid:

```powershell
uvicorn backend.api.router:app --workers 2
```

Prefer:

```powershell
python home.py
```

## Clean Shutdown

Use `Ctrl+C` once to stop the server. The app cancels running jobs, waits for job cleanup, closes database connections, and suppresses expected cancellation noise from WebSocket disconnects.

If shutdown hangs, it usually means a browser or network call is waiting inside Playwright. Wait for the request timeout before pressing `Ctrl+C` again.

## Analysis Parallelism

Browser-scraped platforms use conservative analysis defaults:

```env
ANALYSIS_CONCURRENT_TABS=3
ANALYSIS_INTER_PROFILE_DELAY=1.5
```

YouTube and Telegram use official API-backed analysis defaults:

```env
ANALYSIS_API_CONCURRENT_TABS=6
ANALYSIS_API_INTER_PROFILE_DELAY=0
```

Increase API parallelism gradually if your API quotas and laptop resources allow it.

## Secrets and Sessions

Never commit these files or folders:

- `.env`
- `sessions/`
- `*.session`
- `logs/`
- `exports/`

Use `.env.example` as the public template.

## Useful Checks

```powershell
python -m compileall backend home.py
python home.py --build
python home.py
```

For Docker with Python 3.14:

```powershell
docker build --build-arg PYTHON_IMAGE=python:3.14-slim -t unified-social-tool:py314 .
```
