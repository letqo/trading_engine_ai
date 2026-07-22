# JOURNAL.md

Every strategy change, its motivation, and its journaled experiment ID (per
SPEC.md's working agreement). Newest first.

---

## 2026-07-22 -- Add: dashboard System Status panel, loop heartbeats, and fixed a genuinely dead audit trail

**Why.** User's own words: "I need to see what's happening, that's the role
of a dashboard." Also found while diagnosing a real confusion (dashboard
showed `ALPACA_API_KEY not set` after the user had already set it): the
`dashboard` Railway service had never actually been given
`ALPACA_API_KEY`/`ALPACA_API_SECRET` at all -- copied over from
`predict-loop`, redeployed, confirmed fixed. Worth restating clearly since
it caused real confusion: the dashboard is read-only and never places
trades itself, so this only ever broke *display*, never trading capability
-- predict-loop/worker/anticipatory-loop each hold their own separate
credential and were unaffected the whole time.

**Two real gaps found while checking what a status panel could honestly
show, both fixed, not just displayed around:**
1. **No heartbeat existed.** Nothing recorded "predict-loop last actually
   ran a cycle at X" -- Railway's `ON_FAILURE` restart policy can show
   "Online" for a service that's actually crash-looping. Added
   `last_cycle_at` to `PredictLoopConfig`/`AnticipatoryLoopConfig`
   (migration `a722f3ec8a05`), stamped once per loop iteration --
   deliberately *before* the pause check, so a paused-but-alive loop still
   heartbeats and reads as "paused," not "STALE" (no heartbeat at all
   correctly still reads as unknown/stale -- a real, defensible distinction
   surfaced while writing the tests for this).
2. **`record_halt` was dead code.** Defined, rendered by `/risk-events`,
   never once called. A real kill-switch or daily-drawdown halt in
   `papertrade`/`predict-loop`/`anticipatory-loop` would flatten positions
   and log to Railway's own logs, but the dashboard's audit trail would
   stay empty forever. Wired up at all six halt points (kill-switch +
   drawdown breach, x3 loops).

**Dashboard:** new "System status" section on `/` (overview) -- kill
switch (engaged/clear), each loop's enabled/paused + last-heartbeat with a
staleness check (no heartbeat within 2x its own poll interval = STALE),
Alpaca credential presence, and recent halts (now actually populated).

**One bug caught before it shipped, same class as the enum migration
bug earlier this session:** `_system_status()` initially returned
`PredictLoopConfig`/`AnticipatoryLoopConfig` ORM objects fetched inside a
`with get_session(...)` block, for use in the template *after* that block
closed -- SQLAlchemy expires all of a session's tracked objects on
`commit()`, so any attribute access after the session closes raises
`DetachedInstanceError`. Same bug independently resurfaced in
`mark_predict_loop_cycle`/`mark_anticipatory_loop_cycle`'s own commit
expiring the `config` object the CLI loops keep reading for many lines
after the session block closes (`config.enabled`, `config.poll_seconds`,
etc.) -- 13 CLI tests caught this immediately. Fixed by having both mark_*
functions `session.refresh()` and return the row directly, so callers get
one already-fresh object instead of a separate, now-stale one.

**Full test suite: 301 passed** (up from 292), `ruff check` clean.

---

## 2026-07-22 -- Add: Anticipatory Prediction Mode (Polymarket-calibrated), first implementation

