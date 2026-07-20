# Deployment (Railway)

## What's implemented vs. what's a manual one-time step

Implemented in this repo, works locally today:
- `Dockerfile` builds a single image that runs the same code locally and on
  Railway (`docker build . && docker run ...`, or `docker-compose up`).
- `railway.toml` declares the build (Dockerfile), the release command
  (`alembic upgrade head`, run before the worker starts on every deploy),
  the start command (`engine papertrade`), and pins `numReplicas = 1`
  (never run two live instances against the same broker account).
- `engine predict-loop` is a second always-on process (predict-news +
  act-on-predictions + resolve-predictions, on a loop) -- it needs its own
  Railway service. `railway.toml` only configures one service's start
  command; add a second service in the Railway dashboard pointed at the
  same repo/image, with its start command overridden to
  `python -m engine.cli.main predict-loop`. Same `numReplicas = 1` rule
  applies -- it submits real orders through the same broker account.
- `Procfile` mirrors the same two process types for any Heroku-style
  buildpack path as a fallback to the Dockerfile route.
- The paper-only guard (`engine.config.guard`), remote kill switch
  (`HALT=true` env var or a `HALT` flag file, checked every loop iteration
  in `engine papertrade`), startup reconciliation
  (`engine.execution.reconcile`), and fail-flat-on-exception behavior are
  all implemented and unit-tested (`tests/test_paper_only_guard.py`,
  `tests/test_reconcile.py`, `tests/test_cli.py`).
- Structured JSON logs to stdout (`engine.logging_setup`) -- Railway
  captures stdout directly, no extra shipping config needed.
- Optional webhook alerting (`engine.observability`) for trade-executed,
  risk-halt, kill-switch, and service-restart events -- silently disabled
  if `ALERT_WEBHOOK_URL` isn't set.

Requires you, because they need accounts/credentials this environment
cannot create:
1. **Create the Railway project** (`railway init` or via the dashboard) and
   attach a Railway Postgres plugin. Copy its `DATABASE_URL` into the
   service's environment variables.
2. **Create an Alpaca paper account** (free) and generate a paper API
   key/secret. Set `ALPACA_API_KEY` / `ALPACA_API_SECRET` in Railway's
   environment variables. Do **not** set `ALPACA_BASE_URL` or
   `ALPACA_LIVE` -- their mere presence trips the paper-only guard and the
   service will refuse to start (this is intentional, see
   `engine/config/guard.py`).
3. **Set the remaining env vars** from `.env.example` (news API keys if
   you want NewsAPI/Finnhub in addition to the free RSS feeds,
   `ALERT_WEBHOOK_URL` if you want Discord/Telegram/Slack alerts).
4. `railway up` (or connect the GitHub repo for auto-deploy on push).

## Chaos tests to run before trusting this in Phase 6

These need a live Railway deployment against the Alpaca paper account, so
they cannot be run from this environment -- run them yourself before the
3-month paper-trading clock (SPEC.md Phase 6) starts, and record the
results in `JOURNAL.md`:

1. **Kill mid-position.** With an open paper position, run `engine kill`
   (or set `HALT=true` and redeploy). Confirm: all open orders canceled,
   all positions flattened, the flag persists across a restart (kill switch
   stays engaged until explicitly cleared).
2. **Redeploy mid-position.** Trigger a Railway redeploy while a position is
   open and no kill switch is set. Confirm on the new instance's startup
   logs: `reconcile_account_state` reports the correct pre-existing
   position, `cancel_stale_orders` cancels anything left open by the old
   instance, and no duplicate order is submitted for a position the broker
   already reports.
3. **Crash mid-loop.** Kill the container process (not via the kill switch)
   while an order might be in flight. Confirm the next startup's
   reconciliation converges to the broker's actual state rather than trusting
   any in-memory assumption from the killed process.
4. **Webhook down.** Point `ALERT_WEBHOOK_URL` at an unreachable host and
   confirm trading/halting logic is unaffected (alerts are logged and
   swallowed, never raised -- see `engine.observability.send_alert`).

None of these are simulate-able without a real deployment; do not mark
Phase 6 "verified" until all four have been run against the actual Railway
service and Alpaca paper account.
