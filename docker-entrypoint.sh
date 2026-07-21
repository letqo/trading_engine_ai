#!/bin/sh
# Single image, multiple Railway services -- which process runs is chosen
# by SERVICE_ROLE (an env var set per-service in Railway), not by baking a
# different start command into each service's config. Mirrors the
# PAPERTRADE_STRATEGY pattern already used inside the worker role itself.
set -e

case "${SERVICE_ROLE:-worker}" in
  worker)
    exec python -m engine.cli.main papertrade
    ;;
  predict-loop)
    exec python -m engine.cli.main predict-loop
    ;;
  dashboard)
    exec python -m uvicorn engine.dashboard.app:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  *)
    echo "Unknown SERVICE_ROLE: '${SERVICE_ROLE}' -- expected worker, predict-loop, or dashboard" >&2
    exit 1
    ;;
esac
