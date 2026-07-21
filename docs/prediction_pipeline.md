# Consequence-prediction pipeline

A second, deliberately different mechanism from `engine.features.sentiment`
(VADER, headline tone) and `engine.data.router` (keyword topic tagging).
This asks an LLM to reason about **indirect, second-order consequences** --
the kind of connection that links "a new virus outbreak in China" to "cruise
line and airline stocks are exposed" without either company being named in
the headline. Neither VADER nor keyword matching can do this; it requires
actual causal reasoning over world knowledge.

## Why this can't be backtested the normal way

Every other strategy in this repo is validated by running it against
historical data (`engine backtest`). That doesn't work here: an LLM's
training data already contains the outcomes of real historical events, so
asking it to "predict" what happened after a headline it has already seen
the aftermath of isn't a test of foresight -- it's recall wearing a
prediction's clothes. Backtesting this component the way `engine backtest`
backtests a strategy would silently manufacture a track record that means
nothing.

## The fix: forward-testing, gated on the model's training cutoff

Instead of a backtest, this pipeline is a **forward-test log**
(`Prediction` table, `engine.journal.models`): every prediction is written
*before* its outcome is known, timestamped, and never edited except once,
by `resolve_pending_predictions`, when the resolution window closes and
real price data becomes available.

The one field that makes this trustworthy is `forward_safe`:

```
forward_safe = news_item.decision_timestamp > model_knowledge_cutoff
```

This is **not** about live vs. backtest mode. It's a per-prediction check:
was the news event that triggered this prediction chronologically
impossible for the model to have training data about? If yes, the model's
reasoning is necessarily general causal knowledge (analogous past patterns),
not recall of this specific case's outcome -- exactly the reasoning
capability that's wanted. If no, the prediction is kept for inspection but
must never be counted as evidence the pipeline has skill.

`ANTHROPIC_MODEL_KNOWLEDGE_CUTOFF` (`engine/config/settings.py`) is
deliberately not something this codebase guesses -- it ships with an
obvious placeholder (`1970-01-01`) that makes `ConsequencePredictionClient`
refuse to run until you set the real cutoff date for whatever model
`ANTHROPIC_MODEL` names. Getting this field wrong silently defeats the
entire mechanism, so it fails closed instead.

## The retrieval loop

Model weights never update (a deployed LLM does not learn from being
used). What can improve without touching the model is the *context* it's
given: `load_resolved_predictions_by_topics` pulls past cases that share a
topic tag with the new headline (reusing the same keyword router as the
dumb-news/overnight-gap strategies) and feeds them into the prompt as
precedent -- "here's a similar past case and what actually happened." This
is retrieval-augmented grounding on the pipeline's own verified track
record, not the model's training corpus, so it doesn't reintroduce the
hindsight problem: an entry only enters the retrievable pool once its
outcome is actually known and resolved.

Cycle: news -> retrieve topic-matched resolved cases -> LLM analysis (with
those cases as context) -> persist one `Prediction` row per identified
impact -> [after the resolution window closes] fetch real price data,
score, mark resolved -> that resolved row becomes available as context for
the next cycle.

## Resolution is a fixed-horizon snapshot, on purpose (2026-07-21)

`resolve_prediction` compares exactly two prices: `entry_price` (the first
bar at/after `news_decision_timestamp`) and `exit_price` (the last bar
at/before `news_decision_timestamp + resolution_window_hours`, default
24h). `outcome_correct` is set once, from that single comparison, and never
revisited -- there is no mechanism, and deliberately none is planned, that
lets a prediction scored "incorrect" become "correct" later because price
kept moving. A forecast that claims "up over the next 24h" is a falsifiable
statement about *that* window; letting the goalposts move until the market
eventually agrees with it would make the accuracy metric measure patience,
not skill.

That said, a pure two-point comparison throws away everything that
happened in between -- whether the prediction was ever actually validated
by, say, an 8% adverse move mid-window before recovering. `mfe_pct` /
`mae_pct` on `Prediction` capture that path context: the best
(`mfe_pct`) and worst (`mae_pct`) the price moved relative to the
predicted direction at any point during the window, both non-negative
percentages of `entry_price`, computed from the same bars already being
fetched for entry/exit (no extra data pulled). They're diagnostic, not
scoring -- `outcome_correct` still depends only on the endpoint comparison
above.

