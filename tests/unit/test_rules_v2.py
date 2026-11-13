"""#258: rules v2 / schema v3 heat contract; v1 logs with heat_token_cap still replay."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from claude_duel.app.replay import replay
from claude_duel.config import Settings
from claude_duel.domain import events as ev
from claude_duel.domain.errors import EventLogCorruptError
from claude_duel.domain.heat_bound import regime
from claude_duel.domain.heat_tokens import REF_CAP, TOKEN_CAP
from claude_duel.domain.models import RULES_VERSION, AgentSpec, Event, LedgerRule, Rules, SandboxLimits
from claude_duel.domain.modes import preset
from claude_duel.domain.scoring import round_heat, score_round
from tests.unit.test_heat import LOOP
from tests.unit.test_heat_bound import _ch, _held, _valid


def _v1_full_rules() -> Rules:
    """A heat-on log as written on main before #258: rules v1, token cap present, no metric pin."""
    base = preset("ledger")
    led = base.ledger
    assert led is not None
    pins = {"heat_token_cap": TOKEN_CAP, "heat_metric": None, "heat_history_matches": None}
    return base.model_copy(update={"rules_version": 1, "ledger": led.model_copy(update=pins)})


def _score_events(rules: Rules, *, schema: int) -> list[Event]:
    chs = [_ch("r1a", 1, "a", LOOP), _ch("r2a", 2, "a", LOOP)]
    vals, atks = _valid(chs), {"r2a": _held("r2a")}
    delta = score_round(2, ["a", "b"], chs, vals, atks, rules)
    hot = round_heat(chs, vals, atks, 2, rules)
    data: dict[str, object] = {
        "seed": "s",
        "agents": [a.model_dump(mode="json") for a in (AgentSpec(name="a"), AgentSpec(name="b"))],
        "rules": rules.dump(),
        "limits": SandboxLimits().model_dump(mode="json"),
    }
    events = [Event(v=schema, seq=0, type=ev.MATCH_CREATED, trace_id="t", data=data)]
    seq = 1
    for ch in chs:
        events.append(
            Event(
                v=schema,
                seq=seq,
                type=ev.CHALLENGE_BUILT,
                trace_id="t",
                data={"challenge": ch.model_dump(mode="json")},
            )
        )
        seq += 1
        events.append(
            Event(
                v=schema,
                seq=seq,
                type=ev.CHALLENGE_VALIDATED,
                trace_id="t",
                data={"validation": vals[ch.id].model_dump(mode="json")},
            )
        )
        seq += 1
    events.append(
        Event(
            v=schema,
            seq=seq,
            type=ev.CHALLENGE_ATTACKED,
            trace_id="t",
            data={"key": "r2a:b", "attack": atks["r2a"].model_dump(mode="json")},
        )
    )
    events.append(
        Event(v=schema, seq=seq + 1, type=ev.ROUND_SCORED, trace_id="t", data={"round": 2, "delta": delta, "heat": hot})
    )
    return events


def test_rules_and_schema_versions_are_bumped() -> None:
    assert (RULES_VERSION, ev.SCHEMA_VERSION) == (2, 3)
    assert preset("ledger").rules_version == 2


def test_v1_full_heat_log_with_token_cap_replays() -> None:
    rules = _v1_full_rules()
    assert rules.ledger is not None
    assert rules.ledger.heat_token_cap == TOKEN_CAP
    dumped = rules.dump()["ledger"]
    assert dumped["heat_token_cap"] == TOKEN_CAP
    assert "heat_metric" in dumped  # model_dump keeps the null; readers accept either
    events = _score_events(rules, schema=2)
    assert events[0].v == 2
    assert events[0].data["rules"]["ledger"]["heat_token_cap"] == TOKEN_CAP
    folded = ev.fold("v1-heat", events)
    assert folded.rules.rules_version == 1
    assert folded.rules.ledger is not None
    assert folded.rules.ledger.heat_token_cap == TOKEN_CAP
    assert folded.rules.ledger.heat_metric is None
    report = replay(folded, events)
    assert report.consistent, report.mismatches


def test_v1_prefix_heat_log_with_token_cap_replays() -> None:
    base = preset("ledger")
    led = base.ledger
    assert led is not None
    rules = base.model_copy(
        update={
            "rules_version": 1,
            "ledger": led.model_copy(
                update={
                    "heat_compare": "prefix",
                    "heat_token_cap": TOKEN_CAP,
                    "heat_metric": None,
                    "heat_history_matches": None,
                }
            ),
        }
    )
    assert rules.ledger is not None
    assert regime(rules.ledger) == "prefix"
    assert rules.dump()["ledger"]["heat_token_cap"] == TOKEN_CAP
    events = _score_events(rules, schema=2)
    folded = ev.fold("v1-prefix", events)
    assert folded.rules.rules_version == 1
    assert folded.rules.ledger is not None
    assert folded.rules.ledger.heat_compare == "prefix"
    report = replay(folded, events)
    assert report.consistent, report.mismatches


