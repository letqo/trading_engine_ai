# Bias review

SPEC.md's anti-self-deception protocol requires every new strategy to come
with written answers to four questions before it's allowed near the
validation set. Template below; two filled-in reviews follow for the
strategies that already exist in this repo.

## Template

**Strategy:** `<name>` (`src/engine/strategy/<file>.py`)
**Date / experiment ID:** `<date>` / `<journal experiment run id, if backtested>`

1. **Where could look-ahead bias enter?**
2. **Survivorship bias?**
3. **Is the news timestamp the *publication* time or the *ingestion* time?**
4. **Anything else that could make these results look better than a live run?**

---

## DumbNewsStrategy (`dumb_news.py`) — Phase 4 control group

1. **Look-ahead:** None identified. Entry only fires from `on_news`, which
   the backtester calls with `NewsItem.decision_timestamp =
   max(published_at, ingested_at)` (see `engine.domain.NewsItem`), never
   `published_at` alone. The resulting order fills at the *next* bar's open,
   never the bar concurrent with the news. Risk: if `ingested_at` is
   backfilled incorrectly during data ingestion (e.g., set equal to
   `published_at` instead of the real fetch time), the timestamp guard is
   silently defeated. `engine/data/news.py` sets `ingested_at =
   datetime.now(timezone.utc)` at fetch time specifically to avoid this, but
   a *replayed/backfilled* dataset assembled after the fact must not
   fabricate `ingested_at` from `published_at` — it should carry whatever
   the pipeline's real historical ingestion log recorded, or the backtest is
   quietly optimistic.
2. **Survivorship:** The universe (`universe.yaml`) is fixed and was chosen
   for today's mega-caps/ETFs, not reconstructed from what was liquid/listed
   at each historical point in time. A backtest run over several years
   implicitly assumes all Tier 1/2 names existed and were tradable
   throughout — not true for some (e.g. COIN IPO'd 2021). Any run spanning
   before a name's listing date will simply see no bars for it, which is
   fail-safe (no phantom trades) but the equity curve for the whole universe
   window should not be read as "what an investor lived through" before
   each symbol existed.
3. **Publication vs ingestion:** Ingestion (`decision_timestamp`), by
   construction — see (1).
4. **Other inflation risks:** Sentiment is scored with VADER against the
   headline only, not full article text; a backtest and a live run score
   the identical headline the identical way, so this isn't a backtest-vs-live
   gap, but it does mean the "signal" is shallow and the strategy is
   expected to lose after costs, per SPEC.md. That expectation is the point
   — this strategy exists to be beaten.

## OvernightGapStrategy (`overnight_gap.py`) — Phase 5 first candidate

1. **Look-ahead:** Same `decision_timestamp` guard as above. Additional
   risk specific to this strategy: the "outside US market hours" and "US
   market open" checks are UTC hour-of-day comparisons
   (`US_MARKET_OPEN_UTC_HOUR = 14`) that do not account for DST. Around DST
   transitions this can shift the effective decision window by an hour in
   either direction versus what a live system (using actual exchange
   calendar hours) would do. This does not leak future information, but it
   does mean backtest entries near DST boundaries fire at a slightly
   different wall-clock offset from the real open than they would live —
   flag, don't trust, any edge that appears concentrated around DST
   transition dates.
2. **Survivorship:** Same universe caveat as above; less relevant here since
   this strategy only trades the long-lived Tier 2 macro/regional ETFs
   (EWJ, FXI, VGK, EEM, SMH, XLE, XLF, GLD, USO, TLT), all listed well
   before any plausible backtest window.
3. **Publication vs ingestion:** Ingestion, via `decision_timestamp` — see
   (1) above. This matters more here than for the dumb strategy: an RSS
   poller that only checks every N minutes will systematically report BoJ/
   ECB overnight headlines later than their true publication time, which
   *reduces* the apparent edge in backtest relative to a lower-latency
   pipeline. That's the conservative direction to be wrong in, which is
   deliberate (SPEC.md: "start pessimistic"), but it means a live poll
   interval slower than what's simulated will underperform the backtest,
   not the reverse — check the actual RSS poll cadence configured for live
   trading against whatever cadence (if any) was assumed when a snapshot
   was built.
4. **Other inflation risks:** The strategy only routes Tier 2 (macro basket)
   symbols and only acts on positive sentiment (v1 is long-only, so negative
   overnight sentiment is simply dropped rather than expressed as a hedge).
   This means the backtest never sees how the strategy would have performed
   on the negative-news half of overnight events — it is not "wrong," but it
   is an intentionally incomplete strategy, and its win rate should not be
   read as representative of "how the engine would trade all overnight
   macro news," only "how it trades the bullish half of it."