**What changed.** Built the full design in `docs/anticipatory_prediction_mode.md`
end to end: a second, parallel prediction mode alongside the existing
reactive headline-consequence engine. Instead of reacting to a fresh
headline, this mode tracks live Polymarket prediction markets as ongoing
"hypotheses," has the LLM independently estimate its own probability for
each (never shown the market's price, to avoid anchoring), and trades the
gap against the market-implied probability on the underlying equity/ETF
via the existing Alpaca paper-trading layer -- never the Polymarket
contract itself.

**New pieces:** `engine.data.polymarket` (read-only Gamma API client,
verified live against the real API, not just documentation --
unauthenticated/unrestricted from the US for reads, unlike CLOB order
placement which this project never touches); `Hypothesis`/
`HypothesisBelief`/`AnticipatoryLoopConfig` tables + migration
(`44e2f028ba06`); `engine.anticipatory.pipeline` (discovery + belief
revision -- decides and journals only, never trades) and
`engine.anticipatory.trading` (RiskGate-gated order execution, same
trust boundary as every other order path -- SPEC.md hard constraint #2);
a new `HypothesisEstimate` LLM schema + `estimate_hypothesis()` method on
both prediction-client backends; the `anticipatory-loop` CLI command,
structurally identical to `predict_loop` (kill-switch-first, config
re-read every cycle, pause-without-exit, unhandled-exception-logs-and-
continues); dashboard `/hypotheses` and `/anticipatory-loop-config`.

**Two real Postgres-specific production issues, both found and fixed
during this deploy, neither visible from the local SQLite test suite:**

1. **Shared-enum migration bug, and `create_type=False` didn't fix it.**
   `Hypothesis.direction_if_yes` reuses the existing `PredictionDirection`
   enum. A fresh Alembic-autogenerated `sa.Enum(name='predictiondirection')`
   in a *new* migration defaults to `create_type=True`, which tries to
   `CREATE TYPE predictiondirection` again (already exists, created by
   the original `add_prediction_table` migration) -- caught this before
   shipping and "fixed" it with `create_type=False`. That flag turned out
   not to reliably suppress the DDL emission for this SQLAlchemy/Alembic
   combination against real Postgres -- `alembic upgrade head` against
   production still failed with `DuplicateObject: type
   "predictiondirection" already exists`. The actual fix: declare the
   column as a plain string in `op.create_table`, then convert it with
   raw SQL (`ALTER TABLE hypothesis ALTER COLUMN direction_if_yes TYPE
   predictiondirection USING direction_if_yes::predictiondirection`),
   which bypasses SQLAlchemy's enum DDL compiler entirely. Verified for
   real against production Postgres on a throwaway scratch table via SSH
   before committing, not just asserted -- see
   `alembic/versions/44e2f028ba06_...py`. The failed attempt rolled back
   cleanly (Postgres transactional DDL); nothing was left partially
   applied.
2. **The git-push-triggered Railway deploy did not run the release
   command either.** Checking `alembic current` against production
   before this fix revealed it was still at `1b5715fb9232` -- meaning
   even the *previous* deploy's migration (`e75c8c2b0b69`,
   `add_prediction_topic_table`, from earlier this session) had silently
   never applied via the automatic git-push deploy, despite that deploy
   showing "Online" the whole time. The earlier documented incident
   (`railway up --detach` skipping `releaseCommand`) turns out not to be
   specific to manual CLI deploys -- automatic deploys aren't reliably
   running it either, at least in this project's current Railway
   config. Lesson generalized: never trust "Online" status as proof a
   migration applied, for *any* deploy path; always verify with `alembic
   current` via SSH and run `alembic upgrade head` manually after any
   deploy that includes a schema change.

**Two deliberate design deviations from the original sketch, decided
during implementation, not upfront:**
1. Position tracking does NOT reuse `TradeRecord` as the design doc
   originally suggested -- `TradeRecord.run_id` is a required FK to
   `experiment_run` and is backtest-only today (no live/paper path,
   including the existing reactive engine's own trading module, writes
   `TradeRecord` rows). `Hypothesis` tracks its own position directly
   instead (`position_side`/`traded_order_id`/`traded_quantity`/
   `exit_order_id`), mirroring how `Prediction` already does this.
2. The retrieval "guarantee a past miss" mixing logic (added earlier this
   session, unrelated) prompted the same question here -- resolved by
   keeping V1 binary (flat or fully positioned, `HypothesisAction.ADDED`/
   `TRIMMED` defined but unused) rather than building partial position
   sizing on the first pass.

**Full test suite: 292 passed** (up from 232), `ruff check` clean across
all new and touched files. See `docs/anticipatory_prediction_mode.md`'s
new "V1 implementation notes" section for the complete list of what
shipped vs. what's still a known simplification.

---

## 2026-07-22 -- Guarantee a past miss in the LLM's few-shot context; add dashboard filters to /predictions

**The retrieval concern.** `load_resolved_predictions_by_topics` (the
few-shot past-case retrieval feeding every new prediction) picked the top
`limit` (default 5) most recent topic-matching resolved predictions --
pure recency, unchanged from before this session's indexing fix. Raised
after shipping that fix: if the most recent 5 topic matches all happened
to be correct, the model would never see its own mistakes on that topic,
which is arguably the most useful thing to show it for self-correction.

**The fix.** `load_resolved_predictions_by_topics` now guarantees the
single most recent *incorrect* (`outcome_correct == False`) match is in
the returned set, if one exists and isn't already in the recency window
-- it replaces the oldest slot of the recency window rather than being
appended, so `limit` still hard-caps the token cost per prediction (this
runs on every headline predict-loop analyzes). Selection is otherwise
unchanged: pure recency when no correction-worthy miss exists.

**Dashboard filters.** `/predictions` previously showed only the most
recent 100 rows with no way to narrow them. Added a GET filter form
(symbol, correct/incorrect, date range) -- `_prediction_stats` now takes
optional `symbol`/`outcome`/`start`/`end` filters applied at the SQL
`WHERE` level to both the summary cards and the row list, so the numbers
shown always describe exactly what's filtered into view. `/` (overview)
is unaffected -- it calls the same function with no filters.

---

## 2026-07-22 -- Fix: Sharpe annualization bug; walk-forward suite rerun confirms the 2026-07-21 finding; a reproducibility gap found along the way

**The bug.** `engine.backtest.metrics.sharpe_ratio` hardcoded
`periods_per_year=252` (one point per trading day) and nothing ever
overrode it. But `BacktestEngine` appends one `EquityPoint` per `BAR`
event, and a full-universe backtest emits one `BAR` event per `(symbol,
timestamp)` pair -- a 27-symbol universe produces ~27 equity points per
calendar day, not 1, and `overnight_gap`'s runs used hourly bars on top of
that. `sqrt(252)` was the wrong annualization factor either way. Found
while verifying the 2026-07-21 walk-forward result before trusting it for
anything downstream -- `sharpe_ratio` was, tellingly, the one function in
`metrics.py` with zero test coverage.

**The fix.** `sharpe_ratio` now (a) collapses same-timestamp
`EquityPoint`s to the last one seen, so a multi-symbol universe's
per-symbol bar events don't inflate the return series, and (b) derives
`periods_per_year` from the curve's own elapsed wall-clock time (365.25
calendar days/year) instead of a hardcoded constant, so daily and hourly
runs are each annualized correctly with no call site needing to remember
which constant to pass. Added hand-computed test coverage that didn't
exist before (`tests/test_metrics.py`). See `docs/bias_review.md`'s new
2026-07-22 section for the full writeup.