def test_v2_dump_omits_heat_token_cap() -> None:
    rules = preset("ledger")
    assert rules.ledger is not None
    assert rules.ledger.heat_token_cap is None
    dumped = rules.dump()["ledger"]
    assert "heat_token_cap" not in dumped
    assert dumped["heat_compare"] == "full"
    assert dumped["heat_vs_opponent"] is True
    assert dumped["heat_ref_cap"] == REF_CAP
    assert dumped["heat_metric"] == "lcs-v1"
    assert dumped["heat_history_matches"] == 3
    created = ev.created_data(
        ev.initial_state(
            "new",
            Event(
                v=ev.SCHEMA_VERSION,
                seq=0,
                type=ev.MATCH_CREATED,
                trace_id="t",
                data={
                    "seed": "s",
                    "agents": [a.model_dump(mode="json") for a in (AgentSpec(name="a"), AgentSpec(name="b"))],
                    "rules": rules.dump(),
                    "limits": SandboxLimits().model_dump(mode="json"),
                },
            ),
        )
    )
    assert "heat_token_cap" not in created["rules"]["ledger"]  # type: ignore[index]


@pytest.mark.parametrize(
    "drop",
    ["heat_compare", "heat_vs_opponent", "heat_ref_cap", "heat_metric", "heat_history_matches"],
)
def test_v2_heat_on_log_without_a_required_pin_is_corrupt(drop: str) -> None:
    rules = preset("ledger")
    data = {
        "seed": "s",
        "agents": [a.model_dump(mode="json") for a in (AgentSpec(name="a"), AgentSpec(name="b"))],
        "rules": rules.dump(),
        "limits": SandboxLimits().model_dump(mode="json"),
    }
    ledger = dict(data["rules"]["ledger"])  # type: ignore[arg-type, index]
    ledger.pop(drop)
    if drop == "heat_compare":
        ledger["heat_token_cap"] = TOKEN_CAP  # otherwise this would look like a partial pin
    data["rules"] = {**data["rules"], "ledger": ledger}  # type: ignore[arg-type, dict-item]
    with pytest.raises(EventLogCorruptError, match="bad payload"):
        ev.payload(Event(v=3, seq=0, type=ev.MATCH_CREATED, trace_id="t", data=data))


def test_v2_rejects_prefix_compare_on_a_heat_on_log() -> None:
    rules = preset("ledger")
    data = {
        "seed": "s",
        "agents": [a.model_dump(mode="json") for a in (AgentSpec(name="a"), AgentSpec(name="b"))],
        "rules": rules.dump(),
        "limits": SandboxLimits().model_dump(mode="json"),
    }
    ledger = dict(data["rules"]["ledger"])  # type: ignore[arg-type, index]
    ledger["heat_compare"] = "prefix"
    ledger["heat_token_cap"] = TOKEN_CAP
    data["rules"] = {**data["rules"], "ledger": ledger}  # type: ignore[arg-type, dict-item]
    with pytest.raises(EventLogCorruptError, match="bad payload"):
        ev.payload(Event(v=3, seq=0, type=ev.MATCH_CREATED, trace_id="t", data=data))
    with pytest.raises(ValidationError, match="heat_compare=full"):
        Rules.model_validate(data["rules"])


def test_v2_classic_needs_no_heat_pins() -> None:
    assert Rules().rules_version == 2
    assert Rules().ledger is None
    ev.payload(
        Event(
            v=3,
            seq=0,
            type=ev.MATCH_CREATED,
            trace_id="t",
            data={
                "seed": "s",
                "agents": [a.model_dump(mode="json") for a in (AgentSpec(name="a"), AgentSpec(name="b"))],
                "rules": Rules().dump(),
                "limits": SandboxLimits().model_dump(mode="json"),
            },
        )
    )


def test_config_pin_writes_the_v2_contract() -> None:
    loaded = Settings(rules=preset("ledger"))
    led = loaded.rules.ledger
    assert loaded.rules.rules_version == 2
    assert led is not None
    assert regime(led) == "full"
    assert led.heat_token_cap is None
    assert "heat_token_cap" not in loaded.rules.dump()["ledger"]


def test_full_regime_does_not_need_a_token_cap() -> None:
    rule = LedgerRule(heat_window=2, heat_ref_cap=REF_CAP, heat_compare="full", heat_metric="lcs-v1")
    assert regime(rule) == "full"
    assert rule.heat_token_cap is None
