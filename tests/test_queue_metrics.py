"""RateTracker reports None for an uncomputable rate and 0.0 only for a computed zero."""

from dashboard.queue_metrics import QueueSample, RateTracker


KEY = ("/", "groovemap-q")


def _sample(published: float = 0.0, acked: float = 0.0) -> dict[tuple[str, str], QueueSample]:
    return {KEY: QueueSample(published_total=published, acked_total=acked)}


class TestRateTracker:
    def test_first_sample_has_no_rate(self) -> None:
        assert RateTracker().update(_sample(10, 5), 0.0) == {KEY: (None, None)}

    def test_computed_rate_follows_the_delta(self) -> None:
        tracker = RateTracker()
        tracker.update(_sample(10, 5), 0.0)
        assert tracker.update(_sample(30, 15), 2.0) == {KEY: (10.0, 5.0)}

    def test_unchanged_counters_are_a_computed_zero(self) -> None:
        tracker = RateTracker()
        tracker.update(_sample(10, 5), 0.0)
        assert tracker.update(_sample(10, 5), 2.0) == {KEY: (0.0, 0.0)}

    def test_counter_reset_has_no_rate_then_recovers(self) -> None:
        tracker = RateTracker()
        tracker.update(_sample(500, 400), 0.0)
        assert tracker.update(_sample(5, 2), 2.0) == {KEY: (None, None)}
        assert tracker.update(_sample(25, 12), 4.0) == {KEY: (10.0, 5.0)}

    def test_reset_of_one_counter_leaves_the_other_computed(self) -> None:
        tracker = RateTracker()
        tracker.update(_sample(10, 400), 0.0)
        assert tracker.update(_sample(30, 2), 2.0) == {KEY: (10.0, None)}

    def test_missed_poll_has_no_baseline(self) -> None:
        tracker = RateTracker()
        tracker.update(_sample(100), 0.0)
        assert tracker.update({}, 2.0) == {}
        assert tracker.update(_sample(300), 4.0) == {KEY: (None, None)}

    def test_non_increasing_time_has_no_rate(self) -> None:
        tracker = RateTracker()
        tracker.update(_sample(10, 5), 5.0)
        assert tracker.update(_sample(20, 10), 5.0) == {KEY: (None, None)}

    def test_absent_counters_on_a_known_queue_count_as_zero(self) -> None:
        tracker = RateTracker()
        tracker.update(_sample(), 0.0)  # counter series not emitted yet parse as zero totals
        assert tracker.update(_sample(10, 4), 2.0) == {KEY: (5.0, 2.0)}
