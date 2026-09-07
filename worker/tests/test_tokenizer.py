"""The token estimator is an approximation, but it must be a *stable* approximation.

Chunk boundaries are decided by these numbers, and Contract 5 §4 requires the same bytes to
produce the same boundaries forever. So what is tested here is determinism, monotonicity and
rough calibration — never an exact BPE count, which this deliberately does not compute.
"""

from __future__ import annotations

import pytest

from queryll_worker.chunking.tokenizer import count_tokens, truncate_to_tokens

PROSE = (
    "Grounded question answering systems are judged less by the fluency of their prose "
    "than by whether the passage behind each claim genuinely supports it."
)


def test_empty_text_is_zero_tokens() -> None:
    assert count_tokens("") == 0
    assert count_tokens("   \n\t ") == 0


def test_counting_is_deterministic() -> None:
    assert count_tokens(PROSE) == count_tokens(PROSE)


def test_counting_is_monotonic_in_text_length() -> None:
    assert count_tokens(PROSE) < count_tokens(PROSE + " " + PROSE)


@pytest.mark.parametrize(
    "text,low,high",
    [
        (PROSE, 25, 45),
        ("a " * 100, 90, 110),
        ("supercalifragilisticexpialidocious", 6, 10),
    ],
)
def test_estimate_is_in_the_right_neighbourhood(text: str, low: int, high: int) -> None:
    """Calibration, not exactness: ~4 characters per token on Latin-script text."""
    assert low <= count_tokens(text) <= high


def test_punctuation_costs_a_token_each() -> None:
    assert count_tokens("...") == 3
    assert count_tokens("hi!") == count_tokens("hi") + 1


def test_cjk_is_roughly_one_token_per_character() -> None:
    assert count_tokens("日本語のテキスト") == 8


def test_truncation_respects_the_budget_and_a_word_boundary() -> None:
    truncated = truncate_to_tokens(PROSE, 10)
    assert count_tokens(truncated) <= 10
    assert PROSE.startswith(truncated)
    assert not truncated.endswith(" ")
    # It stopped mid-sentence, but not mid-word.
    assert truncated.split()[-1] in PROSE.split()


def test_truncation_leaves_short_text_untouched() -> None:
    assert truncate_to_tokens(PROSE, 10_000) == PROSE


def test_truncation_to_nothing() -> None:
    assert truncate_to_tokens(PROSE, 0) == ""
