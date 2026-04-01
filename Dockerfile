# ============================================================
# Unified Social Media Tool — Production Dockerfile
# Optimized for Render.com free tier (512MB RAM)
# ============================================================

# Stage 1: Build the React frontend
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --production=false
COPY frontend/ ./
RUN npm run build

# Stage 2: Production Python image
FROM python:3.11-slim

# Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install only Chromium (smallest browser, saves ~400MB vs all browsers)
RUN playwright install chromium

# Copy backend source
COPY backend/ ./backend/
COPY home.py .
COPY cron_jobs.json .

# Copy built frontend from stage 1
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Create required directories
RUN mkdir -p sessions logs

# Environment defaults for production
ENV HOST=0.0.0.0
ENV PORT=10000
ENV DEFAULT_HEADLESS=true
ENV PYTHONUNBUFFERED=1

# Render uses port 10000 by default
EXPOSE 10000

# Launch the API server
CMD ["python", "home.py", "--host", "0.0.0.0", "--port", "10000"]
