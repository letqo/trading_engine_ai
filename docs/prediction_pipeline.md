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

## What this pipeline is not

- Not a general trading strategy wired into `engine papertrade`'s main
  loop or the backtester's `Strategy` protocol -- `act-on-predictions` and
  `resolve-predictions` are their own standalone commands, run on whatever
  cadence you choose (e.g. a scheduled job), not driven by the bar-by-bar
  event loop every other strategy uses. That's a deliberate difference:
  this pipeline can't be backtested the normal way, so it was never worth
  forcing it into the same interface as strategies that can be.
- Not a replacement for `engine.features.sentiment` or `engine.data.router`
  -- those still drive the actual dumb-news, Overnight-Gap, and
  price-action strategies. This is a separate, slower, LLM-cost-bearing
  track running in parallel.
- Not free. Each `engine predict-news` call is a paid Claude API call per
  headline analyzed; `--limit` caps spend per run. Acting on a prediction
  adds real (paper) order submission on top of that -- still no cost
  beyond the LLM call itself, since Alpaca's paper trading is free.
