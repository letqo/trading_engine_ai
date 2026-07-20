# SPEC.md — News-Driven Trading Research Engine

## Project identity

This is a **research project**, not a live trading system. Its purpose is to build,
backtest, and paper-trade news-driven strategies on equities/futures, and to
determine — with statistical honesty — whether any strategy has a real edge.

The success metric for v1 is **not profit**. It is: a working pipeline whose
backtest results are trustworthy and whose live paper-trading results match
backtest expectations within a stated tolerance.

## Hard constraints (non-negotiable, enforce in code)

1. **PAPER TRADING ONLY.** No live-money API keys, endpoints, or order routes
   may exist anywhere in this codebase. The broker client must be constructed
   with the paper/sandbox base URL hardcoded. If a live URL or live credential
   is ever detected in config, the program must refuse to start and exit with
   an error.
2. **Risk limits are core architecture, not features.** Every order passes
   through a single `RiskGate` module before submission. There is no code path
   to the broker that bypasses it.
3. **No look-ahead.** The backtester may never expose data with timestamp > t
   to a decision made at time t. This includes news timestamps, adjusted
   prices, and any derived features.
4. **Determinism & audit.** Every backtest run is reproducible: config, code
   version (git hash), data snapshot ID, and random seed are logged with the
   results.

## Risk rules (initial values, configurable but always enforced)

- Max capital per position: 5% of paper account equity
- Max total exposure: 20% of equity
- Per-trade stop-loss: 2% adverse move from entry
- Max daily drawdown: 3% of equity → flatten all positions, halt trading until
  next session
- Max consecutive losing trades per day: 4 → halt for the day
- Kill switch: a single CLI command / file flag that cancels all open orders
  and flattens all positions immediately
- No overnight positions in v1 (flatten before market close)

## Stack

- Python 3.11+, managed with `uv` or `poetry`
- Broker: **Alpaca paper trading account** (free) for v1. Futures via a
  broker sandbox (e.g. IBKR paper / Tradovate sim) is a later phase — the
  broker client must be an interface so it can be swapped.
- Storage: **PostgreSQL from day one** (Railway Postgres in production, local
  Postgres via Docker Compose for dev). ORM: **SQLModel** (Pydantic +
  SQLAlchemy) with **Alembic** for versioned schema migrations. All access
  through `DATABASE_URL`. Parquet files for bulk historical bars.
- News: start with free sources — RSS feeds (Reuters, PRNewswire, company IR),
  and one API free tier (e.g. NewsAPI or Finnhub news). Store raw payloads;
  never discard source data.
- Sentiment v1: a small local model or lexicon (e.g. FinBERT via
  transformers, or VADER as the dumbest baseline). Must run offline on stored
  headlines.
- Testing: pytest. Core modules (RiskGate, backtester clock, data loaders)
  require unit tests before strategies are written.
- Deployment target: **Railway** (existing Hobby subscription, new project).
  The app must run locally and on Railway from the same codebase — 12-factor
  style: all config via environment variables, no secrets in the repo.

## Trading universe (fixed, config-driven)

The engine trades only instruments on this watchlist (a config file, e.g.
`universe.yaml`). It never scans or trades outside it. Each instrument entry
carries: symbol, tier, asset class, news topics/keywords to associate with it,
and its futures twin (if any).

**Tier 1 — Core (signals are developed and validated here first):**
- Index ETFs: SPY, QQQ, IWM
- Mega-cap news magnets (~10): AAPL, MSFT, NVDA, TSLA, META, AMZN, GOOGL,
  AMD, JPM, COIN

**Tier 2 — Macro & regional diversification (basket exposure, not
individual foreign/small-cap names):**
- Regions: EWJ (Japan), FXI (China), VGK (Europe), EEM (emerging markets)
- Sectors/commodities/rates: SMH (semis), XLE (energy), XLF (banks),
  GLD (gold), USO (oil), TLT (long bonds)
- Rationale: one broker (Alpaca), one currency, one data pipeline, English
  news — while still getting Asia/Europe/size/commodity exposure. Individual
  foreign stocks and individual small caps are excluded (liquidity, data,
  and manipulated-news risk).