**Reran the full dev+validation walk-forward suite (14 runs) with the
corrected metric to check whether the fix changes the 2026-07-21
conclusion.**

Validation period (2025-07-21..2026-07-20), corrected Sharpe:

| Strategy | Return | Sharpe |
|---|---|---|
| buy_and_hold | 5.28% | 1.55 |
| random_entry | 3.46% | 0.91 |
| momentum | 2.72% | 0.78 |
| multi_factor | 1.05% | 0.32 |
| dumb_news | 0.28% | 0.10 |
| mean_reversion | -1.92% | -0.49 |
| overnight_gap | -0.74% | -1.18 |

**Conclusion unchanged: no strategy beats both baselines on validation
Sharpe.** Both baselines still outrank every real strategy. The bug did
not flip the ranking.

**A second, separate issue surfaced by the rerun, not caused by the
Sharpe fix.** A few strategies' *return and trade-count* numbers shifted
between the original 2026-07-21 run and this rerun, one day later --
most strikingly `random_entry` (dev-period return +1.15% -> -6.51%) and
`multi_factor` (8,311 -> 8,327 trades). Neither total_return_pct nor the
strategies' own logic changed. Root cause: `engine backtest` re-fetches
live data from yfinance on every invocation with no pinned snapshot --
the `DataSnapshot`/`engine ingest` mechanism that's supposed to guarantee
reproducibility exists but `backtest` doesn't actually use it. A single
day's data drift from Yahoo was enough to cascade into different outcomes,
especially for `random_entry` (threshold-random per bar -- any tiny data
shift reshuffles which bars trigger entries). **Backtest runs are not
currently perfectly reproducible run-to-run.** Decision: document as a
known limitation for now rather than fix immediately -- it didn't change
the validation conclusion this time, and wiring `backtest` to reuse a
`DataSnapshot` is a separate, real piece of work for later.

