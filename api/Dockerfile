# ClipCatalyst Cloud API — one image, two roles.
#   default CMD  → FastAPI (uvicorn) on :8000
#   override CMD → Celery worker (see docker-compose.yml)
#
# Build context is api/ (docker compose builds from this directory).

# ---- stage 1: install Python deps into a clean venv -------------------------
FROM python:3.11-slim AS deps

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# requirements-worker.txt pulls in requirements.txt, which now includes boto3,
# so BOTH roles get it in the one shared image (api mints presigned S3 URLs,
# worker uploads clips) — needed only when CC_STORAGE=s3, imported lazily.
COPY requirements.txt /tmp/requirements.txt
COPY requirements-worker.txt /tmp/requirements-worker.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r /tmp/requirements-worker.txt

# ---- stage 2: runtime -------------------------------------------------------
FROM python:3.11-slim

# Real ffmpeg/ffprobe in the image (settings.py finds them on PATH;
# imageio-ffmpeg remains a dev-only fallback outside Docker).
# Fonts: Inter for captions, DejaVu as the libass fallback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-inter \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CC_DATA_DIR=/data

WORKDIR /app
COPY clipcatalyst_api ./clipcatalyst_api

# Non-root. /data is the single writable surface (uploads, clips, db, tmp);
# docker-compose mounts a named volume here.
RUN useradd --create-home --uid 10001 clipcatalyst \
    && mkdir -p /data \
    && chown clipcatalyst:clipcatalyst /data
USER clipcatalyst

EXPOSE 8000

# Image-level healthcheck for the default (api) role. The worker container
# overrides this in docker-compose.yml with a Celery broker ping, since it
# serves no HTTP. Uses only the stdlib so no extra deps are needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/v1/healthz', timeout=3)" || exit 1

# No --proxy-headers by default: uvicorn would then take X-Forwarded-For from
# any peer in --forwarded-allow-ips, and a container that anyone can reach
# directly must not believe a header a client can type. Behind the reverse
# proxy DEPLOY.md §7.2 mandates, override the command with BOTH flags and name
# the proxy explicitly (its address on the docker network, never "*"):
#
#   command: uvicorn clipcatalyst_api.main:app --host 0.0.0.0 --port 8000 \
#            --proxy-headers --forwarded-allow-ips 172.18.0.2
#
# and set CC_TRUSTED_PROXIES to the same address so the auth rate limiter
# counts per real client instead of lumping the whole internet into the
# proxy's one bucket. Neither set = every request is attributed to the proxy.
CMD ["uvicorn", "clipcatalyst_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
