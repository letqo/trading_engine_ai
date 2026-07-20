FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: psycopg needs libpq at runtime; build-essential only for the
# build stage so the final image stays small-ish (single stage is fine for
# a Hobby-plan worker -- no need for multi-stage complexity here).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY universe.yaml ./

RUN pip install --no-cache-dir .

COPY Procfile ./

# No public port: this is a background worker (SPEC.md Deployment section).
# Railway's release step runs `alembic upgrade head` before this starts --
# see Procfile / railway.toml.
CMD ["python", "-m", "engine.cli.main", "papertrade"]
