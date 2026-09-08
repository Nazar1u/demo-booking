# syntax=docker/dockerfile:1

# --- builder -----------------------------------------------------------
# uv копіюємо з офіційного образу, щоб не залежати від тега вигляду
# "uv:python3.14-...", який може не існувати для свіжого Python.
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Спершу лише манифести — шар з залежностями кешується, поки вони не
# змінились. --no-install-project, бо самого коду тут ще немає.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --- runtime -----------------------------------------------------------
# Той самий тег Python, що й у builder: venv несе абсолютні шляхи.
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings

# libpq не потрібен: psycopg[binary] несе його в собі.
RUN useradd --create-home --uid 10001 app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app manage.py entrypoint.sh ./
COPY --chown=app:app config ./config
COPY --chown=app:app booking ./booking

RUN chmod +x entrypoint.sh && \
    mkdir -p /app/staticfiles && chown app:app /app/staticfiles

USER app

EXPOSE 8000

# Той самий ендпоінт, який перевіряє деплой у Фазі 9.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]
