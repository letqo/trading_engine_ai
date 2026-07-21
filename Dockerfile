FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: psycopg needs libpq at runtime; build-essential only for the
# build stage so the final image stays small-ish (single stage is fine for
# a Hobby-plan worker -- no need for multi-stage complexity here). nodejs/npm
# are only here to install the `claude` CLI below -- engine.prediction.cli_client
# shells out to it when CLAUDE_CODE_OAUTH_TOKEN is set (predict-loop role).
# Discovered 2026-07-21: predict-loop had been crash-looping in production
# because this image never had the CLI installed at all -- see JOURNAL.md.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY universe.yaml ./

RUN pip install --no-cache-dir .

COPY Procfile ./
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# One image, three possible Railway services (worker/predict-loop/
# dashboard) selected at runtime by SERVICE_ROLE -- see docker-entrypoint.sh.
# Railway's release step runs `alembic upgrade head` before this starts --
# see Procfile / railway.toml. Only the dashboard role actually listens on
# a port; the other two are background workers with none exposed.
CMD ["./docker-entrypoint.sh"]
