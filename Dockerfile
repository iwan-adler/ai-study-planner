# syntax=docker/dockerfile:1
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install runtime dependencies first so this layer is cached when only code changes.
# pytest is a test tool, so it's left out of the image.
COPY requirements.txt .
RUN grep -viE '^pytest' requirements.txt > /tmp/runtime.txt \
    && pip install -r /tmp/runtime.txt \
    && rm /tmp/runtime.txt

COPY . .

# Run as an unprivileged user; only /data (the database volume) is writable.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir /data && chown appuser /data
USER appuser

ENV DATABASE_URL=sqlite:////data/study_planner.db
VOLUME ["/data"]
EXPOSE 8000

# slim images have no curl, so the health check uses Python's standard library.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
