"""Host work bound for novelty heat: capped LCS, pinned replay, legacy logs that no longer fit."""

from __future__ import annotations

import ast
import time
from collections.abc import Mapping

import pytest

from claude_duel.app.agents import build_prompt
from claude_duel.app.container import build_store
from claude_duel.app.replay import replay
from claude_duel.app.session import open_match
from claude_duel.config import Settings
from claude_duel.domain import events as ev
from claude_duel.domain import memory
from claude_duel.domain.errors import HeatBoundError
from claude_duel.domain.heat import (
    CELL_BUDGET,
    MAX_CHALLENGES,
    PARSE_BYTES,
    REF_CAP,
    TOKEN_CAP,
    Hot,
    bound_ids,
    heat,
    heat_label,
    heat_scan,
    history_refs,
    pin_heat_bound,
    pinned_bound,
    reset_heat_cache,
    similarity,
    tokens,
    work_cells,
)
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
from tests.unit.test_domain import spec
from tests.unit.test_heat import LOOP, RENAMED, _match


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
    return base.model_copy(update={"ledger": led})


def _replay(
    rules: Rules,
    chs: list[Challenge],
    refs: Mapping[str, list[str]],
    delta: dict[str, int],
    hot: dict[str, int],
    *,
    drop_caps: bool,
) -> tuple[MatchState, list[Event]]:
    dumped = rules.model_dump(mode="json")
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
    assert CELL_BUDGET == MAX_CHALLENGES * REF_CAP * TOKEN_CAP * TOKEN_CAP


def test_short_sources_keep_full_similarity() -> None:
    assert similarity(LOOP, RENAMED) == 100
    assert similarity(LOOP, RENAMED, token_cap=TOKEN_CAP) == 100
    assert len(tokens(LOOP)) < TOKEN_CAP


def test_dense_prefix_matches_a_copy_and_legacy_does_not_run_lcs() -> None:
    src = _dense(6144, 1)
    assert len(tokens(src)) == TOKEN_CAP
    reset_heat_cache()
    started = time.perf_counter()
    with pytest.raises(HeatBoundError, match="work bound"):
        similarity(src, _dense(6144, 2))
    assert work_cells() == 0
    assert similarity(src, src, token_cap=TOKEN_CAP) == 100
    assert work_cells() == TOKEN_CAP * TOKEN_CAP
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

    monkeypatch.setattr("claude_duel.domain.heat.ast.parse", counting)
    src = "v = 1\n" + "x" * (PARSE_BYTES + 10)
    reset_heat_cache()
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

    monkeypatch.setattr("claude_duel.domain.heat.ast.parse", counting)
    reset_heat_cache()
    got = heat([window, current], _valid([window, current]), 2, rule, {"a": [*old, "v = 1\n"]})
    assert got["r2a"] == similarity(RENAMED, LOOP, token_cap=TOKEN_CAP)
    assert all(not item.startswith("z = ") for item in seen)


def test_too_many_challenges_or_legacy_refs_raise_before_lcs() -> None:
    reset_heat_cache()
    many = [_ch(f"c{i:02d}", 1, "a", LOOP) for i in range(MAX_CHALLENGES + 1)]
    pinned = LedgerRule(heat_window=1, heat_token_cap=8, heat_ref_cap=1)
    with pytest.raises(HeatBoundError):
        heat(many, _valid(many), 1, pinned)
    one = [_ch("r1a", 1, "a", LOOP)]
    with pytest.raises(HeatBoundError):
        heat(one, _valid(one), 1, LedgerRule(heat_window=1), {"a": [LOOP] * (REF_CAP + 1)})
    assert work_cells() == 0


def test_a_logged_cap_above_the_ceiling_is_not_clamped_into_a_score() -> None:
    reset_heat_cache()
    rule = LedgerRule(heat_window=1, heat_token_cap=10**9, heat_ref_cap=10**9)
    chs = [_ch("r1a", 1, "a", LOOP)]
    with pytest.raises(HeatBoundError):
        heat(chs, _valid(chs), 1, rule, {"a": [LOOP] * (REF_CAP + 12)})
    assert work_cells() == 0


