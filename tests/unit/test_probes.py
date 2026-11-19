"""Junk-input probes must skip the answer and keep its shape."""

from claude_duel.domain.probes import junk_inputs


def test_junk_inputs_skip_the_answer() -> None:
    assert "0000003" not in junk_inputs("0000003")
    assert {"a", "0", "AAAAAAA", "a000003"} <= set(junk_inputs("0000003"))


def test_junk_inputs_keep_the_answer_shape() -> None:
    """Last and middle char are swapped within their class (digit, letter, other)."""
    assert {"0000004", "0001003"} <= set(junk_inputs("0000003"))
    assert {"abD", "acC"} <= set(junk_inputs("abC"))
    assert "ab#" in junk_inputs("ab!")


def test_junk_inputs_include_first_char_and_a_seeded_index() -> None:
    assert "1000003" in junk_inputs("0000003")
    once = junk_inputs("abcdefg", seed="match-seed")
    assert once == junk_inputs("abcdefg", seed="match-seed")
    assert "bbcdefg" in once  # index 0, a -> b
    other = junk_inputs("abcdefg", seed="other-seed")
    assert "bbcdefg" in other
    # a different seed may add a different extra index; both stay deterministic
    assert once == junk_inputs("abcdefg", seed="match-seed")
    assert other == junk_inputs("abcdefg", seed="other-seed")
