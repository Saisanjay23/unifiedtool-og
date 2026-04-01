# Setup Guide: Running the OSINT Platform on a New Laptop

Complete step-by-step guide to get the Unified Social Media Tool running from scratch.

---

## Step 1: Install Prerequisites

Install these **3 things** on the new laptop:

| Software | Version | Download Link |
|----------|---------|---------------|
| **Python** | 3.10+ | https://www.python.org/downloads/ |
| **Node.js** | 18+ (LTS) | https://nodejs.org/ |
| **MongoDB** | Community 7+ | https://www.mongodb.com/try/download/community |

> **Python Installer Tip**: Check ✅ "Add Python to PATH" during installation.

> **MongoDB Alternative**: You can use MongoDB Atlas (cloud) instead of installing locally. Get a free cluster at https://www.mongodb.com/atlas

---

## Step 2: Copy the Project

Copy the entire `unifiedtool-og` folder to the new laptop (USB, zip, Google Drive, etc).

---

## Step 3: Configure Environment

1. Open the project folder in File Explorer
2. Find `.env.example` → **make a copy** and rename it to `.env`
3. Open `.env` in any text editor and fill in:

```env
# Required — MongoDB connection
MONGO_URI=mongodb://localhost:27017

# Optional — only if you use YouTube scraping
YOUTUBE_API_KEY=your_key_here

# Optional — only if you use Telegram scraping
TELEGRAM_API_ID=your_id
TELEGRAM_API_HASH=your_hash
TELEGRAM_PHONE=+91xxxxxxxxxx
```

> If using MongoDB Atlas, replace `MONGO_URI` with your Atlas connection string.

---

## Step 4: Install Backend (Python)

Open **Terminal / Command Prompt** inside the project folder and run these commands **one by one**:

```bash
# 1. Create a virtual environment
python -m venv venv

# 2. Activate it
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# 3. Install all Python packages
pip install -r requirements.txt

# 4. Install Playwright browser (downloads Chromium ~150MB)
playwright install chromium
```

---

## Step 5: Install & Build Frontend (React)

```bash
# 1. Build the frontend (installs npm packages + compiles React app)
python home.py --build
```

This does `npm install` + `npm run build` automatically.

---

## Step 6: Start the Application

```bash
python home.py
```

The app will start at: **http://localhost:9000**

You should see:
```
+--------------------------------------------------+
|     Unified Social Media Tool v2                 |
|     OSINT Intelligence Platform                  |
+--------------------------------------------------+
|  API Server:  http://localhost:9000              |
|  API Docs:    http://localhost:9000/docs         |
|  Health:      http://localhost:9000/health       |
+--------------------------------------------------+
```

Open **http://localhost:9000** in your browser.

---

## Step 7: Login to Platforms (First-Time Only)

Scrapers need valid sessions to work. Log in to each platform you plan to use:

### Option A: From the Web UI
1. Open http://localhost:9000
2. Select a platform (e.g., Instagram) from the sidebar
3. Click **"Interactive Login"** in the Session panel
4. A browser window opens → log in manually
5. Session is saved automatically when login is detected

### Option B: From Command Line
```bash
python home.py --login instagram
python home.py --login facebook
python home.py --login twitter
```

Each command opens a browser → log in manually → session saves on success.

> **Instagram**: After logging in, if you see "We suspect automated behavior", the tool will automatically dismiss it.

> **YouTube & Telegram** use API keys instead of browser login. Set them in `.env`.

---

## Quick Reference — All Commands

| Command | Description |
|---------|-------------|
| `python home.py` | Start the server (main command) |
| `python home.py --build` | Build/rebuild the frontend |
| `python home.py --login instagram` | Login to Instagram interactively |
| `python home.py --login facebook` | Login to Facebook interactively |
| `python home.py --login twitter` | Login to Twitter/X interactively |
| `python home.py --cron` | Start the cron scheduler (background jobs) |
| `python home.py --port 8080` | Start on a custom port |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `python` not found | Use `python3` instead, or reinstall Python with "Add to PATH" checked |
| `npm` not found | Reinstall Node.js |
| MongoDB connection error | Make sure MongoDB is running: `mongod` or check Atlas IP whitelist |
| `playwright install` fails | Run as admin: `pip install playwright && playwright install chromium` |
| Instagram/Facebook blocked | Re-login: `python home.py --login instagram` |
| Port 9000 already in use | Use a different port: `python home.py --port 8080` |
| Frontend not loading | Rebuild: `python home.py --build` |

---

## Folder Structure (What NOT to Delete)

```
unifiedtool-og/
├── .env                  ← Your config (DO NOT share — has API keys)
├── sessions/             ← Saved login sessions (DO NOT delete)
├── backend/              ← Python backend
├── frontend/             ← React frontend
├── home.py               ← Main entry point
├── requirements.txt      ← Python dependencies
└── SETUP.md              ← This file
```