This is also how the "wrong now, could become right later" concern
actually gets addressed -- not by extending patience on the scoring side,
but by keeping two independently-computed answers to two different
questions: *was the forecast right at the horizon it committed to*
(`outcome_correct`), and *would a real position have survived to collect
that outcome* (`mae_pct` vs. `RiskGate`'s stop-loss threshold). A
prediction can be `outcome_correct=True` with a `mae_pct` well past the
live stop-loss -- that's not a contradiction, it means the directional
call was right but a real position would have been stopped out before
ever seeing it pay off. `engine predictions-report` flags exactly this
count. See `engine.prediction.trading.close_expired_prediction_trades` for
the actual (separate, already-existing) mechanism that realizes real P&L
on this subset: it exits at the resolution window boundary or the
stop-loss, whichever comes first, independent of how the prediction itself
gets scored.

## What "skill" would actually look like

Query `Prediction` where `status = resolved AND forward_safe = true`
(`engine predictions-report`) and compare accuracy to a naive baseline
(50% for a binary up/down call, weighted by the base rate of the
predicted direction actually occurring for that symbol/period). This
pipeline has no claim to skill yet -- it has no resolved forward-safe
predictions until it's been run forward for a while. That absence is the
expected starting state, not a bug.

## Acting on predictions (`engine.prediction.trading`, 2026-07-20)

The pipeline can now do more than log a hypothetical outcome: `engine
act-on-predictions` submits a real paper order for any PENDING,
forward_safe prediction whose confidence clears
`PREDICTION_ACTION_CONFIDENCE_THRESHOLD` (default 0.6) -- "up" goes long,
"down" goes short (see the short-selling support added to
`engine.backtest.engine` the same day). `engine resolve-predictions` closes
the linked position once the resolution window ends, in addition to its
existing scoring step, if `ALPACA_API_KEY` is set.

This does **not** change what makes the forward-test log honest:
`forward_safe` still gates evidence-of-skill the same way, and every
prediction -- traded or not -- is still scored against real historical bars
by `resolve_pending_predictions`, independent of whether it was ever
traded. Trading a subset of predictions adds a second, real-money-shaped
(paper) consequence on top of the log; it doesn't touch the scoring itself.
Every order still goes through `RiskGate.evaluate()`, no exceptions, same
as every other order path in this codebase.

Confidence gating for *trading* is deliberately separate from
`forward_safe`, which is a scoring-integrity concept, not a trading-risk
one: in live operation forward_safe is essentially always true (the event
just happened, so it's always after the model's training cutoff by
construction) -- it's still checked before trading anyway, out of caution,
but confidence is the real gate here.

## Reasoning isn't limited to the tracked universe; trading still is (2026-07-20)

Earlier, the LLM's prompt hard-restricted it to naming only symbols from
`universe.yaml` ("the only symbols you may name"). That was a real
constraint on the reasoning itself -- if the actual best answer to "who's
exposed by this" was a company we don't track, the model could only ever
name the closest tracked proxy, which isn't the same thing as the real
answer. This is now decoupled: the model can name any real ticker it
judges to be the best fit; `Prediction.in_tracked_universe` records
whether that symbol happens to be one we can act on.

Every prediction is still logged and still scored by
`resolve_pending_predictions` against real historical bars, tracked or not
-- an off-universe pick is real evidence either way. Only
`in_tracked_universe = true` rows are ever eligible for a real order
(`load_actionable_predictions`), because only universe.yaml symbols are
vetted as Alpaca-tradable and risk-calibrated; that's a hallucination and
liquidity guardrail, not a reasoning limit.

`engine ticker-suggestions` surfaces every off-universe symbol with its
accumulated evidence (times named, resolved count, accuracy, most recent
rationale), sorted by resolved sample size. It never adds anything to
`universe.yaml` automatically -- growing the tracked universe is always a
human decision, made by looking at accumulated evidence and editing
`universe.yaml` directly. The report just makes that evidence visible
instead of it sitting unexamined in the `Prediction` table.

## Seeing what it's actually doing

- `engine ticker-suggestions` -- off-universe symbol suggestions and their
  track record (see above).
- `engine prediction-trades` -- history of every prediction actually acted
  on with a real order: symbol, direction, size, open/closed state, and
  outcome once resolved. Distinct from `predictions-report`, which is
  about the accuracy of the whole log; most predictions are never traded.

## Two interchangeable backends (2026-07-21)

`engine.prediction.factory.build_prediction_client` picks between
`ConsequencePredictionClient` (`ANTHROPIC_API_KEY`, metered, calls the
Messages API directly) and `ClaudeCLIPredictionClient`
(`CLAUDE_CODE_OAUTH_TOKEN`, subscription, shells out to the `claude` CLI)
based on which credential is set -- OAuth wins if both are present.
Nothing downstream (`run_prediction_for_news_item`, `predict-loop`) knows
or cares which one is active; both implement the same `analyze()`
contract.

**The OAuth/CLI backend has a known, unresolved problem as of this
writing** -- see JOURNAL.md 2026-07-21 for the full investigation. Short
version: `claude -p` intercepts ordinary, unambiguous prompts with a
clarifying question instead of following the system prompt, for reasons
that don't look content-related (reproduced on both a mundane and an
evocative headline). This backend fails safely (logged, per-cycle,
non-crashing) but likely produces few or no real predictions until fixed.
If accuracy tracking in `predictions-report` looks suspiciously empty
despite `predict-loop` showing as running, check which backend is active
before assuming the pipeline itself is broken.

## What this pipeline is not

- Not a general trading strategy wired into `engine papertrade`'s main
  loop or the backtester's `Strategy` protocol -- `predict-news`,
  `act-on-predictions`, and `resolve-predictions` are their own standalone
  commands, not driven by the bar-by-bar event loop every other strategy
  uses. `engine predict-loop` runs all three automatically on a schedule
  (default hourly); the individual commands remain available for manual
  runs. That's a deliberate difference from the other strategies: this
  pipeline can't be backtested the normal way, so it was never worth
  forcing it into the same interface as strategies that can be.
- Not a replacement for `engine.features.sentiment` or `engine.data.router`
  -- those still drive the actual dumb-news, Overnight-Gap, and
  price-action strategies. This is a separate, slower, LLM-cost-bearing
  track running in parallel.
- Not free. Each `engine predict-news` call is a paid Claude API call per
  headline analyzed; `--limit` caps spend per run. Acting on a prediction
  adds real (paper) order submission on top of that -- still no cost
  beyond the LLM call itself, since Alpaca's paper trading is free.
