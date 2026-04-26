# Architecture

## Runtime Shape

The app is a single-process FastAPI service with an in-memory job manager. This is intentional for local workstation usage and keeps live progress updates fast. Run it with one application worker only; multiple workers split job state across processes.

```text
React UI
  -> FastAPI REST routes
  -> JobManager
  -> platform discoverer/analyzer
  -> MongoDB result storage
  -> WebSocket progress stream
```

## Backend Modules

- `backend/api`: HTTP routes, WebSocket endpoint, app bootstrap, and global exception shielding.
- `backend/core`: shared runtime services such as settings, jobs, health, database, cron, filesystem, and Python-version diagnostics.
- `backend/platforms`: platform-specific discovery and analysis logic. These modules contain the scraping/API behavior and should be changed carefully.
- `backend/stealth`: Playwright browser creation, browser pool, fingerprint profile handling, and human-like browser helpers.

## Frontend Modules

- `frontend/src/api`: API client and WebSocket connection helpers.
- `frontend/src/components`: dashboard panels, session login, job monitor, results grids, and platform UI.
- `frontend/src/store`: client-side state management.
- `frontend/src/styles`: shared theme styles.

## Job Flow

1. The frontend calls `POST /jobs`.
2. `backend/api/routes/jobs.py` builds the right discoverer or analyzer.
3. `JobManager` creates one managed async task.
4. The task reports progress through a broadcast event stream.
5. The frontend subscribes to `/ws/jobs/{job_id}` and can reconnect with `after_seq`.
6. Results are saved to MongoDB and streamed back to the UI.

## Platform Boundaries

Keep platform-specific parsing, selectors, and API behavior inside the matching folder under `backend/platforms`. Shared reliability work should go in `backend/core`, `backend/api`, or `backend/stealth` so platform logic stays isolated.

## Generated Data

- `sessions/`: browser storage state and Telegram session files.
- `logs/`: health snapshots and runtime logs.
- `frontend/dist/`: built React frontend.
- `__pycache__/`: Python bytecode cache.

These are runtime artifacts and should not be committed.
