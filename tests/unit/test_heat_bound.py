"""Host work bound for novelty heat: capped LCS, pinned replay, legacy logs that no longer fit."""

from __future__ import annotations

import ast
import random
import time
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

import claude_duel
from claude_duel.adapters.providers.fake import TEMPLATES
from claude_duel.app.agents import build_prompt
from claude_duel.app.container import build_store
from claude_duel.app.replay import replay
from claude_duel.app.session import open_match
from claude_duel.config import Settings
from claude_duel.domain import events as ev
from claude_duel.domain import memory, policy
from claude_duel.domain.errors import EventLogCorruptError, HeatBoundError
from claude_duel.domain.heat import HeatScorer, heat_label, history_refs
from claude_duel.domain.heat_bound import (
    MAX_CHALLENGES,
    WORK_BUDGET,
    Hot,
    admit,
    admit_bytes,
    heavy,
    overlap,
    pin_heat_bound,
    slots,
    work,
)
from claude_duel.domain.heat_bound import lcs as bit_lcs
from claude_duel.domain.heat_tokens import PARSE_BYTES, REF_CAP, TOKEN_CAP, profile, tokens
from claude_duel.domain.models import (
    AgentSpec,
    Attack,
    Challenge,
    Event,
    LedgerRule,
    MatchState,
    Rules,
    SandboxLimits,
    Validation,
)
from claude_duel.domain.modes import preset
from claude_duel.domain.scoring import round_heat, score_round
from claude_duel.presentation.arena import _heat_view
from claude_duel.presentation.card import CardData, card_svg
from tests.helpers import similarity
from tests.unit.test_domain import spec
from tests.unit.test_heat import LOOP, OTHER, RENAMED, _match


