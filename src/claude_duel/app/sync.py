"""``duel sync`` (ADR 0012): verify each event log, replay it, and load its public projection into the results DB.

The logs stay the source of truth. A match is loaded only if its hash chain verifies and its replay matches the
snapshot; otherwise it is recorded as a reject. Every row is keyed on an event hash, so re-running inserts nothing
new. A hash the DB holds but the log no longer has means history was rewritten: sync halts. ``<id>.secret`` files
are never opened (``MatchStore.snapshot``) and event payloads pass ``domain/public.py`` before storage.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import nullcontext, suppress
from dataclasses import dataclass

from pydantic import ValidationError

from claude_duel.app.replay import replay
from claude_duel.domain import events as ev
from claude_duel.domain.errors import DuelError, FlagLeakError, HistoryRewrittenError, ReplayMismatchError
from claude_duel.domain.flags import flag_shaped, scrub
from claude_duel.domain.models import Event, MatchState, Stage
from claude_duel.domain.public import public_data, public_text
from claude_duel.domain.ratings import ELO_K, entrant, ratings, results
from claude_duel.domain.results import (
    AgentRow,
    AttackRow,
    CallRow,
    EntrantRow,
    EventRow,
    MatchProjection,
    MatchRow,
    RatingRow,
    ResultRow,
    RunCounts,
    ScoreRow,
)
from claude_duel.domain.series import game_winner
from claude_duel.ports import EventLog, MatchStore, ResultsStore

ELO_ORDER = "match_id ascending"  # domain/ratings.py folds Elo in this order; stored with every snapshot
PROJECTION_VERSION = 1  # bump when rows() changes; stored matches with another version are re-projected
REASON_MAX = 300
# A bad log or snapshot is the match's problem, not the run's: these become rejects (pydantic's ValidationError
# is a ValueError; it is listed for clarity). Anything else (DB errors, bugs) fails the run.
_REJECTABLE = (DuelError, ValidationError, KeyError, ValueError, TypeError, OSError)


@dataclass(frozen=True)
class SyncReport:
    inserted: int  # matches with new events (or re-projected)
    skipped: int  # matches already up to date
    rejected: dict[str, str]  # match id -> reason
    run_id: str
    ratings_written: bool = True


def sync(
    log: EventLog,
    snapshots: MatchStore,
    db: ResultsStore,
    boot: int = 200,
    *,
    rebuild: bool = False,
    git_sha: str | None = None,
) -> SyncReport:
    """Load every log in the store, then append a ratings snapshot over all verified matches.

    The run is recorded first and always closed: ``ok``, ``halted`` (history rewritten) or ``failed`` (anything
    else), so a crash never looks like a no-op. With ``rebuild`` the derived tables are emptied and reloaded in one
    transaction, so readers never see an empty database.
    """
    run = str(uuid.uuid4())
    db.start_run(run, git_sha)
    counts = RunCounts()
    try:
        with db.rebuilding() if rebuild else nullcontext():
            loaded, rejected = _load(log, snapshots, db, run, counts)
        table = _rating_rows(loaded, boot)
        digest = _digest(loaded, boot)
        fresh = digest != db.last_ratings_digest()
        db.finish_run(run, "ok", counts, table if fresh else [], digest)
    except HistoryRewrittenError:
        _close(db, run, "halted", counts)
        raise
    except BaseException:
        _close(db, run, "failed", counts)
        raise
    return SyncReport(counts.inserted, counts.skipped, rejected, run, fresh)


def _load(
    log: EventLog, snapshots: MatchStore, db: ResultsStore, run: str, counts: RunCounts
) -> tuple[list[MatchState], dict[str, str]]:
    loaded: list[MatchState] = []
    rejected: dict[str, str] = {}
    for mid in log.ids():
        try:
            state, chain = _verified(log, snapshots, mid)
            projection = rows(state, chain)
        except _REJECTABLE as exc:
            rejected[mid] = reason(exc)
            db.reject(run, mid, rejected[mid])
            counts.rejected += 1
            continue
        stored = db.stored(mid)
        if not stored.hashes <= {h for h, _ in chain}:
            raise HistoryRewrittenError(f"{mid}: the results DB has events its log no longer has")
        loaded.append(state)
        current = stored.projection_version == PROJECTION_VERSION and not stored.rejected
        if current and chain[-1][0] in stored.hashes:
            counts.skipped += 1
            continue
        db.ingest(projection)
        counts.inserted += 1
    return loaded, rejected


def _close(db: ResultsStore, run: str, status: str, counts: RunCounts) -> None:
    # Best effort: the original error matters more than a failure to record it (e.g. the connection is gone).
    with suppress(Exception):
        db.finish_run(run, "halted" if status == "halted" else "failed", counts, [], None)


def reason(exc: BaseException) -> str:
    """A stored reject reason: class name and message, scrubbed and capped. Parse errors keep no input text."""
    if isinstance(exc, ValidationError):  # pydantic echoes input_value
        text = f"ValidationError: {exc.error_count()} invalid field(s) in {exc.title}"
    elif isinstance(exc, DuelError | json.JSONDecodeError):  # our own messages; JSON errors give a position only
        text = scrub(f"{type(exc).__name__}: {exc}")
    else:  # KeyError, TypeError, OSError...: their text can be data or a path
        text = type(exc).__name__
    return text if len(text) <= REASON_MAX else text[: REASON_MAX - 1] + "…"


def _digest(states: list[MatchState], boot: int) -> str:
    """What a ratings snapshot depends on; an unchanged digest means an identical snapshot, so none is written."""
    h = hashlib.sha256(f"{ELO_ORDER}|{ELO_K}|{boot}|{PROJECTION_VERSION}".encode())
    for s in sorted(states, key=lambda s: s.match_id):
        h.update(f"\n{s.match_id}:{s.last_seq}:{s.stage.value}".encode())
    return h.hexdigest()


def _verified(log: EventLog, snapshots: MatchStore, mid: str) -> tuple[MatchState, list[tuple[str, Event]]]:
    """Snapshot and the chain up to it, or an error saying why the match can't be loaded."""
    state = snapshots.snapshot(mid)
    chain = [(h, e) for h, e in log.chain(mid) if e.seq <= state.last_seq]
    report = replay(state, [e for _, e in chain])
    if not chain or not report.consistent:
        raise ReplayMismatchError("replay mismatch: " + (", ".join(report.mismatches) or "no events"))
    for _, e in chain:  # logs from before the record-time guard (R3-2) were never checked
        if flag_shaped(json.dumps(e.data)):
            raise FlagLeakError(f"flag-shaped text in event {e.seq} ({e.type})")
    return state, chain