**Tier 3 — Futures twins (real-money era only, after paper validation):**

| ETF (rehearsal) | Futures twin (live focus) | Underlying |
|---|---|---|
| SPY | ES | S&P 500 |
| QQQ | NQ | Nasdaq 100 |
| IWM | RTY | Russell 2000 |
| GLD | GC | Gold |
| USO | CL | Crude oil |
| TLT | ZB | 30Y bonds |
| EWJ | NKD | Nikkei 225 |

Principle: leverage multiplies edge, it does not create it. Signals are
proven on the unleveraged ETF; the futures twin inherits the signal, with
position sizing recomputed for contract size and margin.

**News-to-instrument routing:** each Tier 2 instrument is tagged with its
macro news drivers (e.g. BoJ/Japan → EWJ; PBoC/China stimulus → FXI;
ECB → VGK; EIA oil inventories → USO/XLE; Fed/FOMC/CPI → SPY, QQQ, TLT).
The news pipeline routes scored items to instruments via these tags.

## Architecture

```
data/           ingestion + storage (bars, news), snapshot management
features/       sentiment scoring, event detection, feature computation
strategy/       signal generation; strategies implement a common interface
backtest/       event-driven simulator with realistic fills, fees, slippage
risk/           RiskGate: position sizing, limits, kill switch
execution/      broker interface + Alpaca paper implementation
journal/        run logs, trade logs, experiment registry
cli/            commands: ingest, backtest, papertrade, report, kill
```

Strategy interface (approximate):

```python
class Strategy(Protocol):
    def on_bar(self, ctx: MarketContext) -> list[Signal]: ...
    def on_news(self, ctx: MarketContext, item: NewsItem) -> list[Signal]: ...
```

The same strategy object must run unmodified in both the backtester and the
live paper-trading loop. If it can't, the design is wrong.

## Backtester requirements

- Event-driven (process bars and news items in strict timestamp order from a
  unified event queue). No vectorized shortcuts for v1 — correctness first.