**One broken sub-run.** `overnight_gap`'s dev-period backtest
(2024-07-22..2025-07-20, `--interval 1h`) returned 0 bars, 0 trades --
yfinance's hourly-data cap is a rolling "last 730 days from *today*," and
the one day that passed since the original run pushed that same fixed
window just outside the limit. Its validation-period run was unaffected
and matches the original run exactly (return -0.74%, 264 trades). This
will recur for any fixed past hourly window as time passes -- a
structural ceiling of the free data source, not a one-off glitch; not
re-run here since the dev period doesn't bear on the validation
conclusion above.

---

## 2026-07-21 -- Add: subscription-backed prediction client (CLAUDE_CODE_OAUTH_TOKEN), with an honest open problem

**What changed.** `ConsequencePredictionClient` (metered API key, via the
Python SDK) now has a sibling, `engine.prediction.cli_client.
ClaudeCLIPredictionClient`, authenticating with a `claude setup-token`
long-lived OAuth token against a Claude subscription instead of per-call
billing -- matches the earlier decision (this session) that a
subscription is fine for real usage once proven, not just local testing.
`engine.prediction.factory.build_prediction_client` picks between the
two based on which credential is set (OAuth token wins if both are
present, since a deliberately-configured subscription shouldn't lose
silently to a leftover test API key); both CLI call sites in
`engine/cli/main.py` now go through the factory instead of constructing
`ConsequencePredictionClient` directly.

**This was actually verified live, not just unit-tested against
assumptions** -- a real token became available this session. Two real
bugs found and fixed in the process, both about the `claude` CLI
specifically, neither obvious from its `--help` text:
1. `subprocess.run(["claude", ...])` fails on Windows -- npm installs a
   `.CMD` shim there, and Windows' `CreateProcess` doesn't do the
   PATHEXT-aware resolution a shell would. Fixed with `shutil.which`,
   resolved once at construction so a missing CLI fails fast.
2. Running with `cwd` inside this repo lets Claude Code auto-discover
   `CLAUDE.md`/`.env` and the model responds *about this codebase*
   instead of analyzing the headline it was asked about. Fixed by
   running with `cwd` set to a neutral temp directory.

**One real problem remains open, and matters more than the two above.**
Even with a neutral cwd and a completely ordinary, unambiguous headline
("Fed raises rates 0.25pp -- ordinary financial news, not evocative or
sensitive in any way), `claude -p` responds with a clarifying question
about user intent instead of following the system prompt and
`--json-schema` constraint. This reproduced identically on an evocative
headline (disease-outbreak framing) and a mundane one, ruling out "the
model is being cautious about sensitive content" as the explanation --
it looks structural: Claude Code's own intent-classification layer (see
`claude auto-mode`) appears to intercept short, non-coding-task-shaped
prompts before the system prompt gets to drive behavior the way the raw
Messages API does. `--bare` mode (which disables extra harness behavior)
was tried as a fix and made things worse -- it also strictly disables
OAuth/keychain auth, so it's incompatible with this credential entirely.

**Net effect, left as-is deliberately rather than half-fixed:** this
backend will likely raise `ClaudeCLIError` on most real headlines right
now. That's a safe failure mode, not a dangerous one -- `predict-loop`'s
per-cycle exception handling logs it and moves to the next headline
rather than crashing, so nothing breaks, but no real predictions get
produced either. Shipped anyway because (a) it's the credential path
actually available tonight, (b) the failure is loud and logged, not
silent, and (c) the fallback (`ANTHROPIC_API_KEY`, unaffected by any of
this since it calls the Messages API directly, no CLI harness involved)
remains a one-env-var swap away with zero code changes. Needs either a
different invocation approach or accepting the API-key path as primary.
Do not read "the predict-loop service is running" as "this backend
works" until this is resolved -- check the dashboard/logs for actual
predictions being produced, not just service uptime.

