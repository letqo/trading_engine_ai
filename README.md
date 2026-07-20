# News-Driven Trading Research Engine

A research project (**not a live trading system**) for building,
backtesting, and paper-trading news-driven strategies, with statistical
honesty about whether any of it has a real edge. Full spec: [`SPEC.md`](SPEC.md).
Build log / design decisions: [`JOURNAL.md`](JOURNAL.md).

**Paper trading only.** The broker client's base URL is hardcoded to
Alpaca's paper endpoint (`engine/config/guard.py`); if a live URL or
credential is ever detected in config, the program refuses to start. See
`tests/test_paper_only_guard.py`.

## Quick start (local dev)

```bash
python -m venv .venv
.venv/Scripts/activate   # or source .venv/bin/activate on macOS/Linux
pip install -e ".[dev]"
cp .env.example .env

# Local DB: SQLite works out of the box (DATABASE_URL default in .env.example).
# For Postgres instead: docker-compose up -d postgres, then point
# DATABASE_URL at postgresql+psycopg://engine:engine@localhost:5432/trading_engine
alembic upgrade head

pytest                        # full suite
pytest -m "not network"       # skip tests that hit real yfinance/RSS

engine --help
```

## CLI

```bash
engine ingest --start 2026-01-01 --end 2026-07-01              # snapshot bars+news
engine replay --date 2026-07-17 --symbols SPY,QQQ --interval 1h  # Phase 1 demo
engine backtest --strategy buy_and_hold --start 2026-01-01 --end 2026-07-01
engine backtest --strategy overnight_gap --start ... --end ... --perturb
engine report                                                    # recent runs
engine reconcile --run-id <id> --week-start ... --week-end ... \
    --backtest-expected-pct 1.2 --realized-pct 0.8               # weekly Phase 6 check
engine kill                                                       # kill switch
engine papertrade                                                 # live worker loop

engine predict-news --limit 10                                    # LLM consequence-prediction forward-test
engine act-on-predictions                                         # trade confident predictions (real paper orders)
engine resolve-predictions                                        # score predictions + close expired prediction trades
engine predictions-report                                         # accuracy of resolved, forward-safe predictions
```

Strategies: `buy_and_hold`, `random_entry` (Phase 3 baselines),
`dumb_news` (Phase 4 control group), `overnight_gap` (Phase 5 candidate),
`momentum`, `mean_reversion`, `multi_factor` (pure price-action, trade both
long and short -- see `engine/strategy/technical.py`).

`engine ingest`/`engine backtest` use Alpaca's News API (real dated
articles back to 2015) for historical news whenever `ALPACA_API_KEY` is
set, falling back to live-only RSS otherwise -- see
`engine/data/alpaca_news.py`. This means a `backtest` over a past date
range can now get news that actually existed in that range, instead of
only whatever `engine ingest` happened to be running when it was current.

The consequence-prediction pipeline (`engine.prediction`) is a separate,
parallel research track -- not a `Strategy`, not driven by the backtester's
event loop. It asks an LLM to reason about indirect/second-order
consequences of news (mechanisms keyword routing and VADER sentiment can't
see), and forward-tests itself since it can't be backtested honestly.
Confident predictions can now be traded for real (paper) money via `engine
act-on-predictions` -- both directions, through RiskGate like every other
order -- with the position closed again by `engine resolve-predictions`
once the resolution window ends. See
[`docs/prediction_pipeline.md`](docs/prediction_pipeline.md) for why the
forward-test design exists, and what config it needs (`ANTHROPIC_API_KEY`,
`ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF`, `PREDICTION_ACTION_CONFIDENCE_THRESHOLD`).

## Architecture

```
data/           ingestion (yfinance bars, RSS + Alpaca historical news) + storage, snapshots
features/       sentiment scoring (VADER)
strategy/       Strategy protocol + implementations (same object runs in
                both the backtester and the live loop)
backtest/       event-driven simulator, cost model, metrics, perturbation
risk/           RiskGate -- single non-bypassable choke point for every order
execution/      Broker interface + Alpaca paper client + startup reconciliation
journal/        SQLModel tables + registry (experiment/trade/journal, Postgres)
prediction/     LLM consequence-prediction forward-test loop (separate from strategy/)
cli/            ingest, replay, backtest, report, reconcile, kill, papertrade
```

See module docstrings for the specific design decisions and why (long/short
support in the backtester, no-overnight scoped to intraday bars, news
`decision_timestamp` vs `published_at`, etc.) -- they're documented at the point of the decision,
not duplicated here.

## Deployment

See [`docs/deployment.md`](docs/deployment.md): what's already wired up
(Dockerfile, `railway.toml`, kill switch, alerting) vs. what needs your
Railway/Alpaca accounts, and the chaos tests to run before trusting Phase 6.

## Anti-self-deception protocol

See [`docs/bias_review.md`](docs/bias_review.md) for the mandatory
look-ahead / survivorship / publication-vs-ingestion writeups required for
every strategy. `engine.backtest.perturbation` implements the ±20%
parameter-perturbation fragility check. Validation-period backtests require
an explicit `--validation-reason` and are logged in the experiment journal
(`ExperimentRun.is_validation_run`).

## Status

Phases 0-5 implemented and tested (89 tests). Phase 6 is scaffolded
(Dockerfile, kill switch, reconciliation, alerting) but not live -- it
needs your Alpaca paper keys and a Railway project, and the actual 3-month
paper-trading run is calendar-bound. See `JOURNAL.md` for the full status
and open questions.