- Model costs: commission + a slippage assumption (start pessimistic:
  fill at next bar's open ± 1 tick against you).
- Outputs per run: equity curve, total return, max drawdown, Sharpe, win rate,
  profit factor, number of trades, average holding time, exposure.
- Every run is registered in the experiment journal (SQLite table) with
  config, git hash, data snapshot, and metrics.

## Anti-self-deception protocol (mandatory)

- **Train/validation split by time.** Develop on the older period; the most
  recent 12+ months are touched only for final validation, at most a few
  times ever. Log every time the validation set is used.
- **Baselines first.** Before any "smart" strategy: (a) buy-and-hold,
  (b) random entries with the same trade frequency and the same risk rules.
  A strategy that doesn't beat both, after costs, is dead.
- **Overfitting checks.** Report results with parameter perturbation (±20% on
  each parameter). If performance collapses under small perturbations, flag
  the run as fragile.
- **Bias review.** Each new strategy PR must include written answers to:
  Where could look-ahead bias enter? Survivorship bias? Is the news timestamp
  the *publication* time or the *ingestion* time?
- **Paper-vs-backtest reconciliation.** During live paper trading, weekly
  report comparing realized results to backtest expectations. Divergence
  beyond tolerance = investigate before iterating further.

## Deployment (Railway)

- **Service type:** background worker (no public web port required). Provide a
  `Procfile`/start command and a Dockerfile so the same image runs locally and
  on Railway.
- **Persistence:** all durable state (trades, journals, experiment registry,
  news, bars metadata) lives in **Railway Postgres** — never on the
  container filesystem, which is ephemeral. Bulk historical bar files
  (Parquet) are dev-side artifacts; anything the live worker needs must be
  in Postgres or fetchable on startup. Losing the trade journal on redeploy
  is a critical bug. Schema changes only via Alembic migrations, run as a
  release step before the worker starts.
- **Config & secrets:** broker API keys, news API keys, and all limits come
  from environment variables. `.env` for local dev (gitignored), Railway
  variables in production. The paper-only startup check applies identically
  on Railway: if credentials resolve to a live endpoint, the service must
  log the violation and exit nonzero instead of trading.
- **Observability:** structured logs to stdout (Railway captures them); a
  daily summary written to the journal; optional alerting via a webhook
  (e.g. Discord/Telegram) for: trade executed, risk halt triggered, kill
  switch engaged, service restart.
- **Remote kill switch:** since there's no local terminal, the kill switch
  must work remotely — e.g. a `HALT=true` environment variable checked every
  loop iteration (flipping it in Railway and redeploying/restarting flattens
  and halts), or a tiny authenticated HTTP endpoint. Halting must also be
  the automatic response to unhandled exceptions: fail flat, not open.
- **Restart semantics:** the service must be crash-safe. On startup it
  reconciles state with the broker (open positions, open orders) before
  emitting any new signals. Railway restarts and deploys must never result
  in duplicate or orphaned orders.
- **Budget:** stay within Hobby plan included usage; this is a single small
  worker + volume. No GPU, no heavy models server-side in v1 (sentiment
  scoring for live headlines must be lightweight or precomputed).

## Build phases (each phase ends with tests passing and a demo)

**Phase 0 — Skeleton.** Repo, package layout, config system, logging, CI with
pytest. `docker-compose.yml` with local Postgres; SQLModel models and initial
Alembic migration for the experiment/trade journal tables. RiskGate
implemented and unit-tested against synthetic orders.

**Phase 1 — Data layer.** Ingest daily + intraday bars and news for the
Tier 1 + Tier 2 universe from `universe.yaml`, including the
news-to-instrument routing tags. Snapshot mechanism. Demo: replay any past
day's events in order from the CLI.

**Phase 2 — Backtester.** Event-driven engine with fills, fees, slippage,
wired to RiskGate. Validated against hand-computed toy scenarios in tests.

**Phase 3 — Baselines.** Buy-and-hold and random-entry strategies run through
the backtester; results registered in the journal. These numbers are the bar
to clear.

**Phase 4 — Dumb news strategy (control group).** "Positive-sentiment headline
→ long, exit after N hours or on stop." Backtest it. It is expected to lose
after costs; document what it teaches.

**Phase 5 — Iteration.** Improve signals (event types, sentiment quality,
filters, timing) strictly via the backtester and the anti-self-deception
protocol. One change per experiment, journaled. First named candidate:
**Overnight-Gap strategy** — score Asia/Europe macro news published outside
US market hours (BoJ, ECB, PBoC, overnight geopolitical/commodity events),
route it to the tagged Tier 2 ETFs, and act at or shortly after the US open
with a defined exit horizon. This targets a single well-defined decision
moment (the open) rather than millisecond reaction, which suits retail
infrastructure. Backtest it against the same baselines and cost model as
everything else.

**Phase 6 — Deploy & live paper trading.** Containerize and deploy to Railway
as a worker with a mounted volume; verify restart-reconciliation and the
remote kill switch with deliberate chaos tests (kill the service mid-position,
redeploy, confirm clean state). Then run the best strategy against the Alpaca
paper account in real time for a minimum of 3 months. Weekly reconciliation
reports. Only after this period is any discussion of real capital even
permitted — and that decision lives outside this codebase.

## Explicitly out of scope for v1

- Real-money trading of any kind
- Futures execution (rehearsed via ETF twins; futures come in the real-money
  era per the Tier 3 table)
- Individual foreign stocks and individual small caps (basket ETFs only)
- Options strategies (come after the equity/futures pipeline is trusted)
- Sub-minute / HFT-style trading (retail infrastructure cannot compete there)
- Social-media firehose ingestion (Phase 5+ at the earliest)
- ML model training beyond off-the-shelf sentiment (earn it with data first)

## Working agreement for the AI agent

- Prefer boring, testable code over clever code.
- When asked for "a profitable strategy," respond with an experiment plan,
  not a curve-fit. Actively look for reasons results are inflated.
- Never weaken a risk rule or the paper-only constraint to make something
  "work." If a constraint blocks progress, stop and surface it to the human.
- Keep an updated `JOURNAL.md`: every strategy change, its motivation, and
  its journaled experiment ID.