---

## 2026-07-21 -- Fix: migrations weren't applied on Railway; root cause still open

**What happened.** Deploying the dashboard service surfaced `relation
"prediction" does not exist` against the real production Postgres --
discovered by actually hitting the live dashboard with curl and reading
its logs, not by assuming a clean deploy meant a working one. The
`worker` service's earlier clean startup logs never caught this because
startup reconciliation only touches broker/account state, never the
`prediction`/`experiment_run`/etc. tables.

**Immediate fix, not in question:** ran the migration once by hand
directly against the real Railway Postgres (via its public proxy URL).
Production schema is correct as of this entry.

**The root-cause investigation went sideways and is worth recording
honestly.** First hypothesis: Railway renamed `releaseCommand` to
`preDeployCommand` (array syntax) and silently ignores the old key. Acted
on that immediately -- changed `railway.toml` to `preDeployCommand =
["alembic", "upgrade", "head"]`, pushed, and it made things *worse*: all
three services' configs failed to parse entirely
(`fileServiceManifest.deploy` came back null from Railway's own API),
blocking every future deploy, not just the migration step. Caught by
inspecting `railway status --json` after the push rather than assuming
the fix worked. Reverted to `releaseCommand` and confirmed via
`propertyFileMapping` in that same JSON output that Railway's TOML
schema *does* still recognize `releaseCommand` -- so the rename claim
(from a web-docs fetch) was either JSON-config-only, describes a schema
not yet live for TOML, or was simply wrong. Reverting restored the
ability to deploy at all. Why `releaseCommand` didn't execute the
migration in the first place remains unresolved -- possibly specific to
how the two newly-added services (`dashboard`, `predict-loop`) inherited
config versus `worker`'s original connection, possibly something else.
Not blocking (schema is correct by hand, and future schema changes will
surface the same way if it recurs), but flagged rather than quietly
assumed fixed.

**Lesson applied going forward:** verifying a deploy means checking that
the *data path* actually works (hit a real endpoint that queries a real
table) and that the *next* deploy still succeeds after a config change --
not just that the current container starts and logs look clean.

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

---

## 2026-07-21 -- Item 5: full walk-forward backtest suite (dev + validation)

All 5 live-eligible strategies (`dumb_news`, `overnight_gap`, `momentum`,
`mean_reversion`, `multi_factor`) plus both baselines (`buy_and_hold`,
`random_entry`) run over a development period (2020-01-01..2025-07-20) and,
in one deliberate pass, the held-out validation period
(2025-07-21..2026-07-20), per SPEC.md's "touched only for final validation,
at most a few times ever" rule. Each `--validation` run is logged in the DB
via `--validation-reason`.

**Three real bugs found and fixed along the way -- not cosmetic, each one
would have quietly corrupted results if missed:**

1. **Alpaca News API 429s had no retry.** A 5.5-year, 27-symbol backfill
   routinely hit the rate limit and crashed outright. Added
   exponential-backoff retry (`engine/data/alpaca_news.py`, up to 6
   attempts) specific to 429s; 401/403 still fail fast as auth errors.

2. **The 500-page pagination cap silently truncated multi-year fetches.**
   `_MAX_PAGES` was a budget for the whole `[start, end]` range, not a
   safety net -- the first `dumb_news` dev-period run hit it and, because
   `sort=asc`, silently dropped everything after 2021-01-11 out of a
   requested 2020-01-01..2025-07-20 range (150,909 real articles vs. the
   25,000 it actually fetched). The resulting backtest looked *good*
   (return 6.79%, Sharpe 0.15) purely because it was unknowingly trading
   only the 2020-2021 COVID-recovery bull run. Fixed by chunking the fetch
   into 90-day windows, each with its own page budget, then deleted the
   truncated cache rows and re-ran. Corrected result: return -0.47%, Sharpe
   -0.00. **The "good" number was a data-truncation artifact, not a
   finding** -- exactly the kind of self-deception this project's process
   exists to catch, and it would have gone unnoticed without checking why
   the fetch logged a cap warning.

3. **`load_news_items` returned naive datetimes on a second read of an
   already-cached range**, crashing `build_event_stream`'s sort
   (`TypeError: can't compare offset-naive and offset-aware datetimes`)
   the moment `overnight_gap` tried to backtest the same range `dumb_news`
   had just warmed the cache for. SQLite drops tzinfo on datetime
   round-trip regardless of the column's declared type; `_hours_elapsed`
   already had a workaround for this, `load_news_items` didn't. Fixed with
   a shared `_as_utc` helper in `engine/journal/registry.py`.

**A fourth issue, structural rather than a bug to fix:** `overnight_gap`
produced exactly 0 trades over the 2020-2025 daily-bar dev period. Root
cause: `_entry_signals` only fires when `ctx.timestamp.hour >=
US_MARKET_OPEN_UTC_HOUR (14)`, but daily bars from yfinance always carry
`hour == 0` -- this strategy is structurally incompatible with `--interval
1d`, full stop, not something the perturbation/tuning knobs can fix. It
needs intraday bars to ever act. yfinance hard-caps hourly history to the
last 730 days, so `overnight_gap`'s dev period had to be shrunk to
2024-07-22..2025-07-20 (`--interval 1h`) instead of the 5.5-year range
every other strategy used -- the best available window that doesn't
overlap the reserved validation period. This is a real data-availability
ceiling, not a workaround to revisit later without a paid intraday data
source.

### Results

**Development period (2020-01-01..2025-07-20, `--interval 1d` except
overnight_gap which used `1h` over 2024-07-22..2025-07-20):**

| Strategy | Return | Max DD | Sharpe | Win rate | PF | Trades | Fragile? |
|---|---|---|---|---|---|---|---|
| buy_and_hold | -0.51% | 1.21% | -0.05 | 0.0% | 0.00 | 6 | -- |
| random_entry | 1.15% | 10.39% | 0.02 | 27.6% | 1.06 | 5,869 | -- |
| dumb_news | -0.47% | 10.41% | -0.00 | 30.1% | 1.03 | 3,762 | No |
| overnight_gap | -0.81% | 1.30% | -0.12 | 48.6% | 0.81 | 247 | No |
| momentum | -15.25% | 17.69% | -0.18 | 23.3% | 0.96 | 8,133 | No |
| mean_reversion | -29.65% | 30.77% | -0.44 | 27.4% | 0.83 | 6,493 | No |
| multi_factor | -22.18% | 23.34% | -0.31 | 23.2% | 0.91 | 8,311 | No |

**Validation period (2025-07-21..2026-07-20, held-out, first use):**

| Strategy | Return | Max DD | Sharpe | Win rate | PF | Trades |
|---|---|---|---|---|---|---|
| buy_and_hold | 5.28% | 2.14% | 0.30 | 0.0% | 0.00 | 5 |
| random_entry | 3.46% | 2.79% | 0.21 | 27.1% | 1.19 | 1,037 |
| dumb_news | 0.28% | 3.63% | 0.02 | 29.7% | 1.06 | 748 |
| overnight_gap | -0.74% | 1.13% | -0.10 | 51.5% | 0.86 | 264 |
| momentum | 2.73% | 2.56% | 0.17 | 24.7% | 1.15 | 1,514 |
| mean_reversion | -1.92% | 4.39% | -0.11 | 29.1% | 0.96 | 1,127 |
| multi_factor | 1.05% | 2.80% | 0.07 | 24.5% | 1.09 | 1,411 |

### Honest interpretation

**None of the 5 deterministic strategies beat both baselines' Sharpe ratio
in the validation period, and none beat `buy_and_hold` in either period.**
`buy_and_hold` (0.30) and `random_entry` (0.21) both outrank every real
strategy on validation Sharpe; the closest real strategy is `momentum`
(0.17), still below both. `dumb_news` (0.02) and `multi_factor` (0.07) are
roughly flat. `overnight_gap` (-0.10) and `mean_reversion` (-0.11) are
negative on the exact window that matters most (the one never touched
until this run). None of the dev-period perturbation checks flagged any
strategy as fragile, so this isn't a parameter-sensitivity artifact --
the dev-period numbers (all substantially worse, -15% to -30% return for
the factor-based strategies) reflect the 2020 COVID crash and 2022
drawdown sitting inside that window, which the shorter validation window
avoids.

**Conclusion: this backtest suite found no validated edge for any of the
5 deterministic strategies over naive baselines.** That is a real result,
not a setback to explain away -- it's exactly the outcome the "beat both
baselines after costs, same universe, same window" bar in this project's
own process (see the Overnight-Gap note above, 2026-05-xx entries) was
designed to catch before anything trades with real signal-driven logic.
None of these 5 strategies should be handed real (even paper) capital on
the strength of this data. This does not touch the separate LLM
consequence-prediction pipeline (`engine.prediction`), which was never
claimed to be validated this way and has its own forward-test-only
evidence bar (see "What 'skill' would actually look like" in
`docs/prediction_pipeline.md`) -- that pipeline's evidence, once it
accumulates enough forward-safe resolved predictions, is the more
promising track going forward.

Per SPEC.md, the validation period is now considered touched and should
not be re-run casually -- any future re-validation needs its own
deliberate `--validation-reason` and should be treated as a rare event,
not a tuning loop.

---

## 2026-07-21 (later) -- Fix: predict-loop had been crash-looping in
production since deploy; "CLI intent-classifier problem" was a red herring

**What happened.** Checked the live `predict-loop` Railway service's logs
directly (not just its "Online" status, which only reflects the container
staying up under `restartPolicyType = ON_FAILURE`) and found it had been
restarting every ~5 seconds since deploy, never once reaching a real
`analyze()` call:

```
ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF is unset or still the placeholder
(1970-01-01). Set it to the actual training-data knowledge cutoff date...
```

**Root cause, part 1:** `ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF` was never set
on the deployed service -- an oversight from when `predict-loop` was
first created, not something either backend's code was doing wrong. Fixed
by looking up the actual value (not guessing, per this field's whole
reason for existing): Anthropic's docs
(platform.claude.com/docs/en/about-claude/models/overview, checked
2026-07-21) list claude-opus-4-8's training data cutoff and reliable
knowledge cutoff both as "Jan 2026" -- no exact day given, so used
2026-01-31 (the conservative end of that range, consistent with SPEC.md's
"start pessimistic" default). Set via `railway variables --service
predict-loop --set ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF=2026-01-31` and
documented in `.env.example`.

**Root cause, part 2, found immediately after fixing part 1:** the
service then failed a different way -- `ClaudeCLIError: claude CLI not
found on PATH`. The production image is `python:3.12-slim`; it never had
Node.js/npm installed, so `@anthropic-ai/claude-code` could never have
been present, regardless of `CLAUDE_CODE_OAUTH_TOKEN` being set correctly.
This means the entire OAuth/CLI backend had never actually run in
production -- every earlier "live" test of `ClaudeCLIPredictionClient`
(2026-07-21 morning entry, transport verified working, intent-classifier
problem discovered) was run locally on the user's Windows machine, never
inside the actual deployed container. Fixed by adding `nodejs`, `npm`, and
`npm install -g @anthropic-ai/claude-code` to the Dockerfile.

**Result: after both fixes, the first real production cycle ran clean.**
10 headlines in, 28 predictions out, correct causal reasoning across a
genuinely diverse set of stories (mortgage-rate news -> RKT/DHI, Julius
Baer earnings -> MS/UBS wealth managers, Mideast de-escalation ->
JETS/DAL/ITA travel & defense names, a chip-stock rally -> SOXX/AVGO/TSM,
the Reformation IPO -> RVLV). Cycle completed with no errors except one
harmless `yfinance` rate-limit on a single symbol (USO), which was caught
and logged as a skipped trade-sizing rather than crashing the cycle.

**Anti-self-deception note.** The "clarifying question instead of a
direct answer" problem documented earlier this same day
(`engine/prediction/cli_client.py` docstring, `docs/prediction_pipeline.md`)
never actually reproduced in production -- it's not clear yet whether
that was an artifact of local/Windows testing conditions or something
that could still resurface. Left the original investigation notes in
place rather than deleting them, with a note pointing to this entry as
the current status, per this project's own rule about not quietly erasing
a documented negative result just because a later test looked better.
**One clean cycle is not a track record.** Whether this pipeline has real
skill is still an open question to be answered by `predictions-report`
accumulating forward-safe resolved predictions over time, exactly as
`docs/prediction_pipeline.md`'s "What 'skill' would actually look like"
section already says -- nothing about this fix changes that bar.

**Process note:** this was only caught because "Online" status was
treated as necessary but not sufficient -- a service can restart-loop
indefinitely under `ON_FAILURE` and still show green. Checking actual
service logs (`railway logs --service <name>`), not just deployment
status, should be the standard way to verify anything deployed in this
project actually works, not just that it's technically running.

## 2026-07-22 (later) -- Add: per-symbol hypothesis cap + dashboard-tunable RiskGate overrides

Two small, separable asks. First: the user noticed two `/hypotheses` rows
for the same underlying (`USO`, up) from a single discovery cycle --
Polymarket's WTI-$110 and WTI-$120 threshold questions, each a genuinely
distinct market with its own `market_id`, both independently judged
relevant by the LLM. Not a bug (`discover_hypotheses` dedups by
`market_id`, correctly, since these really are different questions with
different real probabilities -- 0.04 vs 0.01), but a real design gap:
Polymarket routinely splits one commodity into several threshold markets
that all map to the same tradable symbol, so `max_open_hypotheses` could
end up spent entirely on correlated bets on one instrument rather than
diverse ideas. Added `AnticipatoryLoopConfig.max_open_hypotheses_per_symbol`
(default 2, dashboard-tunable at `/anticipatory-loop-config`), enforced in
`discover_hypotheses` (`engine/anticipatory/pipeline.py`) via a `Counter`
seeded from currently-open hypotheses. The cap is checked only *after* the
relevance LLM call returns a symbol -- the symbol isn't known before
asking, so the paid call's cost can't be avoided, only the resulting
Hypothesis/position creation is skipped.

Second, and larger: the user asked whether `RiskGate`'s limits (position
cap, exposure cap, stop-loss, daily drawdown, consecutive-loss halt,
overnight-positions) could be set manually from the dashboard too, "with
the possibility to activate the default mode." Until now `RiskGate` was
always built from `engine.config.settings.RiskLimits` -- env-var-only,
fixed for the life of the process, the same category of limitation
`PredictLoopConfig`/`AnticipatoryLoopConfig` were built to solve for their
own loops. Added `RiskGateConfig` (`engine/journal/models.py`), a
single-row live-tunable table mirroring `RiskLimits`' fields exactly, plus
a `use_defaults: bool` column (default `True`). `use_defaults=True` means
every live trading path ignores the row entirely and uses `Settings.risk`
-- "activate default mode" without losing whatever's been typed into the
override fields, so flipping back to manual later doesn't mean re-entering
every number.