def rows(state: MatchState, chain: list[tuple[str, Event]]) -> MatchProjection:
    """The typed public projection of one verified match. Only allowlisted facts are kept."""
    mid = state.match_id
    prevs = ["0" * 64] + [h for h, _ in chain[:-1]]
    return MatchProjection(
        match=_match_row(state, chain),
        entrants=tuple(_entrant_row(a.provider, a.model, entrant(a)) for a in state.agents),
        agents=tuple(AgentRow(mid, a.name, entrant(a), a.handicap.model_dump()) for a in state.agents),
        result=_result_row(state),
        events=tuple(
            EventRow(h, p, mid, e.seq, e.type, e.v, public_data(e)) for (h, e), p in zip(chain, prevs, strict=True)
        ),
        calls=tuple(_call_row(mid, h, e) for h, e in chain if e.type == ev.CALL_CHARGED),
        attacks=tuple(_attack_row(state, h, e) for h, e in chain if e.type == ev.CHALLENGE_ATTACKED),
        scores=tuple(r for h, e in chain if e.type == ev.ROUND_SCORED for r in _score_rows(mid, h, e)),
    )


def _match_row(state: MatchState, chain: list[tuple[str, Event]]) -> MatchRow:
    r = state.rules
    return MatchRow(
        match_id=state.match_id,
        head_hash=chain[-1][0],
        event_count=len(chain),
        stage=state.stage.value,
        round=state.round,
        mode=r.mode,
        challenge_form=r.challenge_form,
        framing=r.framing.kind,
        rules=r.model_dump(mode="json"),
        limits=state.limits.model_dump(mode="json"),
        failure=None if state.failure is None else public_text(state.failure),
        projection_version=PROJECTION_VERSION,
    )


def _entrant_row(provider: str, model: str | None, entrant_id: str) -> EntrantRow:
    return EntrantRow(entrant_id, provider, model or "default", provider == "fake")


def _result_row(state: MatchState) -> ResultRow:
    u = state.usage
    winner = game_winner(state) if state.stage is Stage.COMPLETE else None
    return ResultRow(state.match_id, winner, state.final_scores(), u.calls, u.tokens, round(u.usd, 6))


def _call_row(mid: str, h: str, e: Event) -> CallRow:
    p = ev.CallCharged.model_validate(e.data)
    return CallRow(h, mid, p.agent, p.model, p.tokens, p.input_tokens, p.output_tokens, p.usd, p.elapsed_ms)


def _attack_row(state: MatchState, h: str, e: Event) -> AttackRow:
    a = ev.ChallengeAttacked.model_validate(e.data).attack
    c = state.challenges[a.challenge_id]
    return AttackRow(
        h,
        state.match_id,
        a.challenge_id,
        c.round,
        c.author,
        a.attacker,
        a.solved,
        a.attempts,
        a.tokens_used,
        a.usd_micros,
    )


def _score_rows(mid: str, h: str, e: Event) -> list[ScoreRow]:
    p = ev.RoundScored.model_validate(e.data)
    return [ScoreRow(h, mid, p.round, n, v) for n, v in sorted(p.delta.items())]


def _rating_rows(states: list[MatchState], boot: int) -> list[RatingRow]:
    rs = results(states)
    last = rs[-1].match_id if rs else ""
    return [
        RatingRow(r.entrant, r.games, r.elo, r.bt, r.lo, r.hi, ELO_ORDER, ELO_K, last) for r in ratings(rs, boot=boot)
    ]