def test_pin_fills_and_clamps_only_when_heat_is_on() -> None:
    assert pin_heat_bound(preset("classic")) is preset("classic")
    assert pin_heat_bound(preset("ledger")) is preset("ledger")
    bare = preset("ledger").model_copy(
        update={"ledger": LedgerRule(heat_window=2, heat_token_cap=10**6, heat_ref_cap=10**6)}
    )
    pinned = pin_heat_bound(bare)
    assert pinned.ledger is not None
    assert (pinned.ledger.heat_token_cap, pinned.ledger.heat_ref_cap) == (TOKEN_CAP, REF_CAP)


def test_pinned_oversize_source_scores_as_empty() -> None:
    src = "v = 1\n" + "y" * (PARSE_BYTES + 5)
    assert similarity(src, LOOP, token_cap=TOKEN_CAP) == 0


def test_an_overflowing_window_keeps_the_newest_rounds() -> None:
    older = [_ch("r1a", 1, "a", LOOP), _ch("r2a", 2, "a", "v = 1\n"), _ch("r3a", 3, "a", RENAMED)]
    rule = LedgerRule(heat_window=3, heat_token_cap=TOKEN_CAP, heat_ref_cap=1)
    # Cap 1 keeps round 2 only, so the copied LOOP in round 1 and in history does not heat.
    cold = heat(older, _valid(older), 3, rule, {"a": [LOOP]})
    assert "r3a" not in cold
    newer = [_ch("r1a", 1, "a", "v = 1\n"), _ch("r2a", 2, "a", LOOP), _ch("r3a", 3, "a", RENAMED)]
    assert heat(newer, _valid(newer), 3, rule, {"a": ["v = 2\n"]})["r3a"] == 100


def test_a_partial_or_mistyped_pin_is_not_filled() -> None:
    with pytest.raises(HeatBoundError):
        pinned_bound(LedgerRule(heat_window=1, heat_token_cap=32))
    with pytest.raises(HeatBoundError):
        pinned_bound(LedgerRule(heat_window=1, heat_ref_cap=3))
    for raw in (True, "256", 256.0):
        led = LedgerRule.model_validate({"heat_window": 1, "heat_token_cap": raw, "heat_ref_cap": raw})
        with pytest.raises(HeatBoundError):
            pinned_bound(led)
    # A new match still normalizes a missing config pin into the logged ceiling.
    bare = preset("ledger").model_copy(update={"ledger": LedgerRule(heat_window=2, heat_token_cap=32)})
    written = pin_heat_bound(bare)
    assert written.ledger is not None
    assert (written.ledger.heat_token_cap, written.ledger.heat_ref_cap) == (32, REF_CAP)


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
    assert (created.rules.ledger.heat_token_cap, created.rules.ledger.heat_ref_cap) == (TOKEN_CAP, REF_CAP)
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
    reset_heat_cache()
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


def test_legacy_dense_log_is_unverified_not_rescored() -> None:
    rules = _legacy_rules()
    src = _dense(6144, 3)
    chs = [_ch("r1a", 1, "a", src), _ch("r1b", 1, "b", src)]
    reset_heat_cache()
    started = time.perf_counter()
    folded, events = _replay(rules, chs, {"a": [src], "b": [src]}, {"a": 1, "b": 1}, {"r1a": 1}, drop_caps=True)
    report = replay(folded, events)
    assert time.perf_counter() - started < 2
    assert work_cells() == 0
    assert report.consistent is False
    assert any("heat unverified" in item for item in report.mismatches)
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
    reset_heat_cache()
    started = time.perf_counter()
    got = heat(chs, _valid(chs), 3, rule, refs)
    # Identical token sequences (names and int literals dropped) are a whole-source LCS of 100, no DP.
    assert work_cells() == 0
    assert set(got) == {c.id for c in chs if c.round == 3}
    assert set(got.values()) == {100}
    again = work_cells()
    heat(chs, _valid(chs), 3, rule, refs)
    assert work_cells() == again
    state = MatchState(
        match_id="dense",
        agents=(AgentSpec(name="a"), AgentSpec(name="b")),
        rules=preset("ledger"),
        limits=SandboxLimits(),
        heat_refs=refs,
    )
    state.challenges = {c.id: c for c in chs}
    state.validations = _valid(chs)
    memory.summarize(state, "a", 3, state.rules.memory)
    prompted = work_cells()
    memory.summarize(state, "a", 3, state.rules.memory)
    assert work_cells() == prompted == 0
    assert prompted <= CELL_BUDGET
    # Cell counts are the bound. 180s only catches a runaway walker; a slow runner has taken ~56s.
    assert time.perf_counter() - started < 180


