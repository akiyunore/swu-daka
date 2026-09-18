FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS frontend-builder
WORKDIR /build
COPY checkin-web/frontend/package.json checkin-web/frontend/package-lock.json ./
RUN npm ci
COPY checkin-web/frontend/ ./
COPY checkin-web/docs/ ../docs/
RUN npm run build

FROM mcr.microsoft.com/playwright/python:v1.55.0-noble@sha256:640d578aae63cfb632461d1b0aecb01414e4e020864ac3dd45a868dc0eff3078
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp/swu-daka \
    XDG_CACHE_HOME=/tmp/swu-daka/cache \
    TZ=Asia/Shanghai \
    CHECKIN_WEB_ENV=production \
    CHECKIN_WEB_DATA_DIR=/data \
    CHECKIN_WEB_DATABASE_URL=sqlite:////data/app.db \
    CHECKIN_WEB_LOG_DIR=/data/logs \
    CHECKIN_WEB_CHROME_HEADLESS=false \
    CHECKIN_WEB_CLI_TIMEOUT_SECONDS=270 \
    SWU_RUNTIME_DIR=/data/cli \
    SWU_USER_DATA_DIR=/data/cli/browser-profile \
    SWU_TOKEN_FILE=/data/cli/token \
    SWU_CACHE_FILE=/data/cli/checkin_cache.json \
    SWU_AUDIT_LOG_DIR=/data/cli/audit \
    SWU_NETWORK_LOG_DIR=/data/cli/network \
    SWU_CHROME_HEADLESS=0 \
    SWU_CHROME_STARTUP_TIMEOUT_SECONDS=45 \
    SWU_LOGIN_FLOW_TIMEOUT_SECONDS=180 \
    SWU_IDM_HTTP_LOGIN=1 \
    SWU_IDM_HTTP_TIMEOUT_SECONDS=20 \
    MIMO_API_URL=https://api.xiaomimimo.com/v1/chat/completions \
    MIMO_MODEL=mimo-v2.5 \
    MIMO_API_TIMEOUT_SECONDS=20 \
    MIMO_OCR_MAX_CALLS_PER_LOGIN=2

WORKDIR /app
COPY requirements.docker.lock.txt ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.docker.lock.txt
COPY --chown=pwuser:pwuser login_and_checkin.py /app/login_and_checkin.py
COPY --chown=pwuser:pwuser legacy /app/legacy
COPY --chown=pwuser:pwuser checkin-web/backend /app/checkin-web/backend
COPY --chown=pwuser:pwuser LICENSE /app/LICENSE
COPY --from=frontend-builder --chown=pwuser:pwuser /build/dist /app/checkin-web/frontend/dist
RUN rm -f /app/requirements.docker.lock.txt && \
    mkdir -p /data /tmp/swu-daka && \
    chown -R pwuser:pwuser /data /tmp/swu-daka /app

USER pwuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)); raise SystemExit(0 if d.get('status') == 'ok' and d.get('admin_configured') is True and d.get('credential_key_configured') is True else 1)"
CMD ["xvfb-run", "-a", "-s", "-screen 0 1280x900x24 -nolisten tcp", "python", "-m", "uvicorn", "app.main:app", "--app-dir", "/app/checkin-web/backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "32", "--backlog", "64", "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "285", "--no-server-header"]
