FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq5 \
    libjxl-tools \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /uvx /usr/local/bin/

RUN groupadd --system app \
    && useradd --system --gid app --home /app --shell /sbin/nologin app

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

RUN mkdir -p /data \
    && chown -R app:app /app /data

EXPOSE 8000

CMD ["sh", "-c", "mkdir -p /data && [ \"$(stat -c '%u' /data)\" = \"$(id -u app)\" ] || chown -R app:app /data; exec setpriv --reuid=app --regid=app --clear-groups env HOME=/app sh -c 'alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000'"]
