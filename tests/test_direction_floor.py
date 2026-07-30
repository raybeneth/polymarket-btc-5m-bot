"""Hard floor on the direction filter score (DIRECTION_MED_MIN_SCORE).

Regression cover for the 2026-07-30 raise 10 -> 25. Live positions 116-137
(order 0x6bc8ffdf -> 0xdf54e8214e) showed odds+timing alone clearing
MIN_COMBINED_SCORE while direction sat near zero: #136 (dir=10.9, timing=99.2)
lost -12.30 and #127 (dir=11.5, timing=89.2) lost -11.72. Both cleared the old
floor of 10.
"""
import config
from config import (
    DIRECTION_MED_MIN_SCORE,
    MIN_COMBINED_SCORE,
    SCORE_WEIGHTS,
)

# (id, direction, timing, odds, combined_score, result) from the live positions
# table for the batch under review. Kept literal so the test documents the
# evidence rather than depending on DB access.
BATCH = [
    (116, 37.7, 57.4, 67.5, 51.06, "WIN"),
    (117, 19.0, 60.1, 85.0, 47.83, "WIN"),
    (118, 34.9, 53.6, 87.5, 53.66, "WIN"),
    (119, 47.9, 14.5, 85.0, 47.16, "WIN"),
    (120, 34.3, 38.5, 87.5, 48.86, "WIN"),
    (121, 53.0, 90.9, 67.5, 68.00, "WIN"),
    (122, 53.3, 41.5, 100.0, 61.44, "WIN"),
    (123, 51.3, 5.9, 97.5, 49.23, "WIN"),
    (124, 33.0, 54.0, 82.5, 51.68, "WIN"),
    (125, 34.5, 80.5, 72.5, 57.80, "WIN"),
    (126, 42.9, 82.6, 65.0, 60.33, "WIN"),
    (127, 11.5, 89.2, 85.0, 53.19, "LOSS"),
    (128, 16.5, 97.7, 95.0, 60.49, "LOSS"),
    (129, 54.6, 17.9, 67.5, 46.82, "WIN"),
    (130, 56.1, 38.1, 65.0, 52.93, "WIN"),
    (131, 46.6, 11.5, 82.5, 45.05, "LOSS"),
    (132, 36.7, 65.2, 85.0, 57.33, "LOSS"),
    (133, 76.8, 5.1, 80.0, 56.09, "LOSS"),
    (134, 13.3, 90.8, 77.5, 52.60, "WIN"),
    (135, 19.8, 73.7, 70.0, 48.52, "LOSS"),
    (136, 10.9, 99.2, 75.0, 53.42, "LOSS"),
    (137, 45.8, 40.7, 75.0, 51.57, "WIN"),
]


def test_floor_is_at_least_25():
    """Guards the raise from 10. Anything lower re-admits #127/#136."""
    assert DIRECTION_MED_MIN_SCORE >= 25.0


def test_zero_direction_can_clear_combined_without_the_floor():
    """The reason the floor has to exist: odds+timing alone outvote direction.

    With odds at 25% and timing at 30%, a direction score of 0 still reaches
    above MIN_COMBINED_SCORE, so the combined-score gate cannot stop a trade
    that has no directional edge.
    """
    combined = (
        SCORE_WEIGHTS["odds"] * 100.0
        + SCORE_WEIGHTS["timing"] * 100.0
        + SCORE_WEIGHTS["direction"] * 0.0
    )
    assert combined >= MIN_COMBINED_SCORE


def test_known_zero_edge_losses_are_rejected():
    """#127 and #136 are the two trades that motivated the raise."""
    for pid in (127, 136):
        dir_score = next(d for i, d, *_ in BATCH if i == pid)
        assert dir_score < DIRECTION_MED_MIN_SCORE, f"#{pid} should be skipped"


def test_floor_improves_the_batch():
    """Applying the floor to the live batch must raise both win rate and total.

    Uses win/loss counts only — the point is that the gate is not merely
    trimming trades at random.
    """
    kept = [row for row in BATCH if row[1] >= DIRECTION_MED_MIN_SCORE]
    assert len(kept) < len(BATCH), "floor should exclude something"

    def win_rate(rows):
        return sum(1 for r in rows if r[5] == "WIN") / len(rows)

    assert win_rate(kept) > win_rate(BATCH)
    # 13W/3L kept vs 15W/7L overall
    assert sum(1 for r in kept if r[5] == "LOSS") == 3


def test_high_tier_floor_stays_above_med():
    assert config.DIRECTION_HIGH_MIN_SCORE >= DIRECTION_MED_MIN_SCORE
