# JOURNAL.md

Every strategy change, its motivation, and its journaled experiment ID (per
SPEC.md's working agreement). Newest first.

---

## 2026-07-20 -- Initial build: Phases 0-5 implemented, Phase 6 scaffolded

**What was built.** The full pipeline described in SPEC.md through Phase 5:
repo skeleton, config + paper-only guard, Postgres-via-SQLModel journal with
Alembic migrations, RiskGate (17 unit tests), the fixed `universe.yaml`
(Tier 1/2/3 with news-topic routing), a data layer pulling real bars
(yfinance, no key) and real news (free RSS: Yahoo Finance, MarketWatch,
PRNewswire), VADER sentiment, an event-driven backtester (bars+news merged
into one strictly time-ordered queue, next-bar-open ± 1 tick fills,
commission, RiskGate wired into every order), baselines (buy-and-hold,
random-entry), the Phase 4 dumb news-sentiment strategy, and the Phase 5
Overnight-Gap strategy. 89 tests passing, including hand-computed
toy-scenario tests for the backtester and a real end-to-end CLI backtest
run against live SPY/QQQ data.

**What was not built, and why.** Phase 6's "run the best strategy against
Alpaca paper for a minimum of 3 months" cannot be done inside a single
build session -- it is calendar-bound by definition. What exists instead:
a crash-safe `engine papertrade` loop skeleton (paper-only guard, startup
reconciliation against the broker, kill-switch check every iteration,
fail-flat on unhandled exceptions) that intentionally does **not** yet wire
a strategy's signals into live order submission. See "Open questions" below
for why that's a deliberate stopping point, not an oversight.

**Scope decisions made without asking, and the reasoning:**
- **Long-only in v1.** Every strategy SPEC.md actually specifies for v1
  (buy-and-hold, random-entry, positive-sentiment-headline, Overnight-Gap)
  is long-side. Short-selling brings margin/borrow mechanics SPEC.md never
  asks for. A SELL/CLOSE signal with nothing open to sell is dropped, never
  turned into a short. Revisit only when a strategy actually needs it.
- **No-overnight rule scoped to intraday timeframes.** Enforcing "flatten
  before close" literally at daily-bar resolution would make buy-and-hold
  impossible to backtest at all (there'd be no bar on which it's allowed to
  still be holding). The rule is enforced for intraday bars (where "before
  market close" is a real moment in the data) and not for daily bars (where
  a bar already spans the full session). Buy-and-hold itself is a static
  reference benchmark, never a strategy that would run live -- see
  `engine/backtest/engine.py` module docstring.
- **Buy-and-hold is still subject to RiskGate's stop-loss and daily-drawdown
  halt.** SPEC.md hard constraint #2 says there is no code path to the
  broker that bypasses RiskGate, full stop -- no baseline gets a carve-out.
  First real backtest run (SPY/QQQ/etc., 2026-05-01..2026-07-01, daily
  bars) closed 4 of ~21 opened positions early via risk halts rather than
  holding to the end, which is the expected, correct behavior given that
  constraint. Don't read "buy-and-hold underperformed a textbook buy-and-hold"
  as a bug -- it's risk-managed buy-and-hold by design, and that's the
  correct comparison basis for baselines run through *this* engine.
- **News timestamp for backtests is `max(published_at, ingested_at)`**, not
  `published_at` alone (`engine.domain.NewsItem.decision_timestamp`). See
  `docs/bias_review.md` for the full reasoning -- this is the specific
  mechanism preventing look-ahead bias from news data.
- **Alpaca client talks directly to Alpaca's REST API via `requests`**
  rather than an SDK, so the hardcoded paper base URL
  (`engine/config/guard.py::ALPACA_PAPER_BASE_URL`) is the only URL in the
  whole request path -- no SDK internals to audit for a hidden live-endpoint
  fallback.

**Open questions for the human before Phase 6 continues:**
1. Alpaca paper API keys, a Railway project + Postgres plugin, and
   (optionally) NewsAPI/Finnhub keys are all still needed -- none of these
   can be created from inside a build session. See `docs/deployment.md`.
2. `engine papertrade` deliberately does not wire strategy signals to real
   order submission yet. Before it does: which strategy should even be
   trusted with paper capital first? Overnight-Gap is the only "smart"
   candidate that exists, and per the anti-self-deception protocol it
   hasn't beaten both baselines after costs on a real backtest yet --that
   comparison needs to happen (Phase 3 baselines vs. Phase 5 candidate, same
   universe, same window, same cost model) before anything trades live,
   even on paper.
3. The four chaos tests in `docs/deployment.md` (kill mid-position, redeploy
   mid-position, crash mid-loop, webhook down) need an actual Railway
   deployment to run and have not been run.

**Bias review:** see `docs/bias_review.md` for the mandatory look-ahead /
survivorship / publication-vs-ingestion writeups for `DumbNewsStrategy` and
`OvernightGapStrategy`.
