"""``duel replay --verify``: re-run every logged ghost run in the sandbox and check its hit and stdout hash.

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
    bad: list[str] = []
    for e in (e for e in events if e.type == ev.GHOST_RUN):
        rnd, gid, cid = int(str(e.data["round"])), str(e.data["ghost_id"]), str(e.data["target"])
        ghost = {g.id: g for g in ghosts.ghosts_before(state, rnd)}.get(gid)
        ch = state.challenges.get(cid)
        if ghost is None or ch is None or ch.spec is None:
            bad.append(f"ghost run #{e.seq}: unknown ghost or target")
            continue
        flag = flag_of(state.secret, cid, ch.flag_epoch)
        run = sandbox.run(ch.spec.source, ghost.payload, flag, state.limits)
        sha = hashlib.sha256(redact(run.stdout, (flag,)).encode()).hexdigest()
        if (flag in run.stdout) != e.data["hit"] or sha != e.data["stdout_sha"]:
            bad.append(f"ghost run #{e.seq}: {gid} -> {cid} does not reproduce")
    return bad
