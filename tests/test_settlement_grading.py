"""Tests for settlement outcome grading (the Phase 2 scoreboard fix).

Regression guard for the yes/no vs up/down bug: weather signals store their
direction as "yes"/"no", but grading previously compared that against an
"up"/"down" actual outcome, so every weather prediction graded as wrong.
"""
from backend.core.settlement import grade_signal_outcome


# (direction, settlement_value, expected_actual_outcome, expected_correct)
CASES = [
    # Weather vocabulary (yes/no) — the previously broken path
    ("yes", 1.0, "yes", True),    # bet YES, YES won  -> correct
    ("yes", 0.0, "no", False),    # bet YES, NO won   -> wrong
    ("no", 0.0, "no", True),      # bet NO, NO won    -> correct
    ("no", 1.0, "yes", False),    # bet NO, YES won   -> wrong
    # Legacy BTC vocabulary (up/down) — must still grade correctly
    ("up", 1.0, "up", True),
    ("up", 0.0, "down", False),
    ("down", 0.0, "down", True),
    ("down", 1.0, "up", False),
]


def test_grade_signal_outcome_all_cases():
    for direction, settlement_value, expected_outcome, expected_correct in CASES:
        actual_outcome, correct = grade_signal_outcome(direction, settlement_value)
        assert actual_outcome == expected_outcome, (
            f"{direction}@{settlement_value}: outcome {actual_outcome} != {expected_outcome}"
        )
        assert correct is expected_correct, (
            f"{direction}@{settlement_value}: correct {correct} != {expected_correct}"
        )


def test_weather_yes_is_not_always_wrong():
    """The specific regression: a winning weather YES bet must grade correct."""
    _, correct = grade_signal_outcome("yes", 1.0)
    assert correct is True


def test_actual_outcome_uses_signal_vocabulary():
    """Recorded actual_outcome stays in the signal's own vocabulary."""
    assert grade_signal_outcome("yes", 0.0)[0] == "no"     # weather -> yes/no
    assert grade_signal_outcome("down", 1.0)[0] == "up"    # legacy  -> up/down


if __name__ == "__main__":
    test_grade_signal_outcome_all_cases()
    test_weather_yes_is_not_always_wrong()
    test_actual_outcome_uses_signal_vocabulary()
    print(f"All {len(CASES)} grading cases + regression checks passed.")
