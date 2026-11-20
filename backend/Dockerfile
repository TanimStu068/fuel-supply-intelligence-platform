FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.32.0" \
    "httpx>=0.27.2" \
    "pydantic>=2.9.0" \
    "pydantic-settings>=2.6.0" \
    "sqlalchemy[asyncio]>=2.0.36" \
    "asyncpg>=0.30.0" \
    "prometheus-client>=0.21.0" \
    "alembic>=1.14.0" \
    "psycopg[binary]>=3.2.0"

COPY app ./app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Apply the schema migrations, then serve. The migration step is allowed to
# fail: the service also creates any missing table at startup, so a
# migration hiccup never blocks the boot.
CMD ["sh", "-c", "python -m alembic upgrade head || true; exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
