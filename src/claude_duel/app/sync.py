"""``duel sync`` (ADR 0012): verify each event log, replay it, and load its public projection into the results DB.

The logs stay the source of truth. A match is loaded only if its hash chain verifies and its replay matches the
snapshot; otherwise it is recorded as a reject. Every row is keyed on an event hash, so re-running inserts nothing
new. A hash the DB holds but the log no longer has means history was rewritten: sync halts. ``<id>.secret`` files
are never opened (``FileMatchStore.snapshot``) and event payloads pass ``domain/public.py`` before storage.
"""

from __future__ import annotations

from dataclasses import dataclass

from claude_duel.adapters.store.event_log import FileEventLog
from claude_duel.adapters.store.file_store import FileMatchStore
from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.errors import DuelError, HistoryRewrittenError
from claude_duel.domain.flags import scrub
from claude_duel.domain.models import Event, MatchState, Stage
from claude_duel.domain.public import public_data
from claude_duel.domain.ratings import ELO_K, entrant, ratings, results
from claude_duel.domain.series import game_winner
from claude_duel.ports import ResultsStore, Row

ELO_ORDER = "match_id ascending"  # domain/ratings.py folds Elo in this order; stored with every snapshot


@dataclass(frozen=True)
class SyncReport:
    inserted: int  # matches with new events
    skipped: int  # matches already up to date
    rejected: dict[str, str]  # match id -> reason
    run_id: int


def sync(log: FileEventLog, snapshots: FileMatchStore, db: ResultsStore, boot: int = 200) -> SyncReport:
    """Load every log in the store, then append a ratings snapshot over all verified matches."""
    run = db.start_run()
    loaded: list[MatchState] = []
    rejected: dict[str, str] = {}
    inserted = skipped = 0
    for mid in log.ids():
        try:
            state, chain = _verified(log, snapshots, mid)
        except DuelError as exc:
            rejected[mid] = _reason(exc)
            db.reject(run, mid, rejected[mid])
            continue
        stored = db.stored_hashes(mid)
        if not stored <= {h for h, _ in chain}:
            raise HistoryRewrittenError(f"{mid}: the results DB has events its log no longer has")
        loaded.append(state)
        if chain[-1][0] in stored:
            skipped += 1
            continue
        db.ingest(rows(state, chain))
        inserted += 1
    table = _rating_rows(loaded, boot)
    db.finish_run(run, {"inserted": inserted, "skipped": skipped, "rejected": len(rejected)}, table)
    return SyncReport(inserted, skipped, rejected, run)


def _reason(exc: DuelError) -> str:
    return f"{type(exc).__name__}: {exc}"


def _verified(log: FileEventLog, snapshots: FileMatchStore, mid: str) -> tuple[MatchState, list[tuple[str, Event]]]:
    """Snapshot and the chain up to it, or a ``DuelError`` saying why the match can't be loaded."""
    state = snapshots.snapshot(mid)
    chain = [(h, e) for h, e in log.chain(mid) if e.seq <= state.last_seq]
    report = replay(state, [e for _, e in chain])
    if not chain or not report.consistent:
        raise DuelError("replay mismatch: " + (", ".join(report.mismatches) or "no events"))
    return state, chain


def rows(state: MatchState, chain: list[tuple[str, Event]]) -> dict[str, list[Row]]:
    """Every table's rows for one verified match, in insert order. Only allowlisted facts are kept."""
    mid = state.match_id
    prevs = ["0" * 64] + [h for h, _ in chain[:-1]]
    events = [
        {"event_hash": h, "prev_hash": p, "match_id": mid, "seq": e.seq, "kind": e.type, "v": e.v}
        | {"data": public_data(e)}
        for (h, e), p in zip(chain, prevs, strict=True)
    ]
    return {
        "matches": [_match_row(state, chain)],
        "entrants": [_entrant_row(a.provider, a.model, entrant(a)) for a in state.agents],
        "match_agents": [
            {"match_id": mid, "agent_name": a.name, "entrant_id": entrant(a), "handicap": a.handicap.model_dump()}
            for a in state.agents
        ],
        "match_results": [_result_row(state)],
        "events": events,
        "calls": [_call_row(mid, h, e) for h, e in chain if e.type == ev.CALL_CHARGED],
        "attacks": [_attack_row(state, h, e) for h, e in chain if e.type == ev.CHALLENGE_ATTACKED],
        "round_scores": [r for h, e in chain if e.type == ev.ROUND_SCORED for r in _score_rows(mid, h, e)],
    }


def _match_row(state: MatchState, chain: list[tuple[str, Event]]) -> Row:
    r = state.rules
    return {
        "match_id": state.match_id,
        "head_hash": chain[-1][0],
        "event_count": len(chain),
        "stage": state.stage.value,
        "round": state.round,
        "mode": r.mode,
        "challenge_form": r.challenge_form,
        "framing": r.framing.kind,
        "rules": r.model_dump(mode="json"),
        "limits": state.limits.model_dump(mode="json"),
        "failure": None if state.failure is None else scrub(state.failure),
    }


def _entrant_row(provider: str, model: str | None, entrant_id: str) -> Row:
    return {"entrant_id": entrant_id, "provider": provider, "model": model or "default", "is_fake": provider == "fake"}


def _result_row(state: MatchState) -> Row:
    u = state.usage
    winner = game_winner(state) if state.stage is Stage.COMPLETE else None
    return {
        "match_id": state.match_id,
        "winner": winner,
        "scores": state.final_scores(),
        "calls": u.calls,
        "tokens": u.tokens,
        "usd": round(u.usd, 6),
    }


def _call_row(mid: str, h: str, e: Event) -> Row:
    return {"event_hash": h, "match_id": mid} | ev.CallCharged.model_validate(e.data).model_dump()


def _attack_row(state: MatchState, h: str, e: Event) -> Row:
    a = ev.ChallengeAttacked.model_validate(e.data).attack
    c = state.challenges[a.challenge_id]
    return {
        "event_hash": h,
        "match_id": state.match_id,
        "challenge_id": a.challenge_id,
        "round": c.round,
        "author": c.author,
        "attacker": a.attacker,
        "solved": a.solved,
        "attempts": a.attempts,
        "tokens_used": a.tokens_used,
        "usd_micros": a.usd_micros,
    }


def _score_rows(mid: str, h: str, e: Event) -> list[Row]:
    p = ev.RoundScored.model_validate(e.data)
    return [
        {"event_hash": h, "match_id": mid, "round": p.round, "agent": n, "points": v}
        for n, v in sorted(p.delta.items())
    ]


def _rating_rows(states: list[MatchState], boot: int) -> list[Row]:
    rs = results(states)
    last = rs[-1].match_id if rs else ""
    return [
        {"entrant_id": r.entrant, "games": r.games, "elo": r.elo, "bt": r.bt, "bt_lo": r.lo, "bt_hi": r.hi}
        | {"elo_order": ELO_ORDER, "elo_k": ELO_K, "through_match": last}
        for r in ratings(rs, boot=boot)
    ]