def _dense(n: int, salt: int) -> str:
    head = f"v = {salt}\n"
    line = "v = v + 1\n"
    return head + line * max(1, (n - len(head)) // len(line))


def _ch(cid: str, round_no: int, author: str, source: str) -> Challenge:
    return Challenge(id=cid, round=round_no, author=author, spec=spec(source, difficulty=2))


def _valid(chs: list[Challenge]) -> dict[str, Validation]:
    return {c.id: Validation(challenge_id=c.id, valid=True, reason="ok") for c in chs}


def _held(cid: str) -> Attack:
    return Attack(challenge_id=cid, attacker="b", solved=False, attempts=1, tokens_used=0, reason="held", stake=4000)


def _legacy_rules() -> Rules:
    base = preset("ledger")
    led = LedgerRule(keystone=True, terrain="seeded", heat_window=2)
    return base.model_copy(update={"rules_version": 1, "ledger": led})


def _replay(
    rules: Rules,
    chs: list[Challenge],
    refs: Mapping[str, list[str]],
    delta: dict[str, int],
    hot: dict[str, int],
    *,
    drop_caps: bool,
) -> tuple[MatchState, list[Event]]:
    dumped = rules.dump()
    if drop_caps:
        dumped["ledger"].pop("heat_token_cap", None)
        dumped["ledger"].pop("heat_ref_cap", None)
    data: dict[str, object] = {
        "seed": "s",
        "agents": [a.model_dump(mode="json") for a in (AgentSpec(name="a"), AgentSpec(name="b"))],
        "rules": dumped,
        "limits": SandboxLimits().model_dump(mode="json"),
    }
    if refs:
        data["heat_refs"] = dict(refs)
    events = [Event(seq=0, type=ev.MATCH_CREATED, trace_id="t", data=data)]
    seq = 1
    vals = _valid(chs)
    for ch in chs:
        events.append(
            Event(seq=seq, type=ev.CHALLENGE_BUILT, trace_id="t", data={"challenge": ch.model_dump(mode="json")})
        )
        seq += 1
        events.append(
            Event(
                seq=seq,
                type=ev.CHALLENGE_VALIDATED,
                trace_id="t",
                data={"validation": vals[ch.id].model_dump(mode="json")},
            )
        )
        seq += 1
    round_no = max(ch.round for ch in chs)
    for ch in chs:
        if ch.round != round_no:
            continue
        atk = _held(ch.id)
        events.append(
            Event(
                seq=seq,
                type=ev.CHALLENGE_ATTACKED,
                trace_id="t",
                data={"key": f"{ch.id}:b", "attack": atk.model_dump(mode="json")},
            )
        )
        seq += 1
    events.append(
        Event(seq=seq, type=ev.ROUND_SCORED, trace_id="t", data={"round": round_no, "delta": delta, "heat": hot})
    )
    return ev.fold("bound", events), events


def test_ceiling_is_the_documented_contract() -> None:
    assert (TOKEN_CAP, REF_CAP, PARSE_BYTES, MAX_CHALLENGES) == (256, 48, 65_536, 20)
    assert WORK_BUDGET == 1_000_000_000
    assert work(32_768, 32_768) == 32_768 * 513  # a 32k x 32k pair: under 2% of a call


def test_short_sources_keep_full_similarity() -> None:
    assert similarity(LOOP, RENAMED) == 100
    assert similarity(LOOP, RENAMED, token_cap=TOKEN_CAP) == 100
    assert len(tokens(LOOP)) < TOKEN_CAP


def test_dense_prefix_matches_a_copy_and_legacy_does_not_run_lcs() -> None:
    src = _dense(6144, 1)
    assert len(tokens(src)) == TOKEN_CAP
    sc = HeatScorer()
    started = time.perf_counter()
    with pytest.raises(HeatBoundError, match="work bound"):
        similarity(src, _dense(6144, 2), scorer=sc)
    assert sc.work == 0
    assert similarity(src, _dense(6144, 2), token_cap=TOKEN_CAP, scorer=sc) == 100
    assert sc.work == 0  # one prefix holds the other: no LCS
    assert time.perf_counter() - started < 2


def test_deep_nesting_does_not_escape_the_host() -> None:
    prefix = "def f(x):\n    return "
    src = prefix + "+".join(["x"] * 20_000) + "\n"
    started = time.perf_counter()
    assert tokens(src) == ()
    assert similarity(src, src, token_cap=TOKEN_CAP) == 0
    assert time.perf_counter() - started < 2


def test_oversize_source_is_not_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = ast.parse

    def counting(source: str, *args: object, **kwargs: object) -> ast.AST:
        seen.append(source)
        return real(source, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("claude_duel.domain.heat_tokens.ast.parse", counting)
    src = "v = 1\n" + "x" * (PARSE_BYTES + 10)
    assert tokens(src) == ()
    with pytest.raises(HeatBoundError):
        similarity(src, LOOP)
    assert all(len(item) <= PARSE_BYTES for item in seen)


def test_pinned_window_wins_and_dropped_history_is_not_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    window = _ch("r1a", 1, "a", LOOP)
    current = _ch("r2a", 2, "a", RENAMED)
    old = [f"z = {i}\n" for i in range(30)]
    rule = LedgerRule(heat_window=2, heat_token_cap=TOKEN_CAP, heat_ref_cap=2)
    seen: list[str] = []
    real = ast.parse

    def counting(source: str, *args: object, **kwargs: object) -> ast.AST:
        seen.append(source)
        return real(source, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("claude_duel.domain.heat_tokens.ast.parse", counting)
    got = HeatScorer().heat([window, current], _valid([window, current]), 2, rule, {"a": [*old, "v = 1\n"]})
    assert got["r2a"] == similarity(RENAMED, LOOP, token_cap=TOKEN_CAP)
    assert all(not item.startswith("z = ") for item in seen)


def test_too_many_challenges_or_legacy_refs_raise_before_lcs() -> None:
    sc = HeatScorer()
    many = [_ch(f"c{i:02d}", 1, "a", LOOP) for i in range(MAX_CHALLENGES + 1)]
    pinned = LedgerRule(heat_window=1, heat_token_cap=8, heat_ref_cap=1)
    with pytest.raises(HeatBoundError):
        sc.heat(many, _valid(many), 1, pinned)
    one = [_ch("r1a", 1, "a", LOOP)]
    with pytest.raises(HeatBoundError):
        sc.heat(one, _valid(one), 1, LedgerRule(heat_window=1), {"a": [LOOP] * (REF_CAP + 1)})
    assert sc.work == 0


def test_pin_fills_full_pins_and_drops_the_token_cap() -> None:
    assert pin_heat_bound(preset("classic")) is preset("classic")
    assert pin_heat_bound(preset("ledger")) is preset("ledger")
    bare = preset("ledger").model_copy(update={"rules_version": 1, "ledger": LedgerRule(heat_window=2)})
    pinned = pin_heat_bound(bare)
    assert pinned.ledger is not None
    assert (
        pinned.ledger.heat_token_cap,
        pinned.ledger.heat_ref_cap,
        pinned.ledger.heat_compare,
        pinned.ledger.heat_metric,
        pinned.ledger.heat_history_matches,
    ) == (None, REF_CAP, "full", "lcs-v1", 3)
    prefix = LedgerRule(heat_window=2, heat_token_cap=32, heat_ref_cap=3)
    written = pin_heat_bound(bare.model_copy(update={"ledger": prefix})).ledger
    assert written is not None
    assert (written.heat_token_cap, written.heat_ref_cap, written.heat_compare, written.heat_metric) == (
        None,
        3,
        "full",
        "lcs-v1",
    )


@pytest.mark.parametrize(
    "pins",
    [
        {"heat_token_cap": 32},
        {"heat_ref_cap": 3},
        {"heat_compare": "full"},
        {"heat_token_cap": 10**9, "heat_ref_cap": 10**9},
        {"heat_token_cap": 0, "heat_ref_cap": 1},
        {"heat_token_cap": True, "heat_ref_cap": True},
        {"heat_token_cap": "256", "heat_ref_cap": "48"},
        {"heat_token_cap": 256.0, "heat_ref_cap": 48.0},
        {"heat_token_cap": 256, "heat_ref_cap": 48, "heat_compare": ""},
    ],
)
def test_a_partial_mistyped_or_out_of_range_pin_is_invalid(pins: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LedgerRule.model_validate({"heat_window": 1, **pins})


def test_pinned_oversize_source_scores_as_empty() -> None:
    src = "v = 1\n" + "y" * (PARSE_BYTES + 5)
    assert similarity(src, LOOP, token_cap=TOKEN_CAP) == 0


def test_an_overflowing_window_keeps_the_newest_rounds() -> None:
    older = [_ch("r1a", 1, "a", LOOP), _ch("r2a", 2, "a", "v = 1\n"), _ch("r3a", 3, "a", RENAMED)]
    rule = LedgerRule(heat_window=3, heat_token_cap=TOKEN_CAP, heat_ref_cap=1)
    # Cap 1 keeps round 2 only, so the copied LOOP in round 1 and in history does not heat.
    cold = HeatScorer().heat(older, _valid(older), 3, rule, {"a": [LOOP]})
    assert "r3a" not in cold
    newer = [_ch("r1a", 1, "a", "v = 1\n"), _ch("r2a", 2, "a", LOOP), _ch("r3a", 3, "a", RENAMED)]
    assert HeatScorer().heat(newer, _valid(newer), 3, rule, {"a": ["v = 2\n"]})["r3a"] == 100


def test_unpinned_prompt_does_not_claim_the_cap() -> None:
    rules = _legacy_rules()
    assert "AST tokens" not in build_prompt("a", 1, 0, rules, None)


def test_history_refs_keep_the_newest_sources() -> None:
    past = []
    for i in range(3):
        match = _match(f"p{i}", "x", seq=i)
        match.challenges = {str(j): _ch(str(j), 1, "a", f"v = {i}{j}\n") for j in range(4)}
        match.validations = _valid(list(match.challenges.values()))
        past.append(match)
    refs = history_refs(past, [AgentSpec(name="me", model="x")], source_limit=5)
    assert refs["me"][-1] == "v = 23\n"
    assert len(refs["me"]) == 5


def test_open_match_pins_caps_and_resume_keeps_a_legacy_rule(tmp_path) -> None:  # type: ignore[no-untyped-def]
    legacy = _legacy_rules()
    settings = Settings(
        store_dir=tmp_path,
        rules=legacy,
        agents=(AgentSpec(name="a"), AgentSpec(name="b")),
    )
    store = build_store(settings)
    created = open_match(store, settings, "new")
    assert created.rules.ledger is not None
    assert created.rules.rules_version == 2
    assert (
        created.rules.ledger.heat_token_cap,
        created.rules.ledger.heat_ref_cap,
        created.rules.ledger.heat_compare,
    ) == (
        None,
        REF_CAP,
        "full",
    )
    assert (created.rules.ledger.heat_metric, created.rules.ledger.heat_history_matches) == ("lcs-v1", 3)
    assert open_match(store, settings, "new").rules == created.rules
    old = MatchState(
        match_id="old",
        secret="ab" * 16,
        agents=(AgentSpec(name="a"), AgentSpec(name="b")),
        rules=legacy,
        limits=SandboxLimits(),
    )
    store.save(old)
    loaded = open_match(store, settings, "old")
    assert loaded.rules.ledger is not None
    assert loaded.rules.ledger.heat_token_cap is None


def test_pre_cap_log_of_short_sources_replays_exactly() -> None:
    rules = _legacy_rules()
    chs = [_ch("r1a", 1, "a", LOOP), _ch("r2a", 2, "a", LOOP)]
    vals, atks = _valid(chs), {"r2a": _held("r2a")}
    delta = score_round(2, ["a", "b"], chs, vals, atks, rules)
    hot = round_heat(chs, vals, atks, 2, rules)
    folded, events = _replay(rules, chs, {}, delta, hot, drop_caps=True)
    report = replay(folded, events)
    assert report.consistent, report.mismatches
    assert folded.rules.ledger is not None
    assert folded.rules.ledger.heat_token_cap is None


def test_pinned_dense_heat_replays_and_a_forged_line_is_tampered() -> None:
    rules = preset("ledger")
    src = _dense(6144, 7)
    chs = [_ch("r1a", 1, "a", src)]
    refs = {"a": [src]}
    vals, atks = _valid(chs), {"r1a": _held("r1a")}
    delta = score_round(1, ["a", "b"], chs, vals, atks, rules, heat_refs=refs)
    hot = round_heat(chs, vals, atks, 1, rules, refs)
    assert hot["r1a"] > 0
    folded, events = _replay(rules, chs, refs, delta, hot, drop_caps=False)
    report = replay(folded, events)
    assert report.consistent, report.mismatches
    forged = [
        e.model_copy(update={"data": {**e.data, "heat": {k: v + 1 for k, v in e.data["heat"].items()}}})
        if e.type == ev.ROUND_SCORED
        else e
        for e in events
    ]
    mismatches = replay(folded, forged).mismatches
    assert any(item.endswith("heat tampered") for item in mismatches)


def test_legacy_dense_log_is_legacy_unreplayable_not_rescored() -> None:
    rules = _legacy_rules()
    src = _dense(6144, 3)
    chs = [_ch("r1a", 1, "a", src), _ch("r1b", 1, "b", src)]
    started = time.perf_counter()
    folded, events = _replay(rules, chs, {"a": [src], "b": [src]}, {"a": 1, "b": 1}, {"r1a": 1}, drop_caps=True)
    report = replay(folded, events)
    assert time.perf_counter() - started < 2
    assert report.consistent is False
    assert any("heat legacy-unreplayable" in item for item in report.mismatches)
    assert any("score not checked" in item for item in report.mismatches)
    assert not any("tampered" in item for item in report.mismatches)
    assert not any(item == "total score" for item in report.mismatches)


def test_full_history_dense_round_stays_inside_the_budget() -> None:
    """Ledger round 3 with a full cross-match history of 6KiB dense sources. Cells are the bound; the clock is slack."""
    chs = [
        _ch(f"r{rnd}{author}{i}", rnd, author, _dense(6144, rnd * 10 + i + 5 * (author == "b")))
        for rnd in (1, 2, 3)
        for i in range(3)
        for author in ("a", "b")
    ]
    refs = {author: [_dense(6144, 100 + n) for n in range(27)] for author in ("a", "b")}
    rule = preset("ledger").ledger
    assert rule is not None
    sc = HeatScorer()
    started = time.perf_counter()
    got = sc.heat(chs, _valid(chs), 3, rule, refs)
    # Identical token sequences (names and int literals dropped) are a whole-source LCS of 100, no DP.
    assert sc.work == 0
    assert set(got) == {c.id for c in chs if c.round == 3}
    assert set(got.values()) == {100}
    assert sc.heat(chs, _valid(chs), 3, rule, refs) == got
    state = MatchState(
        match_id="dense",
        agents=(AgentSpec(name="a"), AgentSpec(name="b")),
        rules=preset("ledger"),
        limits=SandboxLimits(),
        heat_refs=refs,
    )
    state.challenges = {c.id: c for c in chs}
    state.validations = _valid(chs)
    memory.summarize(state, "a", 3, state.rules.memory, sc)
    memory.summarize(state, "a", 3, state.rules.memory, sc)
    assert sc.work == 0
    # Cell counts are the bound. 180s only catches a runaway walker; a slow runner has taken ~56s.
    assert time.perf_counter() - started < 180


def test_a_one_sided_pin_is_a_corrupt_log() -> None:
    rules = _legacy_rules()
    chs = [_ch("r1a", 1, "a", LOOP)]
    vals, atks = _valid(chs), {"r1a": _held("r1a")}
    delta = score_round(1, ["a", "b"], chs, vals, atks, rules)
    hot = round_heat(chs, vals, atks, 1, rules)
    _folded, events = _replay(rules, chs, {}, delta, hot, drop_caps=True)
    created = events[0]
    data = dict(created.data)
    dumped = dict(data["rules"])  # type: ignore[arg-type]
    ledger = dict(dumped["ledger"])
    ledger["heat_ref_cap"] = REF_CAP
    dumped["ledger"] = ledger
    data["rules"] = dumped
    events[0] = created.model_copy(update={"data": data})
    with pytest.raises(EventLogCorruptError, match="bad payload"):
        ev.fold("bound", events)


def test_reordered_long_sources_do_not_run_an_unbounded_lcs() -> None:
    """Same token multiset, different order, 20k tokens each: the exact LCS, fast, below the old ceiling."""
    left = "a = a + 1\n" * 4000
    right = "a = 1 + a\n" * 4000
    rule = preset("ledger").ledger
    assert rule is not None
    chs = [_ch("r1a", 1, "a", left), _ch("r2a", 2, "a", right)]
    sc = HeatScorer()
    started = time.perf_counter()
    got = sc.scan(chs, _valid(chs), 2, rule)
    assert time.perf_counter() - started < 5
    assert sc.work > 0
    assert got["r2a"].exact
    assert got["r2a"].pct < 100
    assert heat_label(got["r2a"]) == f"{got['r2a'].pct}%"
    assert sc.unchecked_ids(chs, _valid(chs), 2, rule) == []


def test_tokens_stop_inside_a_wide_compare() -> None:
    """Prefix compare only spends work on the first heat_token_cap tokens, even when the source is wider."""
    src, other = "a = a + 1\n" * 400, "a = 1 + a\n" * 400
    assert len(src) < PARSE_BYTES
    walk = profile(src)
    assert walk is not None
    assert TOKEN_CAP == len(tokens(src)) < walk.length
    sc = HeatScorer()
    assert 0 < similarity(src, other, token_cap=TOKEN_CAP, scorer=sc) < 100
    assert sc.work == work(TOKEN_CAP, TOKEN_CAP)


def test_card_says_legacy_unreplayable_when_heat_was_not_recomputed() -> None:
    svg = card_svg(
        CardData(
            match_id="m",
            players=[("a", "m", 1), ("b", "m", 0)],
            costs=["", ""],
            rounds="1/1",
            headline="level",
            spend="$0",
            verified=False,
            digest="0123456789abcdef",
            mismatches=("round 1 heat legacy-unreplayable: heat exceeds the work bound", "round 1 score not checked"),
        )
    )
    assert "replay heat legacy-unreplayable" in svg
    assert "MISMATCH" not in svg


def test_arena_heat_uses_refs_and_does_not_raise_past_the_ceiling() -> None:
    pinned = preset("ledger")
    state = MatchState(
        match_id="view",
        agents=(AgentSpec(name="a"), AgentSpec(name="b")),
        rules=pinned,
        limits=SandboxLimits(),
        heat_refs={"a": [LOOP]},
    )
    copied = _ch("r1a", 1, "a", RENAMED)
    state.challenges = {copied.id: copied}
    state.validations = _valid([copied])
    assert _heat_view(state, 1)["r1a"] == Hot(similarity(RENAMED, LOOP, token_cap=TOKEN_CAP), True)
    bare = state.model_copy(deep=True)
    bare.heat_refs = {}
    assert _heat_view(bare, 1) == {}
    legacy = state.model_copy(deep=True)
    assert legacy.rules.ledger is not None
    legacy.rules = legacy.rules.model_copy(update={"ledger": LedgerRule(heat_window=1)})
    dense = _ch("r1a", 1, "a", _dense(6144, 9))
    legacy.challenges = {dense.id: dense}
    legacy.validations = _valid([dense])
    legacy.heat_refs = {"a": [_dense(6144, 8)]}
    assert _heat_view(legacy, 1) == {}


def test_a_spent_call_budget_is_unchecked_not_charged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second pair finds the call budget spent: no LCS, no raise, no charge, labeled heat over budget."""
    rule = preset("ledger").ledger
    assert rule is not None
    old = _ch("r1a", 1, "a", "a = a + 1\n" * 5)
    first, second = (_ch(cid, 2, "a", "a = 1 + a\n" * 5) for cid in ("r2a", "r2b"))
    chs = [old, first, second]
    monkeypatch.setattr("claude_duel.domain.heat.WORK_BUDGET", work(35, 35) + 10)
    sc = HeatScorer()
    got = sc.scan(chs, _valid(chs), 2, rule)
    assert sc.work == work(35, 35)
    assert got["r2a"].exact
    assert got["r2a"].pct < 100
    assert got["r2b"] == Hot(0, False)
    assert "r2b" not in sc.heat(chs, _valid(chs), 2, rule)
    assert heat_label(got["r2b"]) == "heat over budget"
    assert heat_label(got["r2a"]) == f"{got['r2a'].pct}%"


def test_unparsed_reference_is_a_bound_and_history_skips_it() -> None:
    rule = preset("ledger").ledger
    assert rule is not None
    big = "x = 1\n" * (PARSE_BYTES // 6 + 1)
    chs = [_ch("r1a", 1, "a", "x = 1\n")]
    assert HeatScorer().scan(chs, _valid(chs), 1, rule, {"a": [big]}) == {"r1a": Hot(0, False)}
    match = _match("p0", "x", seq=0)
    match.challenges = {"0": _ch("0", 1, "a", big), "1": _ch("1", 1, "a", "y = 2\n")}
    match.validations = _valid(list(match.challenges.values()))
    assert history_refs([match], [AgentSpec(name="me", model="x")]) == {"me": ["y = 2\n"]}


def test_whole_source_heat_caps_admission_at_the_work_budget() -> None:
    """Full heat advertises a byte cap that a legal window can compare; legacy is still only the parse ceiling."""
    full = preset("ledger").model_copy(update={"max_source_bytes_base": 4096})
    cap = admit_bytes(full)
    assert cap < PARSE_BYTES
    assert full.max_source_bytes(20) == cap
    assert full.max_source_bytes(2) == min(8192, cap)
    assert _legacy_rules().model_copy(update={"max_source_bytes_base": 4096}).max_source_bytes(20) == 81920
    assert slots(full) * work(admit(full), admit(full)) <= WORK_BUDGET


def test_author_filter_applies_before_the_ref_cut() -> None:
    """#279 x #282: own-only heat picks the author's own window before ``heat_ref_cap`` keeps the newest."""
    own = LedgerRule(heat_window=2, heat_token_cap=TOKEN_CAP, heat_ref_cap=1, heat_compare="full")
    own = own.model_copy(update={"heat_vs_opponent": False})
    chs = [_ch("r1a", 1, "a", LOOP), _ch("r2b", 2, "b", OTHER), _ch("r3a", 3, "a", RENAMED)]
    assert HeatScorer().heat(chs, _valid(chs), 3, own) == {"r3a": 100}
    wide = own.model_copy(update={"heat_vs_opponent": True})
    assert HeatScorer().heat(chs, _valid(chs), 3, wide) == {}  # the newest reference is b's unrelated source


def test_config_pins_opponent_heat_and_the_caps_together() -> None:
    """S13-5 on top of the caps: an overlay inherits the preset's opponent heat; no preset means off."""

    def pinned(mode: str, **ledger: object) -> LedgerRule:
        rules = Settings.model_validate({"rules": {"mode": mode, "scoring": {"kind": "ledger"}, "ledger": ledger}})
        assert rules.rules.ledger is not None
        return rules.rules.ledger

    on, off = pinned("ledger", heat_window=2), pinned("custom", heat_window=2)
    assert (on.heat_vs_opponent, off.heat_vs_opponent) == (True, False)
    assert (on.heat_token_cap, on.heat_ref_cap, on.heat_compare, on.heat_metric, on.heat_history_matches) == (
        None,
        REF_CAP,
        "full",
        "lcs-v1",
        3,
    )


def test_large_unrelated_programs_are_compared_exactly_and_not_heated() -> None:
    """S13-4: two real, unrelated modules (thousands of tokens) share a token mix but not an order. The old
    multiset ceiling charged them at the threshold; the bit-parallel LCS is exact and below it."""
    root = Path(claude_duel.__file__).parent
    cli, arena = (root / "cli.py").read_text(), (root / "presentation" / "arena.py").read_text()
    left, right = tokens(cli, PARSE_BYTES), tokens(arena, PARSE_BYTES)
    assert min(len(left), len(right)) > 2000
    rule = preset("ledger").ledger
    assert rule is not None
    sc = HeatScorer()
    got = sc.similarity(cli, arena, rule)
    assert got.exact
    assert got.pct == 100 * bit_lcs(left, right) // max(len(left), len(right)) < rule.heat_threshold_pct
    chs = [_ch("r1b", 1, "b", arena), _ch("r2a", 2, "a", cli)]
    assert sc.scan(chs, _valid(chs), 2, rule) == {}


def test_bit_parallel_lcs_matches_the_dp() -> None:
    rng = random.Random(7)
    alphabet = "".join(chr(33 + i) for i in range(80))
    for _ in range(500):
        letters = alphabet[: rng.randint(2, 80)]
        a = tuple(rng.choice(letters) for _ in range(rng.randint(0, 120)))
        b = tuple(rng.choice(letters) for _ in range(rng.randint(0, 120)))
        prev = [0] * (len(b) + 1)
        for x in a:
            cur = [0]
            for j, y in enumerate(b):
                cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
            prev = cur
        assert bit_lcs(a, b) == prev[-1]


def test_unchecked_pair_is_never_charged() -> None:
    rule = preset("ledger").ledger
    assert rule is not None
    chs = [_ch("r1b", 1, "b", "a = a + 1\n" * 5), _ch("r2a", 2, "a", "a = 1 + a\n" * 5)]
    sc = HeatScorer()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("claude_duel.domain.heat.WORK_BUDGET", 0)
        got = sc.scan(chs, _valid(chs), 2, rule)
        assert got == {"r2a": Hot(0, False)}
        assert sc.heat(chs, _valid(chs), 2, rule) == {}
        assert sc.unchecked_ids(chs, _valid(chs), 2, rule) == ["r2a"]
    assert heat_label(got["r2a"]) == "heat over budget"
    assert heat_label(Hot(80, False)) == ">=80% (heat over budget)"


def test_multiset_ceiling_skips_a_cold_pair() -> None:
    """S13-4: when the overlap bound is below the threshold, the exact LCS is not run and nothing is charged."""
    rule = preset("ledger").ledger
    assert rule is not None
    left, right = "x = 1\n" * 40, "def f():\n    return 2\n" * 40
    a, b = profile(left), profile(right)
    assert a is not None
    assert b is not None
    bound = 100 * overlap(a.counts, b.counts) // max(a.length, b.length)
    assert bound < rule.heat_threshold_pct
    sc = HeatScorer()
    assert sc.similarity(left, right, rule) == Hot(0, True)
    assert sc.work == 0
    chs = [_ch("r1a", 1, "a", left), _ch("r2b", 2, "b", right)]
    assert sc.scan(chs, _valid(chs), 2, rule) == {}
    assert sc.heat(chs, _valid(chs), 2, rule) == {}
    assert sc.unchecked_ids(chs, _valid(chs), 2, rule) == []


def test_exact_copy_past_32k_tokens_is_charged() -> None:
    """An exact copy longer than the old 32,768-token drop is still compared and charged 100 (never unchecked)
    when it is already in the ledger. New full-heat admission refuses a walk this long: it would not fit a
    packed window's work budget.
    """
    template = next(t for t in TEMPLATES if t.key == "sum")
    unit, prefix = profile("1\n"), profile(template.source)
    assert unit is not None
    assert prefix is not None
    lines = (32_768 - prefix.length) // unit.length + 1
    source = "1\n" * lines + template.source
    walked = profile(source)
    assert walked is not None
    assert walked.length == len(walked.tokens) > 32_768
    assert work(walked.length, walked.length) <= WORK_BUDGET
    rules = preset("ledger")
    assert len(source.encode()) > rules.max_source_bytes(20)
    assert heavy(source, rules) is not None
    rule = rules.ledger
    assert rule is not None
    sc = HeatScorer()
    assert sc.similarity(source, source, rule) == Hot(100, True)
    assert sc.work == 0  # a copied body is decided in linear time
    chs = [_ch("r11a", 11, "a", source), _ch("r12a", 12, "a", source)]
    assert sc.heat(chs, _valid(chs), 12, rule) == {"r12a": 100}
    assert sc.unchecked_ids(chs, _valid(chs), 12, rule) == []


def _near_copy(target: int) -> tuple[str, str]:
    """Two programs at ``target`` bytes: ``1`` / ``+1`` blocks swapped, plus the shipped sum template.

    At 40,960 bytes this is the PR 282 review repro (76% exact, not a subsequence).
    """
    template = next(t for t in TEMPLATES if t.key == "sum").source
    room = max(0, target - len(template.encode()))
    # Mix that measured 76% at 40,959 bytes: 8,971 ones and 7,600 plus-ones.
    plus_bytes = (3 * 7600) * room // (2 * 8971 + 3 * 7600)
    n_plus = plus_bytes // 3
    n_ones = (room - 3 * n_plus) // 2
    a = "1\n" * n_ones + "+1\n" * n_plus + template
    b = "+1\n" * n_plus + "1\n" * n_ones + template
    while max(len(a.encode()), len(b.encode())) > target and n_ones:
        n_ones -= 1
        a = "1\n" * n_ones + "+1\n" * n_plus + template
        b = "+1\n" * n_plus + "1\n" * n_ones + template
    return a, b


def test_packed_round20_near_copy_is_compared_exactly() -> None:
    """A 40,960-byte 76% near-copy in a packed ledger 6-vs-6 window used to exhaust the 1e9 word budget;
    later challenges came back heat over budget and were charged 0. Admission now sizes the cap so every
    pair the rules can admit fits, and a packed window at that cap is charged on the exact LCS.
    """
    rules = preset("ledger").model_copy(update={"rounds": 20})
    cap = rules.max_source_bytes(20)
    assert cap < 40_960
    over, _ = _near_copy(40_960)
    assert len(over.encode()) <= 40_960
    assert policy.check(spec(over), cap) is not None
    assert heavy(over, rules) is not None
    left, right = _near_copy(cap)
    assert max(len(left.encode()), len(right.encode())) <= cap
    assert policy.check(spec(left), cap) is None
    assert policy.check(spec(right), cap) is None
    assert heavy(left, rules) is None
    assert heavy(right, rules) is None
    prev = [_ch(f"p{i}", 19, "a" if i < 3 else "b", left if i % 2 == 0 else right) for i in range(6)]
    current = [_ch(f"c{i}", 20, "a" if i < 3 else "b", left if i % 2 == 0 else right) for i in range(6)]
    chs = prev + current
    rule = rules.ledger
    assert rule is not None
    sc = HeatScorer()
    got = sc.scan(chs, _valid(chs), 20, rule)
    assert sc.unchecked_ids(chs, _valid(chs), 20, rule) == []
    assert set(got) == {c.id for c in current}
    assert all(h.exact and h.pct >= rule.heat_threshold_pct for h in got.values())
    assert sc.heat(chs, _valid(chs), 20, rule) == {cid: h.pct for cid, h in got.items()}
    assert sc.work <= WORK_BUDGET
