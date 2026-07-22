# Anticipatory Prediction Mode (Polymarket-calibrated)

Status: implemented (2026-07-22), first pass. Companion to the existing
reactive pipeline in docs/prediction_pipeline.md -- read that first, this
assumes it. See "V1 implementation notes" at the end for what shipped,
what was deliberately simplified, and what's still open.

## Motivation

The reactive pipeline (engine.prediction.*) reacts to fresh headlines with
second-order consequence reasoning on a short (~24h) horizon. Real example
that validated it works: "Danaher Plummets After One Segment Comes In
'Surprisingly Soft'" correctly fanned out into differentiated sector-peer
calls (AVTR, RGEN, WAT, RVTY, A), each with its own rationale, not a
copy-paste restatement.

But a separate class of theses breaks this design: anticipatable
political/policy/regulatory events (e.g. "if candidate X wins, they invest
in green energy, so green-energy names benefit"). These fail not because
the reasoning chain is wrong, but because the policy stance is public for
months before the event -- the market has already priced it in gradually.
Reacting to the outcome headline captures ~zero edge. The real edge is in
the *gap* between what the market has priced in and what should actually be
priced in, tracked continuously before the event resolves -- which needs a
different mode, not a tweak to the reactive one.

## Two tracks, one spine

- **Reactive engine (existing, unchanged):** headline -> second-order
  consequence -> short-horizon trade. engine.prediction.*, Prediction table.
- **Anticipatory engine (new, this doc):** hypothesis on a not-yet-resolved
  event -> own probability estimate -> compare to Polymarket's price ->
  position sized to the gap -> revise as evidence arrives -> close at
  resolution or when the gap closes.

**Shared** (reuse, do not duplicate): execution/risk layer (RiskGate,
TradeRecord, broker order submission), the LLM-call plumbing (same
claude-CLI invocation pattern as the reactive pipeline), ticker-suggestions
/ off-universe symbol discovery, the dashboard.

**Not shared:** the data model and the trigger loop. Reasons below.

## Event selection: forward from Polymarket's own catalog, not backward

- Pull Polymarket's live markets via their API (free/public).
- Category filter: drop irrelevant categories (sports/entertainment) using
  Polymarket's own tags.
- Relevance filter: keep markets with plausible equity/ETF exposure -- reuse
  the same model-judgment approach the reactive pipeline already uses for
  headline relevance, or keyword/topic-tag matching.
- Backward selection (start from a sector/stock, invent a hypothesis) is
  explicitly rejected: there's no guarantee a real market exists to
  calibrate against, so there'd be nothing to compare the model's
  probability to -- no priced gap, no signal. Forward-only guarantees a
  real, continuously-updating reference price always exists.

## Probability calibration

- Polymarket's share price *is* the market-implied probability already (0-1,
  pays $1 if YES resolves true) -- no conversion needed.
- The model independently estimates its own probability (`P_model`) for the
  same event. Do this *before* looking at Polymarket's current price for
  that market, to avoid anchoring the estimate to the number it's supposed
  to be checked against.
- `gap = P_model - P_market`. No gap (or below some minimum threshold), no
  trade -- a directionally "correct" thesis the market already agrees with
  is worthless, same lesson as the green-energy example.

## Which asset, which direction, how much

- Same second-order "who is exposed" reasoning the reactive engine already
  does, applied to the hypothesized event's outcome instead of a realized
  headline.
- Direction: long the exposed name if `P_model > P_market` (we think the
  favorable outcome is underpriced), short a name exposed to the opposite
  outcome if `P_model < P_market`.
