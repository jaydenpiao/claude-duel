"""``duel replay --verify``: recompute each scored round's ghost schedule, then re-run every logged ghost run in
the sandbox and check its hit and stdout hash. A planned run missing from the log, an unplanned run, or a run that
does not reproduce is reported as tampered.

$0 API: ghost runs make no model calls, so this only spends sandbox time.
"""

import hashlib

from claude_duel.domain import events as ev
from claude_duel.domain import ghosts
from claude_duel.domain.flags import flag_of, redact
from claude_duel.domain.models import Event, MatchState
from claude_duel.ports import Sandbox


def verify_ghosts(state: MatchState, events: list[Event], sandbox: Sandbox) -> list[str]:
    """Mismatches between each logged ghost run and a fresh sandbox run of the same payload and target."""
    bad = _schedule_gaps(state, events)
    for e in (e for e in events if e.type == ev.GHOST_RUN):
        rnd, gid, cid = int(str(e.data["round"])), str(e.data["ghost_id"]), str(e.data["target"])
        ghost = {g.id: g for g in ghosts.ghosts_before(state, rnd)}.get(gid)
        ch = state.challenges.get(cid)
        if ghost is None or ch is None or ch.spec is None:
            bad.append(f"ghost run #{e.seq}: tampered: unknown ghost or target")
            continue
        flag = flag_of(state.secret, cid, ch.flag_epoch)
        run = sandbox.run(ch.spec.source, ghost.payload, flag, state.limits)
        sha = hashlib.sha256(redact(run.stdout, (flag,)).encode()).hexdigest()
        if (flag in run.stdout) != e.data["hit"] or sha != e.data["stdout_sha"]:
            bad.append(f"ghost run #{e.seq}: tampered: {gid} -> {cid} does not reproduce")
    return bad


def _planned_keys(state: MatchState, logged: dict[str, bool]) -> tuple[set[str], list[str]]:
    """Replan each scored round as the engine does; report planned keys and missing logged runs."""
    deep = state.rules.deep
    assert deep is not None
    assert deep.ghosts
    planned: set[str] = set()
    bad: list[str] = []
    for rnd in sorted(state.scored_rounds):
        valid = [
            c
            for c in state.round_challenges(rnd)
            if c.spec and (v := state.validations.get(c.id)) is not None and v.valid
        ]
        plan = ghosts.ghost_schedule(state.seed, rnd, ghosts.ghosts_before(state, rnd), valid, deep.max_ghost_runs)
        hit: set[str] = set()
        for ghost, ch in plan:
            key = ghosts.run_key(rnd, ghost.id, ch.id)
            planned.add(key)
            if ch.id in hit:
                continue  # the engine skips a target an earlier pair already hit
            if key not in logged:
                bad.append(f"ghost run {key}: tampered: planned run missing from the log")
            elif logged[key]:
                hit.add(ch.id)
    return planned, bad


def _unplanned(logged: dict[str, bool], planned: set[str], scored: set[int]) -> list[str]:
    """Logged ghost runs that are not in the recomputed schedule for a scored round."""
    return [
        f"ghost run {k}: tampered: not in the recomputed schedule"
        for k in sorted(logged)
        if k not in planned and int(k.split(":", 1)[0]) in scored
    ]


def _schedule_gaps(state: MatchState, events: list[Event]) -> list[str]:
    """Replan each scored round as the engine does and compare it with the logged ghost runs."""
    deep = state.rules.deep
    if deep is None or not deep.ghosts:
        return []
    logged = {
        ghosts.run_key(int(str(e.data["round"])), str(e.data["ghost_id"]), str(e.data["target"])): bool(e.data["hit"])
        for e in events
        if e.type == ev.GHOST_RUN
    }
    planned, bad = _planned_keys(state, logged)
    return bad + _unplanned(logged, planned, set(state.scored_rounds))
