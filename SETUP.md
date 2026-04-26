# Setup Guide

This guide brings a new laptop from zero to a working local copy of the Unified Social Media Tool.

## 1. Install Prerequisites

Install:

| Software | Version |
| --- | --- |
| Python | 3.10 to 3.14, with 3.11 recommended |
| Node.js | 18+ LTS |
| MongoDB | Community 7+ or MongoDB Atlas |

On Windows, enable "Add Python to PATH" during Python installation.

## 2. Configure Environment

Create `.env` from `.env.example`, then fill in the values you use:

```env
MONGO_URI=mongodb://localhost:27017
YOUTUBE_API_KEY=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=
```

If you use MongoDB Atlas, set `MONGO_URI` to your Atlas connection string.

## 3. Install Backend Dependencies

Open PowerShell inside the project folder:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## 4. Build the Frontend

```powershell
python home.py --build
```

This installs frontend dependencies and builds the React app into `frontend/dist`.

## 5. Start the Application

```powershell
python home.py
```

Open:

```text
http://localhost:9000
```

Useful endpoints:

```text
http://localhost:9000/docs
http://localhost:9000/health
```

## 6. Login to Browser-Based Platforms

Use the UI session panel or command line:

```powershell
python home.py --login instagram
python home.py --login facebook
python home.py --login twitter
```

YouTube and Telegram use API credentials from `.env` instead of browser login.

## Common Commands

| Command | Description |
| --- | --- |
| `python home.py` | Start the server |
| `python home.py --build` | Build or rebuild the frontend |
| `python home.py --login instagram` | Open interactive Instagram login |
| `python home.py --login facebook` | Open interactive Facebook login |
| `python home.py --login twitter` | Open interactive Twitter/X login |
| `python home.py --cron` | Start the cron scheduler |
| `python home.py --port 8080` | Start on a custom port |

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `python` not found | Reinstall Python with PATH enabled, or use the Python launcher. |
| `npm` not found | Install Node.js LTS and reopen PowerShell. |
| MongoDB connection error | Start local MongoDB or check Atlas IP access. |
| Playwright browser missing | Run `playwright install chromium`. |
| Frontend not loading | Run `python home.py --build`. |
| Port already in use | Run `python home.py --port 8080`. |
| Session says logged out | Re-run interactive login for that platform. |

## Folders to Keep Private

Do not share or commit:

```text
.env
sessions/
logs/
exports/
*.session
```

These contain credentials, login state, or runtime output.
