# Queryll API (Instance 2) — deployed on Render as a WEB SERVICE.
#
# The ingestion worker is a separate image and a separate Render service (a Background
# Worker, with no port and no health check). The two share nothing but the database.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a source change does not reinstall the world.
COPY requirements-base.txt requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app

# Not root. The API reads uploaded bytes from untrusted sources.
RUN useradd --create-home --uid 10001 queryll && chown -R queryll:queryll /app
USER queryll

EXPOSE 8000

# Render supplies $PORT; 8000 is the local default.
#
# One worker on purpose. An in-flight answer lives in an asyncio task inside this
# process (that is what lets a dropped reader still get a persisted message), so
# workers do not share it — scale with more instances, not more workers per instance,
# and never put a buffering proxy in front of /ask.
#
# Access logs stay ON. Contract 9 forbids logging file bytes, extracted text, keys and
# tokens, and none of those appear in a request line — questions travel in POST bodies,
# and document and chunk ids in a URL are explicitly fine to log.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
