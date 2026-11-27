"""Per-request provider timeout: one rule for every model, scaled by the reply's output cap."""

from __future__ import annotations

from claude_duel.adapters.providers.http import FULL_TIMEOUT_TOKENS, MIN_REQUEST_TIMEOUT_S, request_timeout
from claude_duel.domain.modes import preset


def test_scaled_floored_and_capped() -> None:
    assert request_timeout(300.0, FULL_TIMEOUT_TOKENS) == 300.0
    assert request_timeout(300.0, FULL_TIMEOUT_TOKENS // 2) == 150.0
    assert request_timeout(300.0, 100) == MIN_REQUEST_TIMEOUT_S
    assert request_timeout(300.0, 10 * FULL_TIMEOUT_TOKENS) == 300.0
    assert request_timeout(30.0, 100) == 30.0  # never above the configured timeout


def test_ladder_preset_stakes_leave_room_for_reasoning() -> None:
    ledger = preset("ladder").ledger
    assert (ledger.min_alloc, ledger.attack_tokens) == (6_000, 24_000)


def test_full_timeout_tokens_is_the_ladder_attack_budget() -> None:
    # one source: a stake change must be a deliberate timeout change too
    assert preset("ladder").ledger.attack_tokens == FULL_TIMEOUT_TOKENS
