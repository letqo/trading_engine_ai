# JOURNAL.md

Every strategy change, its motivation, and its journaled experiment ID (per
SPEC.md's working agreement). Newest first.

---

## 2026-07-21 -- Add: read-only reporting dashboard (engine.dashboard) + role-aware deploy image

**What changed.** New FastAPI service (`engine.dashboard.app`) serving six
read-only pages -- overview (equity, accuracy, MFE/MAE summary), full
predictions log, AI trade history, off-universe ticker suggestions,
backtest/live-run registry, and risk-halt audit trail. HTTP Basic Auth,
single shared password (`DASHBOARD_PASSWORD`); refuses to serve
unauthenticated rather than defaulting open. Deliberately never imports
`engine.execution.broker`/`RiskGate` -- the only broker calls are
`AlpacaPaperClient`'s read methods (`get_account_equity`,
`get_positions`), same trust boundary as any other consumer of a paper
account's public state. Reuses existing `engine.journal.registry` queries;
added `load_recent_experiment_runs`/`load_recent_risk_halts` for the two
tables that had no read path yet.

**Two real bugs caught before they reached production, not after:**
- Starlette's `TemplateResponse` signature changed to `(request, name,
  context)` in the version pinned by the current FastAPI release --  the
  old `(name, {"request": request, ...})` calling convention silently
  passed the context dict as the template *name*, producing a Jinja2
  internal cache-key error on every route. Caught by actually hitting
  every route with curl before calling it done, not by trusting that
  "the code looks right."
- `pip install .` (a real, non-editable install, i.e. what the Dockerfile
  does) does not bundle `templates/*.html` by default -- setuptools only
  packages `.py` files without explicit `package-data` config. Verified by
  installing into a throwaway venv exactly the way the Dockerfile does,
  confirming the templates directory was missing, adding
  `[tool.setuptools.package-data]`, and re-verifying the fix in the same
  clean venv rather than trusting the editable dev install (which never
  would have shown this).

**Deploy shape changed to support this without three separate Dockerfiles:**
one image now serves three possible Railway services (`worker`,
`predict-loop`, `dashboard`), selected at container start by a
`SERVICE_ROLE` env var (`docker-entrypoint.sh`) rather than a per-service
Railway startCommand override -- the CLI has no clean way to set that
per-service, and this mirrors the `PAPERTRADE_STRATEGY` pattern already
used inside the worker role itself. `railway.toml`'s `startCommand` was
removed accordingly (would have overridden the entrypoint script for
every service sharing the repo); `releaseCommand` (alembic) still applies
to all three.

---

## 2026-07-21 -- Add: max-favorable/max-adverse excursion (mfe_pct/mae_pct) on resolved predictions

**Why.** Raised directly by a user question: "how do we handle the fact
that price falls, then maybe rises again -- there's a time component where
'wrong' can become 'right.'" The honest answer is that resolution never
chases that (see the new "Resolution is a fixed-horizon snapshot, on
purpose" section in `docs/prediction_pipeline.md` for why re-litigating the
horizon would make the accuracy metric meaningless) -- but the
`entry_price`/`exit_price` snapshot was throwing away real information
that answers a related, legitimate question: not "did the market
eventually agree," but "would a real position have survived to collect
that outcome."

**What changed.** `Prediction` gains `mfe_pct`/`mae_pct` (migration
`9f2c7a4b1e3d`, both nullable, no backfill -- old resolved rows never kept
their intermediate bars, so there's nothing honest to fill in). Computed in
`engine.prediction.pipeline._fetch_resolution_data` from bars that were
already being fetched for entry/exit -- no new data pull, just no longer
discarding everything except the first and last bar. Both are non-negative
pct-of-entry-price magnitudes relative to the *predicted* direction: how
far price moved in its favor (mfe_pct) and against it (mae_pct) at any
point during the window. `resolve_prediction` accepts them as optional
params, set only on the RESOLVED path (never on INVALID, matching
entry_price/exit_price's existing treatment).

**Surfaced in `predictions-report`** as avg mfe/mae plus an explicit count:
how many "correct" predictions had `mae_pct` past the configured
`RISK_STOP_LOSS_PCT` -- i.e., directionally right at the 24h mark, but a
live position with that stop would have been closed out before ever
seeing it. Also shown per-row in `resolve-predictions` and
`prediction-trades` output. Purely diagnostic -- `outcome_correct` still
depends only on the entry/exit endpoint comparison, unchanged.

---

## 2026-07-20 -- Add: live wiring for dumb_news/overnight_gap/momentum/mean_reversion/multi_factor

**What changed.** `engine papertrade --strategy <name>` now actually trades
live -- the crash-safe skeleton loop (reconcile, kill switch, daily-
drawdown halt) that previously never called any strategy now polls for new
bars/news each cycle and dispatches them to the *same* `Strategy` object
`engine backtest` uses, through the *same* `RiskGate`. New module
`engine/execution/live_loop.py`. Eligible: `dumb_news`, `overnight_gap`,
`momentum`, `mean_reversion`, `multi_factor`. Not eligible, on purpose:
`buy_and_hold`/`random_entry`, which are documented reference benchmarks,
never meant to trade live.

**Two pieces of logic were extracted into shared modules specifically so
backtest and live can never drift apart**, rather than reimplementing the
same math twice: `engine.execution.signal_translation.signal_to_side`
(BUY/SELL/CLOSE -> broker side, now used by both `BacktestEngine` and the
live loop) and `engine.execution.position_bookkeeping` (open/close P&L and
quantity math, now shared by `engine.prediction.trading` and the live
loop, replacing that module's own private copy of the same logic).

**Where live necessarily differs from backtest, and why (full detail in
`docs/bias_review.md`'s new "Live wiring" section):**
- `seed_bar_history` populates real lookback at startup *without* firing
  `on_bar`, so indicator strategies (momentum, mean_reversion,
  multi_factor) have working history immediately without replaying it as
  if it just happened.
- Stop-loss checks bypass `RiskGate.evaluate()` entirely and write
  straight to the account, exactly like the backtester's `_check_stop` --
  a forced risk-reducing exit was never supposed to be blockable by
  opening-order caps.
- **The backtester's no-overnight-position rule has no live equivalent
  yet.** It relies on knowing in advance which bar is the last of the
  trading day -- look-ahead within already-fetched historical data that
  doesn't exist live. Each strategy's own exit timer is what currently
  bounds live holding time instead. Flagged as a real fidelity gap, not
  just a docs note.

**Still true, unchanged by wiring this up: none of these five strategies
have been validated against the Phase 3 baselines.** That comparison is a
separate, deliberate next step -- wiring live trading and proving a
strategy has earned the right to trade capital (even paper) are two
different things.

---

## 2026-07-20 -- Add: unrestricted symbol naming (with universe growth via evidence, not automation)

**What changed.** The consequence-prediction model was previously hard-
restricted to naming only `universe.yaml` symbols ("the only symbols you
may name" in the system prompt). That capped the pipeline's actual
insight: if the real answer to "who's exposed by this headline" was a
company outside the tracked 34, the model could only ever name the closest
tracked proxy. The prompt/schema now let it name any real ticker it judges
best; `Prediction.in_tracked_universe` (migration `6b0f3a85b65d`) records
whether that symbol happens to be one we can act on. Off-universe
predictions are logged and scored exactly like tracked ones -- they're
real evidence either way -- they're just never eligible for a real order
(`load_actionable_predictions` now filters on `in_tracked_universe`).

**Growing the universe is a human decision, gated on accumulated evidence,
never automatic.** `engine ticker-suggestions` aggregates every
off-universe symbol by how many times it's been named, how many
predictions have resolved, and the accuracy of those resolved ones (only
forward_safe rows count, same integrity rule as everywhere else),
flagging ones that cross a configurable count+accuracy bar as worth a
look. Nothing in this codebase writes to `universe.yaml` -- the report
surfaces the evidence, a human decides whether to add the symbol.

**Visibility.** `engine prediction-trades` lists the history of every
prediction actually acted on with a real order -- symbol, direction, size,
open/closed state, and outcome once resolved -- separate from
`predictions-report`'s aggregate accuracy number, since most predictions
are logged and scored but never traded.

---

## 2026-07-20 -- Add: automatic predict-loop, and a real daily-drawdown bug fix found while building it

**`engine predict-loop`.** Automatic version of predict-news +
act-on-predictions + resolve-predictions: runs all three every
`PREDICTION_LOOP_POLL_SECONDS` (default 1h), forever, checking the kill
switch and daily-drawdown halt each cycle like `papertrade` does. The
individual commands remain available for manual, one-off runs -- this
doesn't replace them, it's the default always-on mode once deployed.
Runs log-only (predict + resolve, no real orders) if `ALPACA_API_KEY` isn't
set. Needs its own Railway service alongside `papertrade` -- see
`docs/deployment.md`.

**Found and fixed while building it: the daily-drawdown halt in
`papertrade` could never actually trigger.** Its loop called
`reconcile_account_state(client)` on every iteration, and that function
unconditionally resets `equity_at_session_start = current_equity`. So
immediately before `check_daily_drawdown` ran, the baseline it compares
against had just been reset to the current value -- the computed drawdown
was always ~0%, regardless of what actually happened intraday. No test
exercised the `papertrade` loop at all, so this went unnoticed. Fixed by
splitting the concern: `reconcile_account_state` (resets the session
baseline -- call once at startup and once per calendar day) vs. the new
`refresh_account_state` (updates equity/cash/positions in place, leaves
`equity_at_session_start`/`trades_today`/etc. untouched -- call every
iteration in between). `predict-loop` was built on the correct pattern from
the start; `papertrade`'s loop now tracks the calendar day the same way the
backtester already does (`current_date` check before resetting the
session). `tests/test_reconcile.py` now includes a test that would have
caught this: intraday equity drop -> `check_daily_drawdown` must return
`True`, and did not before this fix.

---

## 2026-07-20 -- Add: real (paper) trading for the prediction pipeline + three technical strategies

**Consequence-prediction pipeline now trades.** `engine act-on-predictions`
submits a real paper order for any PENDING, forward_safe prediction whose
confidence clears `PREDICTION_ACTION_CONFIDENCE_THRESHOLD` (default 0.6) --
"up" goes long, "down" goes short. `engine resolve-predictions` now also
closes the linked position once the resolution window ends (if
`ALPACA_API_KEY` is set), in addition to its existing scoring step. Scoring
itself is unchanged: every prediction, traded or not, is still scored
against real historical bars the same way -- trading is a second, parallel
consequence for a confident subset, not a replacement for the log's
existing honesty mechanism. Every order still goes through
`RiskGate.evaluate()`. New `Prediction` fields: `traded_order_id`,
`traded_quantity`, `exit_order_id` (migration `dad98268679f`). New module:
`engine/prediction/trading.py`.

**Three new price-action strategies (`engine/strategy/technical.py`):**
`momentum` (trend continuation over a configurable lookback), `mean_reversion`
(z-score-based contrarian entries), `multi_factor` (momentum entries gated
by a volatility/regime filter). None of these use news at all -- pure
`bar_history`-driven indicators, a different signal source than the
existing news-driven family. They're also the first strategies in this
repo to actually trade both directions, which is what motivated reversing
the long-only scope decision (previous entry, same day). Bias reviews for
all three: `docs/bias_review.md`.

**Found and fixed while touching `_default_params`/`--perturb` wiring:**
`engine backtest --perturb` was silently non-functional for every strategy,
not just the new ones. The CLI's perturbation factory
(`lambda **kw: STRATEGY_FACTORIES[strategy](universe, seed)`) discarded the
perturbed kwargs entirely and always rebuilt the identical default-param
strategy, so every "perturbed" run was actually identical to the base run
-- `fragile` could never be `True`. No test exercised this path (`grep
perturb tests/` found only unit tests of `run_perturbation_analysis`
itself, called with a correct factory directly, never through the CLI).
Fixed with a separate `STRATEGY_PERTURBATION_FACTORIES` mapping that
actually forwards perturbed values into each strategy's constructor.
Verified against real data: `engine backtest --strategy momentum --start
2026-04-01 --end 2026-07-01 --perturb` now shows perturbed sharpe values
that actually differ from the base run and correctly flags two of four
perturbations as fragile, where before this would have always printed
`fragile=False` with identical perturbed/base numbers. This means **any
prior `--perturb` output from before this fix should not be trusted** --
it was reporting "not fragile" regardless of whether that was true.

---

## 2026-07-20 -- Add: short-selling support in the backtester (reversing the v1 long-only scope decision)

**What changed.** The initial build's "long-only in v1" scope decision (see
below, 2026-07-20 "Initial build") is reversed: `engine.backtest.engine` now
supports opening, adding to, and closing short positions symmetrically with
longs. A SELL signal from flat now opens a short instead of being silently
dropped; a BUY signal against an existing short covers it; CLOSE flattens
whichever direction is actually open.

**Why now.** This was a deliberate v1 simplification, not a technical
limitation -- `RiskGate.evaluate`/`_is_closing`/`is_stop_triggered`/
`flatten_orders` were already written direction-agnostically (they key off
`position.quantity`'s sign, not a long/short flag), because no existing
strategy needed shorts yet. Only `BacktestEngine`'s own signal-queueing and
fill/P&L logic assumed long-only, in four places: `_queue_signals` (dropped
SELL-from-flat), `_fill_pending` (SELL only ever meant "close an existing
long"), `_execute_fill` (BUY/SELL branches assumed long open/close, not
short open/cover), and `_check_stop`/`_flatten_symbol` (both skipped any
position with `quantity <= 0`, which would have silently never stopped-out
or flattened a short once one existed -- a real latent safety gap, not just
a missing feature). All four are now symmetric; P&L sign logic for
closing/covering is centralized in one new helper (`_realize_close`) so a
sign bug can't drift between the fill path, the stop-loss path, and the
flatten path independently.

**What's still a simplification, on purpose.** Margin requirements and
stock-borrow fees for short positions are not modeled -- opening a short
credits the sale proceeds to cash and debits them back on cover, with no
borrow cost or margin-call mechanic. Acceptable for a paper-trading
research engine; flagged so nobody mistakes backtest short P&L for what a
real margin account would actually charge.

**Existing strategies are unaffected.** `dumb_news` and `overnight_gap`
still only ever emit BUY/CLOSE (never SELL-to-open) by their own choice --
see their updated docstrings. The long-only assumption they document is now
explicitly *their* design choice, not an engine-wide restriction.

---

## 2026-07-20 -- Add: Alpaca historical news backfill, replacing live-only RSS for backtests

**What changed.** `engine/data/alpaca_news.py` fetches historical news from
Alpaca's News API (`data.alpaca.markets/v1beta1/news`, Benzinga-sourced,
real dated articles back to 2015). It reuses `ALPACA_API_KEY`/
`ALPACA_API_SECRET` -- no new credential -- and isn't subject to the
paper/live guard, since Alpaca's market data isn't split by paper/live
account (only order routing is, which this doesn't touch).

**Why.** The previous fix (2026-07-20, below) made `engine backtest` read
news from the journal DB instead of blindly re-fetching live RSS, but that
only solved the symptom: the DB was still only ever populated with
"whatever RSS shows right now," so a backtest over a date range nobody had
run `engine ingest` during would still come up empty. This endpoint has
actual history, so that limitation is now fixed for anyone with an Alpaca
key: `engine ingest --start <past date> --end <past date>` and
`engine backtest` (when the DB has nothing cached for the range) both pull
real news for that exact window. RSS remains the fallback when no Alpaca
key is set or auth fails.

**The bias-review trap this could have introduced, and how it's avoided.**
`docs/bias_review.md` already flags this exact scenario: "a
replayed/backfilled dataset assembled after the fact must not fabricate
`ingested_at` from `published_at` ... or the backtest is quietly
optimistic." We were not actually polling in, say, 2019, so there's no real
historical ingestion timestamp for a backfilled article. Setting
`ingested_at = published_at` (zero simulated lag) would assume a live
poller learns about every headline the instant it's published, which no
real polling pipeline does -- exactly the quietly-optimistic mistake. Fixed
by simulating a fixed, pessimistic poll lag instead
(`ALPACA_NEWS_BACKFILL_LAG_SECONDS`, default 900s): `ingested_at =
published_at + lag`. This also uncovered a real latent bug:
`record_news_item` had no `ingested_at` parameter at all -- it always used
the row's `now()` default. For live RSS that's correct (the process really
is seeing the item right now), but for backfilled historical rows it would
have stamped every one with *today's* date regardless of how long ago it
was published, pushing `NewsItem.decision_timestamp` past the entire
backtest window and silently making the whole backfill inert. Added an
optional `ingested_at` param, defaulting to `None` (old behavior preserved
for RSS) with an explicit value required for backfill.

**New CLI behavior.** `engine backtest`, when the DB has no cached news for
the requested range: tries Alpaca backfill first (if `ALPACA_API_KEY` is
set) and persists what it fetches to the DB so a repeat run over the same
window hits the cache; only falls back to live RSS (with the existing
"this will NOT match your window" warning) if no Alpaca key is configured
or the Alpaca call fails auth.

---

## 2026-07-20 -- Add: consequence-prediction forward-test pipeline + universe diversification

**Consequence prediction (`engine.prediction`).** A second, separate news
analysis mechanism alongside VADER sentiment and keyword topic routing: an
LLM (Claude, `engine/prediction/client.py`) reasons about indirect,
second-order consequences of a headline -- the "pandemic in China ->
cruise/airline stocks exposed" kind of connection that keyword matching
structurally cannot find. This does **not** feed any strategy, RiskGate, or
the backtester -- it's a standalone research instrument.

The core design problem: an LLM's training data already contains the
outcomes of real historical events, so backtesting this the normal way
would just measure recall dressed up as prediction. Fixed by making it a
forward-test log instead (`Prediction` table) -- every prediction is
written before its outcome is known, and a `forward_safe` flag (event
timestamp vs. the model's actual training-data cutoff, `ANTHROPIC_MODEL_
KNOWLEDGE_CUTOFF`) gates which rows may ever count as evidence of skill.
`resolve_pending_predictions` scores rows against real price data exactly
once, after the resolution window closes, and never revisits a resolved
row. Retrieval of topic-matched past *resolved* cases is fed back into the
prompt as precedent -- grounding on the pipeline's own verified track
record rather than the model's training corpus, so it doesn't reintroduce
the same hindsight problem. Full reasoning: `docs/prediction_pipeline.md`.

New CLI: `engine predict-news`, `engine resolve-predictions`, `engine
predictions-report`. Requires `ANTHROPIC_API_KEY` and (not guessable, ships
as a refuse-to-run placeholder) `ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF`.

**Universe diversification.** Tier 1 was 10 of 13 names in tech/tech-
adjacent sectors. Added UNH (healthcare), CAT (industrials), WMT (retail),
XOM (energy) -- same "reliable news magnet" selection logic as the
original picks, different sectors. Matching keyword-router patterns added.
Tier 1 is now 17 names.

---

## 2026-07-20 -- Fix: news-driven backtests were silently using today's news

**What happened.** Running `engine backtest --strategy overnight_gap
--start 2026-06-01 --end 2026-07-15` produced 0 trades. Root cause: free
RSS feeds (Yahoo Finance, MarketWatch, PRNewswire -- see
`engine/data/news.py`) have no historical archive, they only ever return
*currently live* items. The backtest command was calling the same live-RSS
fetch regardless of the requested date range, so a backtest over
2026-06-01..2026-07-15 was being fed news timestamped ~2026-07-20 (today) --
outside the bar window entirely, so nothing could ever fill.

**Fix.** `engine ingest` already persisted fetched news to Postgres
(`NewsItemRecord`, with real `published_at`/`ingested_at`) but nothing read
it back. Added `engine.journal.registry.load_news_items(session, start,
end)` and wired `engine backtest` to read historical news from the journal
DB first, falling back to a live RSS fetch (with an explicit stderr
warning that the result won't match the requested window) only when the DB
has nothing for that range. This means: **news-driven backtests are only
meaningful over date ranges where `engine ingest` was actually run while
that news was current.** There is no way to backfill genuine historical
news from free RSS after the fact -- that's an inherent limit of the
source, not a bug to fix later. A real historical news corpus has to be
built by running `engine ingest` regularly (e.g. a scheduled job) starting
now, going forward.

**Anti-self-deception note:** this is exactly the kind of thing the bias
review process exists to catch -- a strategy that "looks dead" (0 trades)
can be a data-availability bug, not evidence the strategy has no edge.
Always check *why* a backtest produced zero trades before concluding
anything about the strategy itself.

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