New `engine.risk.resolve.resolve_risk_limits(settings, config) ->
RiskLimits` is the single place that decides which of the two wins.
Deliberately **not** wired into `engine backtest`/perturbation analysis --
those must keep building `RiskGate` straight from `settings.risk` so a
backtest result stays reproducible regardless of what's been tuned live in
production since. Wired into all five live-trading paths: `papertrade` and
the two research loops (`predict-loop`, `anticipatory-loop`) now re-read
`RiskGateConfig` and refresh `risk_gate.limits` in place every iteration/
cycle -- same live-tunable-without-redeploy pattern as the loops' own
configs -- while the two one-shot commands (`act-on-predictions`,
`resolve-predictions`) read it once before building `RiskGate`. New
`/risk-gate-config` dashboard page (GET/POST, same trust boundary as
`/predict-loop-config`: can retune risk limits, cannot place or cancel an
order directly) shows the current env defaults alongside the override
fields so "what wins right now" is never ambiguous.

Migration `7a2039bab008` adds `anticipatory_loop_config
.max_open_hypotheses_per_symbol` (plain integer column, `server_default`
so existing Postgres rows backfill cleanly) and creates `risk_gate_config`
-- no enum columns involved this time, so none of the Postgres
`create_type=False` issues from the `Hypothesis` migration applied here.
Verified by running `alembic upgrade head` against the local dev SQLite DB
(several revisions behind) and confirming both the new column and table
exist with the right shape before deploying.
project actually works, not just that it's technically running.
