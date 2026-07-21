from datetime import datetime, timezone

from engine.data.news import RSS_ROTATION_ORDER, active_rss_source

ANCHOR = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)


def test_active_rss_source_is_deterministic():
    now = datetime(2026, 7, 21, 3, 30, tzinfo=timezone.utc)
    first = active_rss_source(ANCHOR, now, rotation_hours=1.0)
    second = active_rss_source(ANCHOR, now, rotation_hours=1.0)
    assert first == second


def test_active_rss_source_advances_exactly_one_slot_per_rotation_hours():
    assert active_rss_source(ANCHOR, ANCHOR, rotation_hours=1.0) == RSS_ROTATION_ORDER[0]
    one_hour_later = datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc)
    assert active_rss_source(ANCHOR, one_hour_later, rotation_hours=1.0) == RSS_ROTATION_ORDER[1]
    two_hours_later = datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc)
    assert active_rss_source(ANCHOR, two_hours_later, rotation_hours=1.0) == RSS_ROTATION_ORDER[2]


def test_active_rss_source_wraps_after_the_last_source():
    three_hours_later = datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc)
    assert active_rss_source(ANCHOR, three_hours_later, rotation_hours=1.0) == RSS_ROTATION_ORDER[0]


def test_active_rss_source_survives_a_simulated_restart():
    # The whole justification for computing rotation from wall-clock time
    # instead of a persisted "current index" counter: identical inputs must
    # give identical answers regardless of how many times the process
    # restarted in between, since there's no stored state to lose or
    # double-advance.
    now = datetime(2026, 7, 21, 5, 45, tzinfo=timezone.utc)
    before_restart = active_rss_source(ANCHOR, now, rotation_hours=1.0)
    after_restart = active_rss_source(ANCHOR, now, rotation_hours=1.0)
    assert before_restart == after_restart


def test_active_rss_source_handles_naive_datetimes_from_sqlite_roundtrip():
    naive_anchor = ANCHOR.replace(tzinfo=None)
    naive_now = datetime(2026, 7, 21, 1, 0)
    assert active_rss_source(naive_anchor, naive_now, rotation_hours=1.0) == RSS_ROTATION_ORDER[1]


def test_active_rss_source_rejects_non_positive_rotation_hours_without_raising():
    result = active_rss_source(ANCHOR, ANCHOR, rotation_hours=0)
    assert result in RSS_ROTATION_ORDER
    result = active_rss_source(ANCHOR, ANCHOR, rotation_hours=-5)
    assert result in RSS_ROTATION_ORDER


def test_active_rss_source_respects_custom_rotation_hours():
    now = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)
    # 6 hours elapsed, 2-hour rotation window -> slot 3 -> wraps to index 0
    assert active_rss_source(ANCHOR, now, rotation_hours=2.0) == RSS_ROTATION_ORDER[0]
