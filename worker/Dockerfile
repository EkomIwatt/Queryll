# Queryll ingestion worker.
#
# Deployed on Render as a **Background Worker**, not a Web Service: this process binds no
# port and answers no health check. Its only interface is rows in Postgres.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code change does not re-resolve the whole environment.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY queryll_worker ./queryll_worker

# Unprivileged: this process reads user documents and needs nothing else from the filesystem.
RUN useradd --create-home --uid 10001 worker \
    && chown -R worker:worker /app
USER worker

# No EXPOSE and no HEALTHCHECK on purpose — see the note at the top of this file.
CMD ["python", "-m", "queryll_worker"]