def test_a_one_sided_pin_replays_as_unverified() -> None:
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
    folded = ev.fold("bound", events)
    report = replay(folded, events)
    assert report.consistent is False
    assert any("heat unverified" in item for item in report.mismatches)
    assert any("score not checked" in item for item in report.mismatches)


def test_reordered_long_sources_do_not_run_an_unbounded_lcs() -> None:
    """Same token multiset, different order, too big for one DP: no DP, no raise, a labeled upper bound."""
    left = "a = a + 1\n" * 4000
    right = "a = 1 + a\n" * 4000
    rule = preset("ledger").ledger
    assert rule is not None
    chs = [_ch("r1a", 1, "a", left), _ch("r2a", 2, "a", right)]
    reset_heat_cache()
    started = time.perf_counter()
    got = heat_scan(chs, _valid(chs), 2, rule)
    assert time.perf_counter() - started < 5
    assert work_cells() == 0
    assert got == {"r2a": Hot(100, False)}
    assert heat_label(got["r2a"]) == "<=100% (bound)"
    assert bound_ids(chs, _valid(chs), 2, rule) == ["r2a"]


def test_tokens_stop_inside_a_wide_compare() -> None:
    src = "x = 1" + "==1" * 3000 + "\n"
    assert len(src) < PARSE_BYTES
    assert len(tokens(src)) == TOKEN_CAP
    reset_heat_cache()
    assert similarity(src, src, token_cap=TOKEN_CAP) == 100
    assert work_cells() == TOKEN_CAP * TOKEN_CAP


def test_card_says_unverified_when_heat_was_not_recomputed() -> None:
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
            mismatches=("round 1 heat unverified: heat exceeds the work bound", "round 1 score not checked"),
        )
    )
    assert "replay unverified" in svg
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


def test_a_spent_call_budget_charges_a_labeled_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second pair finds the call budget spent: no DP, no raise, the multiset ceiling labeled as a bound."""
    rule = preset("ledger").ledger
    assert rule is not None
    old = _ch("r1a", 1, "a", "a = a + 1\n" * 5)
    first, second = (_ch(cid, 2, "a", "a = 1 + a\n" * 5) for cid in ("r2a", "r2b"))
    chs = [old, first, second]
    monkeypatch.setattr("claude_duel.domain.heat.CELL_BUDGET", 35 * 35 + 10)
    reset_heat_cache()
    got = heat_scan(chs, _valid(chs), 2, rule)
    assert work_cells() == 35 * 35
    assert got["r2a"].exact
    assert got["r2a"].pct < 100
    assert got["r2b"] == Hot(100, False)
    assert heat_label(got["r2a"]) == f"{got['r2a'].pct}%"


def test_unparsed_reference_is_a_bound_and_history_skips_it() -> None:
    rule = preset("ledger").ledger
    assert rule is not None
    big = "x = 1\n" * (PARSE_BYTES // 6 + 1)
    chs = [_ch("r1a", 1, "a", "x = 1\n")]
    reset_heat_cache()
    assert heat_scan(chs, _valid(chs), 1, rule, {"a": [big]}) == {"r1a": Hot(100, False)}
    match = _match("p0", "x", seq=0)
    match.challenges = {"0": _ch("0", 1, "a", big), "1": _ch("1", 1, "a", "y = 2\n")}
    match.validations = _valid(list(match.challenges.values()))
    assert history_refs([match], [AgentSpec(name="me", model="x")]) == {"me": ["y = 2\n"]}


def test_whole_source_heat_caps_admission_at_the_parse_ceiling() -> None:
    full = preset("ledger").model_copy(update={"max_source_bytes_base": 4096})
    assert full.max_source_bytes(20) == PARSE_BYTES
    assert full.max_source_bytes(2) == 8192
    assert _legacy_rules().model_copy(update={"max_source_bytes_base": 4096}).max_source_bytes(20) == 81920