- Trade target is always the underlying equity/ETF -- never the Polymarket
  contract itself (see open items: regulatory access is unresolved, and
  this pipeline's execution layer is Alpaca-only anyway).
- Size scales with gap magnitude x confidence, discounted for
  time-to-resolution (capital tied up longer needs a wider gap to justify
  the position).

## Ongoing revision loop (the core new mechanic)

- Per-hypothesis polling of Polymarket price -- not the reactive loop's
  fixed hourly RSS cadence. Poll each *open* hypothesis's market, act only
  on a meaningful price delta, mirroring the same "act on delta, not
  snapshot" principle already used for headline near-duplicates
  (engine.journal.registry.headline_near_duplicate).
- The reactive engine's headline stream is a legitimate input here too: a
  headline can move `P_model` for an open hypothesis (new evidence) even
  when Polymarket's own price hasn't moved yet. This is the one real
  coupling point between the two tracks -- headlines feed the anticipatory
  engine's belief revision; the two systems don't otherwise merge.
- On each re-check: re-estimate `P_model`, recompute the gap against the
  current `P_market` -> add to the position (gap grew), trim/exit (gap
  closed, market caught up, edge already captured), or cut (new evidence
  invalidates the thesis, a stop-loss equivalent).
- Close the position at hypothesis resolution regardless of the above.

## Data model: new tables, not an extension of Prediction

`Prediction` (engine.journal.models.Prediction) stays exactly as-is:
write-once, resolved exactly once via `resolve_prediction`, gated by
`forward_safe`. That write-once-then-never-edited shape is deliberate
integrity machinery for a single reactive call and must not be reused here
-- an anticipatory bet needs to be revised repeatedly over its life, which
the existing shape cannot represent honestly.

New tables needed (naming indicative, not final):

- **`Hypothesis`** -- one row per tracked Polymarket market/thesis: market
  id, question text, symbol(s) judged exposed, opened_at, status
  (open/closed), resolution outcome once known.
- **`HypothesisBelief`** -- append-only, one row per re-estimation:
  timestamp, `P_model`, `P_market` at that moment, gap, confidence,
  rationale, position-size decision made from it. Never edited after
  written, same anti-hindsight principle that makes `Prediction` trustworthy
  today, just shaped for a persisting belief instead of a single call.
- Position tracking reuses `TradeRecord`, linked by `hypothesis_id` instead
  of a prediction id.

## Open items to verify before/while building

- Polymarket's actual category/tag taxonomy -- not yet checked against
  their real API, was only assumed in earlier discussion.
- Read-only market-data API access from the US: Polymarket has real trading
  restrictions and a 2022 CFTC settlement. The API being free/public does
  not by itself confirm unrestricted read access for a US-based user --
  worth an explicit check, separate from cost.
- Decide and enforce the exact order of operations so `P_model` is always
  produced before `P_market` is revealed to the model for that same
  re-estimation call, to keep the anchoring concern above real rather than
  theoretical.

## Explicitly out of scope for this mode

- Trading the Polymarket contract directly (equity/ETF only).
- Backward-generated hypotheses with no underlying real market.
- Any change to the existing reactive `Prediction` table or its semantics.

## V1 implementation notes (2026-07-22)

**Open items above, resolved:**
- Polymarket read access checked against the real API: the Gamma API
  (`/events`, `/markets`) is unauthenticated and unrestricted from the US
  for reads. The CFTC/geographic trading restriction only applies to
  placing orders on Polymarket's CLOB, which this mode never does -- so
  it doesn't apply here. `engine.data.polymarket` was written and tested
  directly against the live API, not guessed against documentation.
- `P_model` is produced with no market price shown to the model at all
  (`estimate_hypothesis(question, description)` takes no price
  parameter) -- structurally impossible to anchor, not just
  order-of-operations discipline.
- Category/tag taxonomy: `EXCLUDED_TAG_SLUGS` in `engine.data.polymarket`
  is a first-pass heuristic tuned against real `/events` output (sports,
  entertainment, crypto, esports), not an exhaustive catalogue -- expect
  to extend it as false positives/negatives show up in practice.

**Position tracking:** ended up NOT reusing `TradeRecord` as originally
sketched above. `TradeRecord.run_id` is a required FK to `experiment_run`
and is backtest-only today -- no live/paper trading path in this codebase
writes `TradeRecord` rows (`engine.prediction.trading` doesn't either).
Reusing it would have been a new integration, not an extension of an
existing one, and would have meant either a schema migration (nullable
`run_id`) or a synthetic `ExperimentRun`. Instead, `Hypothesis` tracks its
own position directly (`position_side`, `traded_order_id`,
`traded_quantity`, `exit_order_id`) -- the same pattern `Prediction`
already uses for the reactive engine's trades.

**Deliberate V1 simplifications (not gaps to fix silently -- flag before
changing):**
- Sizing is binary: a hypothesis is either flat or fully positioned.
  `HypothesisAction.ADDED`/`TRIMMED` are defined for a future partial-
  sizing pass but the revision logic (`engine.anticipatory.pipeline.
  _decide_action`) never produces them -- a still-significant same-
  direction gap on an already-open position is `HELD`, not added to.
- No time-to-resolution discount in sizing, despite the design sketch
  above suggesting one -- `_open_position` sizes purely from gap
  magnitude (relative to `min_gap_threshold`) x confidence.
- Resolution outcome is inferred from a closed market's settled price
  (`> 0.5` = YES) rather than a dedicated "which side won" field --
  Polymarket's Gamma read API doesn't expose one.
- `direction_if_yes` is fixed once at hypothesis creation from the
  initial LLM call and never re-derived on revision -- only `P_model`
  gets re-estimated each cycle. If the causal story genuinely reverses
  over an event's life, this mode won't notice; only the reactive
  engine's per-headline analysis re-derives symbol/direction fresh.

**Where the code lives:** `engine.data.polymarket` (read client),
`engine.anticipatory.pipeline` (discovery + belief revision, decide-only),
`engine.anticipatory.trading` (RiskGate-gated order execution),
`engine.journal.models.Hypothesis`/`HypothesisBelief`/
`AnticipatoryLoopConfig`, `engine.cli.main.anticipatory_loop` (the loop,
mirrors `predict_loop`'s shape exactly), dashboard `/hypotheses` +
`/anticipatory-loop-config`.
