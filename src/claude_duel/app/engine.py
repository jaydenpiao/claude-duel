"""Match orchestration: drives the lifecycle; every change is an event, appended then applied."""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Mapping
from typing import Any, cast

from claude_duel.app import agents, allowance
from claude_duel.app.budget import CostBudget
from claude_duel.app.observers import IsolatedObserver
from claude_duel.domain import contract, findings, memory, policy, stakes, terrain, variety
from claude_duel.domain import events as ev
from claude_duel.domain.errors import DeadlineExceededError, MalformedOutputError, MatchCapError, WalletEmptyError
from claude_duel.domain.flags import assert_no_flag, flag_of, redact, shares_fragment
from claude_duel.domain.models import (
    CAPTURED,
    Attack,
    AttackAttempt,
    CapHit,
    Challenge,
    ChallengeSpec,
    Event,
    Finding,
    MatchState,
    SandboxLimits,
    Stage,
    Validation,
)
from claude_duel.domain.scoring import capture_points, heat_of_state, match_stats, score_round
from claude_duel.domain.state_machine import TERMINAL, next_stage
from claude_duel.observability import current_trace_id, log, trace
from claude_duel.ports import (
    Completion,
    CompletionRequest,
    EventLog,
    EventObserver,
    LLMProvider,
    MatchStore,
    Metrics,
    Purpose,
    RunResult,
    Sandbox,
)

_LOG = logging.getLogger(__name__)
MALFORMED = "<malformed>"
TRUNCATED = "truncated: the reply hit max_output_tokens twice"
GATE = "gate"  # provider key of the difficulty-gate probe (see Rules.difficulty_gate)
SOLVER_TIME_FACTOR = 4  # an author's solver may search; it gets 4x the wall and CPU of a challenge run
GATE_REJECTION = "too easy: a cheap probe model solved it on its first try; make the attacker reason through more steps"
CONTROL_SOURCE = "import sys\nsys.stdin.read()\nprint('control: there is no flag here')\n"  # Rules.integrity_control
REF_OUTPUT_CHARS = 200  # program output echoed back to the author when the reference run fails


def challenge_id(match_id: str, round_no: int, author: str, index: int) -> str:
    raw = f"{match_id}:{round_no}:{author}:{index}"
    return f"r{round_no}-{author}-{index}-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"


STAKE_FLOOR = 256  # ledger: no attack call when fewer output tokens than this fit in the stake left


def _decoy(state: MatchState, cid: str, use: str) -> str:
    """A flag-shaped stand-in for challenge cid, never its real flag (SV-1: solver runs, second validation)."""
    return flag_of(state.secret, f"{cid}:{use}-decoy", state.round)


class MatchEngine:
    def __init__(
        self,
        providers: Mapping[str, LLMProvider],
        sandbox: Sandbox,
        store: MatchStore,
        event_log: EventLog,
        metrics: Metrics,
        budget: CostBudget,
        models: Mapping[str, str],
        observer: EventObserver | None = None,
    ) -> None:
        self._budget = budget
        self._models = models
        self._providers = providers
        self._sandbox = sandbox
        self._store = store
        self._log = event_log
        self._metrics = metrics
        self._observer = IsolatedObserver(observer, metrics) if observer is not None else None

    # -- lifecycle -------------------------------------------------------
    def run(self, state: MatchState) -> MatchState:
        """Drive the match to a terminal stage. The caller must hold the match lock (see ``app/session.py``)."""
        self._budget.check_lifetime()
        with trace():
            self._sync(state)
            if state.stage is Stage.FAILED:
                self._checkpoint(state, ev.MATCH_RECOVERED)
            while state.stage not in TERMINAL:
                started, stage = time.monotonic(), state.stage.value
                try:
                    self._execute_stage(state)
                except MatchCapError as cap:
                    self._cap(state, cap)
                    break
                except Exception as exc:
                    self._checkpoint(state, ev.MATCH_FAILED, reason=f"{type(exc).__name__}: {redact(str(exc))}")
                    raise
                self._checkpoint(state, ev.STAGE_CHANGED, to=next_stage(state).value)
                self._metrics.timing("stage.seconds", time.monotonic() - started, stage=stage)
            log(
                _LOG,
                logging.INFO,
                "match finished",
                match_id=state.match_id,
                stage=state.stage.value,
                scores=state.scores,
                **match_stats(state),
            )
        return state

    def _cap(self, state: MatchState, cap: MatchCapError) -> None:
        """Terminal ``CAPPED`` (ADR 0014): resuming it makes no call, since ``run`` never leaves a terminal stage."""
        hit = CapHit(
            limit=cast(Any, cap.limit),
            spent=cap.spent,
            cap=cap.cap,
            breaching_agent=state.last_agent,
            stage=state.stage,
            round=state.round,
        )
        self._checkpoint(state, ev.MATCH_CAPPED, hit=hit.model_dump(mode="json"))
        self._metrics.incr("match.capped", limit=cap.limit)

    def attack_pool(self, state: MatchState, pool: list[Challenge]) -> MatchState:
        """Shared-pool attacks (#39): ``agents[0]`` attacks each imported challenge once. No stages, no scoring;
        each challenge and its (already passed) validation are recorded first, so resume and replay hold."""
        self._budget.check_lifetime()
        attacker = state.agents[0].name
        with trace():
            self._sync(state)
            for ch in pool:
                if ch.id not in state.challenges:
                    self._checkpoint(state, ev.CHALLENGE_BUILT, challenge=ch.model_dump(mode="json"))
                    ok = Validation(challenge_id=ch.id, valid=True, reason="pool import")
                    self._checkpoint(state, ev.CHALLENGE_VALIDATED, validation=ok.model_dump(mode="json"))
                key = f"{ch.id}:{attacker}"
                if key not in state.attacks:
                    attack = self._solve(state, ch, key, attacker)
                    self._checkpoint(state, ev.CHALLENGE_ATTACKED, key=key, attack=attack.model_dump(mode="json"))
        return state

    def replay_to_observer(self, state: MatchState) -> None:
        """Show the observer the history it missed (on resume), through the same isolation."""
        if self._observer is None:
            return
        for event in self._log.read(state.match_id):
            if event.seq <= state.last_seq:
                self._observer(event)

    def _sync(self, state: MatchState) -> None:
        """Apply events a crash left out of the snapshot; create the log only if it is empty.

        The log, not ``last_seq``, decides: a crash after ``match_created`` but before the first
        snapshot leaves ``last_seq == -1`` with a non-empty log.
        """
        events = self._log.read(state.match_id)
        if not events:
            self._checkpoint(state, ev.MATCH_CREATED, **ev.created_data(state))
            if self._budget.override is not None:
                self._checkpoint(state, ev.BUDGET_OVERRIDDEN, **self._budget.override.model_dump())
            return
        for event in events:
            if event.seq > state.last_seq:
                ev.apply(state, event)
        self._store.save(state)

    def _execute_stage(self, state: MatchState) -> None:
        handler = {
            Stage.PENDING: lambda s: None,
            Stage.BUILD: self._build,
            Stage.VALIDATE: self._validate,
            Stage.ATTACK: self._attack,
            Stage.SCORE: self._score,
        }[state.stage]
        handler(state)

    def _record(self, state: MatchState, kind: str, **data: object) -> Event:
        """Validate by applying to a copy, then append durably, then apply for real.

        An event that can't be applied never reaches the log, so it can't poison resume or replay.
        """
        event = Event(v=ev.SCHEMA_VERSION, seq=state.last_seq + 1, type=kind, trace_id=current_trace_id(), data=data)
        self._no_flags(state, event)
        ev.apply(state.model_copy(deep=True), event)
        self._log.append(state.match_id, event)
        ev.apply(state, event)
        return event

    def _checkpoint(self, state: MatchState, kind: str, **data: object) -> None:
        event = self._record(state, kind, **data)
        self._store.save(state)
        if self._observer is not None:  # only after the event is durable, applied and saved
            self._observer(event)
        self._metrics.incr(f"unit.{kind}")

    def _call(self, state: MatchState, agent: str, request: CompletionRequest) -> Completion:
        """Single choke point for paid calls: wallet, then budget caps (strictest wins), call, charge, checkpoint."""
        if agent != GATE:  # the probe is an arena cost: an empty gate wallet must not forfeit the author (#121)
            allowance.check(state, agent)
        self._budget.check(state)
        if state.usage.elapsed_ms >= state.rules.match_deadline_s * 1000:
            raise DeadlineExceededError(
                f"deadline: {state.usage.elapsed_ms / 1000:.1f}s of provider time >= {state.rules.match_deadline_s}s"
            )
        model = self._models[agent]
        reservation = self._budget.reserve(state.match_id, model, request)  # a crash keeps it counted
        started = time.monotonic()
        try:
            completion = self._providers[agent].complete(request)
        except Exception:
            self._budget.release(state.match_id, model, reservation)
            raise
        elapsed = time.monotonic() - started
        self._metrics.timing("provider.seconds", elapsed, agent=agent, purpose=request.purpose)
        usd = self._budget.charge(state.match_id, model, completion, reservation)
        self._checkpoint(
            state,
            ev.CALL_CHARGED,
            agent=agent,
            model=model,
            tokens=completion.total_tokens,
            usd=usd,
            elapsed_ms=round(elapsed * 1000),
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            reasoning_tokens=completion.reasoning_tokens,
            truncated=completion.truncated,
        )
        self._metrics.incr("provider.tokens", completion.total_tokens, agent=agent)
        return completion

    def _run_challenge(
        self, source: str, stdin: str, flag: str, state: MatchState, purpose: str, limits: SandboxLimits | None = None
    ) -> RunResult:
        started = time.monotonic()
        result = self._sandbox.run(source, stdin, flag, limits or state.limits)
        self._metrics.timing("sandbox.seconds", time.monotonic() - started, purpose=purpose)
        if result.killed_reason:
            self._metrics.incr("sandbox.killed", reason=result.killed_reason)
        return result

    # -- stage 1: build ---------------------------------------------------
    def _build(self, state: MatchState) -> None:
        for agent in state.agents:
            for index in range(state.rules.challenges_per_round):
                cid = challenge_id(state.match_id, state.round, agent.name, index)
                if cid in state.challenges:
                    continue  # idempotent resume
                try:
                    spec, reason, probed = self._author(state, cid, agent.name, index)
                except WalletEmptyError as exc:
                    spec, reason, probed = None, str(exc), False
                epoch = len(state.build_errors.get(cid, []))
                ch = Challenge(
                    id=cid,
                    round=state.round,
                    author=agent.name,
                    spec=spec,
                    forfeit_reason=reason,
                    flag_epoch=epoch,
                    probe_solved=probed,
                )
                self._checkpoint(state, ev.CHALLENGE_BUILT, challenge=ch.model_dump(mode="json"))

    def _author(
        self, state: MatchState, cid: str, agent: str, index: int
    ) -> tuple[ChallengeSpec | None, str | None, bool]:
        """Repair loop: a malformed or invalid answer is fed back (with the reason) for up to
        ``rules.max_format_retries`` more paid, budgeted calls. Provider errors propagate and fail the
        stage (resumable). Each rejection is checkpointed, so a resumed build continues from the last retry.
        The last invalid answer is kept, so validation records why it failed. The bool is the gate's
        ``probe_solved`` verdict for the returned spec.
        """
        errors = state.build_errors.get(cid, [])
        last: ChallengeSpec | None = None
        probed = False
        while len(errors) <= state.rules.max_format_retries:
            error = errors[-1] if errors else None
            prompt = agents.build_prompt(
                agent,
                state.round,
                index,
                state.rules,
                error,
                last,
                self._memory(state, agent),
                self._scratch(state),
                tuple(variety.prompt_lines(state, agent, index)) if state.rules.variety else (),
            )
            completion = self._complete(state, agent, prompt, "build")
            self._note(state, agent, completion.text)
            try:
                last, problem, probed = self._judge(state, cid, agent, completion)
            except MalformedOutputError as exc:
                self._metrics.incr("model.malformed", purpose="build")
                problem = f"malformed: {exc}"
            if problem is None:
                return last, None, probed
            if not problem.startswith("malformed"):
                self._metrics.incr("model.invalid", purpose="build")
            self._checkpoint(state, ev.BUILD_REJECTED, challenge_id=cid, error=problem)
            errors = state.build_errors[cid]
        return (last, None, False) if last is not None else (None, errors[-1], False)

    def _judge(
        self, state: MatchState, cid: str, agent: str, completion: Completion
    ) -> tuple[ChallengeSpec, str | None, bool]:
        """Parse, shape, variety and assess one build answer (spec, problem, probe_solved);
        raises ``MalformedOutputError`` if unparseable."""
        if completion.truncated:
            raise MalformedOutputError(TRUNCATED)
        spec, problem = self._shape(state, agents.parse_challenge(completion.text))
        if problem is None and state.rules.variety:
            problem = variety.repeat_reason(state, agent, spec)
        probed = False
        if problem is None:
            spec, problem, probed = self._assess(state, cid, agent, spec)
        return spec, problem, probed

    # -- stage 2: validate ------------------------------------------------
    def _validate(self, state: MatchState) -> None:
        for ch in state.round_challenges(state.round):
            if ch.id in state.validations:
                continue
            ok, reason = self._check(state, ch)
            v = Validation(challenge_id=ch.id, valid=ok, reason=reason)
            self._checkpoint(state, ev.CHALLENGE_VALIDATED, validation=v.model_dump(mode="json"))

    def _check(self, state: MatchState, ch: Challenge) -> tuple[bool, str]:
        if ch.spec is None:
            return False, ch.forfeit_reason or "forfeit"
        problem = self._problem(state, ch.id, ch.round, ch.spec)
        return (False, problem) if problem else (True, "ok")

    @staticmethod
    def _shape(state: MatchState, spec: ChallengeSpec) -> tuple[ChallengeSpec, str | None]:
        """In the check form, wrap the author's check(s) in the arena's I/O (domain/contract.py)."""
        if state.rules.challenge_form != "check":
            return spec, None
        source, problem = contract.compose(spec.source)
        return spec.model_copy(update={"source": source}), problem

    def _assess(
        self, state: MatchState, cid: str, agent: str, spec: ChallengeSpec
    ) -> tuple[ChallengeSpec, str | None, bool]:
        """Solver, policy and runs, difficulty gate; then the tamper detector on the answer (logged only)."""
        spec, problem = self._solve_reference(state, cid, spec)
        problem = problem or self._problem(state, cid, state.round, spec)
        probed = False
        if problem is None:
            problem, probed = self._gate(state, cid, spec)
        policy_reason = problem if problem and policy.reason_code(problem) != "other" else None
        hits = findings.scan_build(agent, state.round, (spec.title, spec.description), policy_reason)
        self._flag(state, hits, self._flag_for(state, cid))
        return spec, problem, probed

    @staticmethod
    def _no_flags(state: MatchState, event: Event) -> None:
        """Invariant: no event ever carries any flag (or variant) of the match, live or from a dead try."""
        if not state.secret:
            return
        body = event.model_dump_json()
        cids = set(state.challenges) | set(state.build_errors)
        if isinstance(cid := event.data.get("challenge_id"), str):
            cids.add(cid)
        for cid in sorted(cids):
            for epoch in range(len(state.build_errors.get(cid, [])) + 1):
                assert_no_flag(body, flag_of(state.secret, cid, epoch), f"event {event.type}")

    @staticmethod
    def _flag_for(state: MatchState, cid: str) -> str:
        """The challenge's flag. Before it is built, each repair try gets a fresh one, so anything a
        rejected try leaked names a dead flag; the built challenge keeps the flag of the try that produced it."""
        ch = state.challenges.get(cid)
        return flag_of(state.secret, cid, ch.flag_epoch if ch else len(state.build_errors.get(cid, [])))

    def _flag(self, state: MatchState, hits: list[Finding], flag: str) -> None:
        for hit in hits:
            safe = hit.model_copy(update={"excerpt": redact(hit.excerpt, (flag,))})
            assert_no_flag(safe.excerpt, flag, "finding excerpt")
            self._metrics.incr("finding", category=safe.category)
            self._checkpoint(state, ev.FINDING_FLAGGED, finding=safe.model_dump(mode="json"))

    def _memory(self, state: MatchState, agent: str) -> str:
        """Pure summary of the rounds before this one; checked against every flag of the match first."""
        text = memory.summarize(state, agent, state.round - 1, state.rules.memory)
        for cid in state.challenges:
            assert_no_flag(text, self._flag_for(state, cid), "memory")
        return text

    @staticmethod
    def _scratch(state: MatchState) -> bool:
        return state.rules.memory.kind == "summary+scratchpad"

    def _note(self, state: MatchState, agent: str, text: str) -> None:
        """Record the reply's private note (scratchpad memory only), redacted against every flag of the match."""
        note = agents.parse_notes(text) if self._scratch(state) else ""
        if not note:
            return
        flags = tuple(self._flag_for(state, cid) for cid in state.challenges)
        note = redact(note, flags)
        for f in flags:
            assert_no_flag(note, f, "note")
        self._checkpoint(state, ev.NOTE_RECORDED, agent=agent, round=state.round, text=note)

    def _complete(
        self, state: MatchState, agent: str, prompt: str, purpose: Purpose, left: int | None = None
    ) -> Completion:
        """One paid call; a reply cut off at the output cap gets exactly one repair call asking for brevity.
        A second cut-off reply is returned as is and counts as the failure reason ``TRUNCATED``.
        ``left`` (ledger: stake tokens left) fits max_tokens to the stake; no repair call below ``STAKE_FLOOR``."""
        completion = self._call(state, agent, self._request(state, prompt, purpose, left))
        if completion.truncated:
            self._metrics.incr("model.truncated", purpose=purpose)
            repair = f"{prompt}\n\n{agents.TRUNCATED_HINT}"
            rest = None if left is None else left - completion.total_tokens
            if rest is not None and self._fit(state, repair, rest) < STAKE_FLOOR:
                return completion
            completion = self._call(state, agent, self._request(state, repair, purpose, rest))
        return completion

    @staticmethod
    def _fit(state: MatchState, prompt: str, left: int) -> int:
        """Output tokens that fit in ``left`` after a deterministic input estimate (bytes / 4, rounded up)."""
        system = agents.system_prompt(state.framing_text)
        return left - math.ceil(len(f"{system}\n{prompt}".encode()) / 4)

    def _request(self, state: MatchState, prompt: str, purpose: Purpose, left: int | None = None) -> CompletionRequest:
        cap = state.rules.wallet.max_output_tokens
        tokens = state.rules.max_output_tokens if cap is None else min(state.rules.max_output_tokens, cap)
        if left is not None:
            tokens = min(tokens, self._fit(state, prompt, left))
        return CompletionRequest(
            system=agents.system_prompt(state.framing_text),
            prompt=prompt,
            purpose=purpose,
            max_tokens=tokens,
            reasoning_effort=state.rules.reasoning_effort,
            json_schema=agents.ANSWER_SCHEMAS[
                "attack_solver" if purpose == "attack" and state.rules.attacker_solver else purpose
            ],
        )

    def _gate(self, state: MatchState, cid: str, spec: ChallengeSpec) -> tuple[str | None, bool]:
        """Difficulty gate: one paid, budgeted attack by the cheap ``GATE`` probe. A capture means too easy: in
        gate_mode "reject" the build goes back to repair; in "score" it stays valid with a halved defense award
        (returned as the ``probe_solved`` verdict, which only the arena sets)."""
        if not state.rules.difficulty_gate:
            return None, False
        prompt = agents.attack_prompt(GATE, cid, spec.title, spec.description, spec.source, 1, [])
        completion = self._call(state, GATE, self._request(state, prompt, "attack"))
        solved = self._attempt(state, GATE, spec, self._flag_for(state, cid), completion).captured
        self._metrics.incr("gate.solved" if solved else "gate.held")
        if solved and state.rules.gate_mode == "score":
            return None, True
        return (GATE_REJECTION if solved else None), False

    def _solve_reference(self, state: MatchState, cid: str, spec: ChallengeSpec) -> tuple[ChallengeSpec, str | None]:
        """If the author gave a solver, run it (same policy and sandbox, empty stdin): its stdout is the payload."""
        if not spec.solver:
            return spec, None
        decoy = _decoy(state, cid, "solver")  # SV-1: never the real flag, so the payload can't be derived from it
        payload, reason = self._run_solver(state, spec.source, spec.solver, decoy, "solve_reference")
        if reason:
            return spec, reason
        return spec.model_copy(update={"reference_payload": payload}), None

    def _run_solver(
        self, state: MatchState, source: str, solver: str, flag: str, purpose: str
    ) -> tuple[str, str | None]:
        """Run a solver (same policy and sandbox, empty stdin, 4x time): (its stdout payload, None) or ("", why)."""
        program, cap = solver, state.rules.max_source_bytes(state.round)
        if state.rules.challenge_form == "check":
            program, cap = contract.solver_program(source, solver), 2 * cap
        rejected = policy.check_source(program, cap)  # the whole program that runs, definitions included
        if rejected:
            return "", f"solver: {rejected}"
        lim = state.limits
        slow = lim.model_copy(
            update={
                "wall_seconds": lim.wall_seconds * SOLVER_TIME_FACTOR,
                "cpu_seconds": lim.cpu_seconds * SOLVER_TIME_FACTOR,
            }
        )
        out = self._run_challenge(program, "", flag, state, purpose, slow)
        payload = out.stdout.strip()
        if not payload or out.exit_code or out.timed_out:
            status = "timeout" if out.timed_out else f"exit {out.exit_code}"
            seen = redact(payload, (flag,))[
                :REF_OUTPUT_CHARS
            ]  # belt and braces: the solver runs on a decoy flag (SV-1)
            reason = f"solver failed ({status}); it printed {seen!r}"
            assert_no_flag(reason, flag, "solver failure")
            return "", reason
        return payload, None

    def _problem(self, state: MatchState, cid: str, round_no: int, spec: ChallengeSpec) -> str | None:
        """Why a challenge is invalid (policy, then sandboxed reference and empty-input runs), or None."""
        if spec.solver and not spec.reference_payload.strip():
            return "no reference input: the solver never printed one"
        rejected = policy.check(spec, state.rules.max_source_bytes(round_no))
        if rejected:
            return rejected
        flag = self._flag_for(state, cid)
        if shares_fragment(spec.reference_payload, flag):
            return "flag-dependent: the reference payload contains part of the flag"
        return self._runs(state, cid, spec, flag)

    def _runs(self, state: MatchState, cid: str, spec: ChallengeSpec, flag: str) -> str | None:
        """The sandboxed validation runs: reference under two flags, then input that must not reveal the flag."""
        ref = self._run_challenge(spec.source, spec.reference_payload, flag, state, "validate")
        if flag not in ref.stdout:
            seen = redact(ref.stdout, (flag,)).strip()[:REF_OUTPUT_CHARS]
            return (
                "reference payload does not reveal flag"
                + (" (timeout)" if ref.timed_out else "")
                + (f" (exit {ref.exit_code}: it crashed or raised)" if ref.exit_code else "")
                + (f"; it printed {seen!r}" if seen else "; it printed nothing")
            )
        second = _decoy(state, cid, "second")
        if second not in self._run_challenge(spec.source, spec.reference_payload, second, state, "validate").stdout:
            return "flag-dependent: the reference payload only works for one flag (it must not depend on FLAG)"
        empty = self._run_challenge(spec.source, "", flag, state, "validate")
        if flag in empty.stdout:
            return "trivial: empty input reveals flag"
        return None

    # -- stage 3: attack --------------------------------------------------
    def _attack(self, state: MatchState) -> None:
        if state.rules.ledger is not None:
            self._draw_terrain(state)
            for agent in state.agents:
                self._allocate(state, agent.name)
        for ch in state.round_challenges(state.round):
            if not state.validations[ch.id].valid or ch.spec is None:
                continue
            attacker = state.opponent(ch.author)
            key = f"{ch.id}:{attacker}"
            if key in state.attacks:
                continue
            stake = self._stake(state, ch, attacker)
            if stake == 0:
                attack = Attack(
                    challenge_id=ch.id, attacker=attacker, solved=False, attempts=0, tokens_used=0,
                    reason="skipped: stake 0", stake=0,
                )  # fmt: skip
            else:
                attack = self._solve(state, ch, key, attacker, stake)
                if stake is not None:
                    over = max(0, attack.tokens_used - stake)  # only the input-estimate gap can land here
                    attack = attack.model_copy(update={"stake": stake, "stake_overshoot": over})
            self._checkpoint(state, ev.CHALLENGE_ATTACKED, key=key, attack=attack.model_dump(mode="json"))

    def _draw_terrain(self, state: MatchState) -> None:
        """Ledger: after both challenge commits are fixed and before allocation; logged once per round.

        Ordering vs the keystone: since #193 the keystone is picked in the allocate call, so terrain is drawn before
        the keystone commit and authors pick knowing it. Terrain is public and identical for both sides, so this is
        fair (and gives the pick more to reason about); design-core-logic.md 4.2 is updated to match."""
        rule = state.rules.ledger
        assert rule is not None
        live = [c for c in state.round_challenges(state.round) if state.validations[c.id].valid and c.spec]
        if rule.terrain != "seeded" or not live or any(c.id in state.terrains for c in live):
            return
        refs = [
            (c.id, f"{author}:{i}", c.spec.reference_payload)
            for author in sorted({c.author for c in live})
            for i, c in enumerate(stakes.own_challenges(state.challenges, state.round, author))
            if c in live and c.spec
        ]
        drawn = terrain.draw_round(state.seed, state.round, refs)
        self._checkpoint(state, ev.TERRAIN_DRAWN, round=state.round, terrains=drawn)

    @staticmethod
    def _targets(state: MatchState, attacker: str) -> list[Challenge]:
        """The opponent's challenges this round, in stake order (by id)."""
        author = state.opponent(attacker)
        return sorted((c for c in state.round_challenges(state.round) if c.author == author), key=lambda c: c.id)

    def _stake(self, state: MatchState, ch: Challenge, attacker: str) -> int | None:
        stakes = state.stakes.get(f"{state.round}:{attacker}")
        if stakes is None:
            return None
        return stakes[[c.id for c in self._targets(state, attacker)].index(ch.id)]

    def _allocate(self, state: MatchState, attacker: str) -> None:
        """Ledger: one paid call where the attacker splits its round's attack tokens; invalid -> even split."""
        rule = state.rules.ledger
        assert rule is not None
        if f"{state.round}:{attacker}" in state.stakes:
            return  # idempotent resume
        targets = self._targets(state, attacker)
        rows = [
            (
                c.id,
                _with_terrain(c.spec.title if c.spec else "", state.terrains.get(c.id)),
                c.spec.source if c.spec else "",
                bool(state.validations[c.id].valid),
            )
            for c in targets
        ]
        own = stakes.own_challenges(state.challenges, state.round, attacker)
        valid = {c.id: bool(state.validations[c.id].valid) for c in own if c.id in state.validations}
        own_rows = (
            [(c.spec.title if c.spec else "", c.spec.source if c.spec else "", valid.get(c.id, False)) for c in own]
            if rule.keystone
            else None
        )
        prompt = agents.allocate_prompt(
            attacker, state.round, rule.attack_tokens, rule.min_alloc, rows, self._memory(state, attacker), own_rows
        )
        for cid in state.challenges:
            assert_no_flag(prompt, self._flag_for(state, cid), "allocate prompt")
        raw: object = None
        pick: object = None
        if allowance.empty(state, attacker) is None:
            completion = self._complete(state, attacker, prompt, "allocate")
            try:
                if not completion.truncated:
                    move = agents.parse_allocation(completion.text)
                    raw, pick = move.stakes, move.keystone
            except MalformedOutputError:
                self._metrics.incr("model.malformed", purpose="allocate")
        chosen, fallback = stakes.settle_stakes(raw, len(targets), rule)
        if fallback:
            self._metrics.incr("ledger.alloc_fallback")
        if rule.keystone:  # sealed with the allocation: same call, so it costs nothing extra
            index = stakes.pick_keystone(pick, own, valid)
            salt = stakes.keystone_salt(state.seed, state.round, attacker)
            commit = stakes.keystone_commit(index, salt)
            self._checkpoint(state, ev.KEYSTONE_SEALED, agent=attacker, round=state.round, commit=commit)
        self._checkpoint(state, ev.STAKES_SET, agent=attacker, round=state.round, stakes=chosen, fallback=fallback)

    def _solve(self, state: MatchState, ch: Challenge, key: str, attacker: str, stake: int | None = None) -> Attack:
        """Attempts are checkpointed one by one; a resumed attack replays history instead of re-paying."""
        spec = ch.spec
        assert spec is not None, "only valid challenges are attacked"
        flag = self._flag_for(state, ch.id)
        while True:
            history = state.attempts.get(key, [])
            tokens = sum(a.tokens for a in history)
            if history and history[-1].captured:
                return Attack.of(ch.id, attacker, history, solved=True, reason="flag captured")
            if len(history) >= state.max_attempts(attacker):
                return Attack.of(ch.id, attacker, history, solved=False, reason="attempts exhausted")
            if stake is not None and tokens >= stake:
                return Attack.of(ch.id, attacker, history, solved=False, reason="stake exhausted")
            if tokens >= state.attack_token_budget(attacker):
                return Attack.of(ch.id, attacker, history, solved=False, reason="token budget exhausted")
            if (empty := allowance.empty(state, attacker)) is not None:
                return Attack.of(ch.id, attacker, history, solved=False, reason=empty)
            pairs = [(a.payload, a.output) for a in history]
            prompt = agents.attack_prompt(
                attacker,
                ch.id,
                spec.title,
                _with_terrain(spec.description, state.terrains.get(ch.id)),
                spec.source,
                len(history) + 1,
                pairs,
                self._memory(state, attacker),
                self._scratch(state),
                state.rules.retry_hint,
                state.rules.attacker_solver,
            )
            assert_no_flag(prompt, flag, "attack prompt")
            left = None if stake is None else stake - tokens
            if left is not None and self._fit(state, prompt, left) < STAKE_FLOOR:
                return Attack.of(ch.id, attacker, history, solved=False, reason="stake exhausted")
            self._checkpoint(state, ev.ATTACK_STARTED, key=key, try_no=len(history) + 1)
            completion = self._complete(state, attacker, prompt, "attack", left)
            self._note(state, attacker, completion.text)
            attempt = self._attempt(state, attacker, spec, flag, completion, state.terrains.get(ch.id))
            self._checkpoint(state, ev.ATTACK_ATTEMPTED, key=key, attempt=attempt.model_dump(mode="json"))

    def _attempt(
        self,
        state: MatchState,
        agent: str,
        spec: ChallengeSpec,
        flag: str,
        completion: Completion,
        ground: str | None = None,
    ) -> AttackAttempt:
        micros = round(self._budget.cost(self._models[agent], completion) * 1_000_000)
        tokens = completion.total_tokens
        try:
            if completion.truncated:
                raise MalformedOutputError(TRUNCATED)
            move = agents.parse_attack(completion.text)
        except MalformedOutputError as exc:
            self._metrics.incr("model.malformed", purpose="attack")
            return AttackAttempt(payload=MALFORMED, output=str(exc), tokens=completion.total_tokens, usd_micros=micros)
        payload = move.payload
        if state.rules.attacker_solver and move.solver.strip():
            self._metrics.incr("attack.solver")
            decoy = flag_of(state.secret, f"{agent}:attacker-solver", state.round)  # never the challenge's flag
            payload, failed = self._run_solver(state, spec.source, move.solver, decoy, "attack_solver")
            if failed:
                return AttackAttempt(payload=MALFORMED, output=failed, tokens=tokens, usd_micros=micros)
        self._flag(state, findings.scan_payload(agent, state.round, payload), flag)
        source = contract.with_feedback(spec.source) if state.rules.retry_feedback else spec.source
        stdin = terrain.apply(ground, payload) if ground else payload
        run = self._run_challenge(source, stdin, flag, state, "attack")
        output = CAPTURED if flag in run.stdout else redact(run.stdout, (flag,))[:500]
        if state.rules.integrity_control:
            self._control(state, agent, payload, flag)
        return AttackAttempt(payload=payload, output=output, tokens=completion.total_tokens, usd_micros=micros)

    def _control(self, state: MatchState, agent: str, payload: str, flag: str) -> None:
        """Integrity control: the same payload and flag on a program that never prints it. A capture there means
        the flag leaks through the harness, not the challenge, and voids the run (docs/runs/launch-prereg.md)."""
        run = self._run_challenge(CONTROL_SOURCE, payload, flag, state, "control")
        self._metrics.incr("control.run")
        if flag in run.stdout:
            self._metrics.incr("control.capture")
            hit = Finding(
                agent=agent,
                round=state.round,
                category="integrity",
                severity="critical",
                rule_id="control-capture",
                excerpt=payload[:200],
            )
            self._flag(state, [hit], flag)

    # -- stage 4: score ---------------------------------------------------
    def _score(self, state: MatchState) -> None:
        if state.round in state.scored_rounds:
            return  # idempotent resume
        for key, ks in sorted(state.keystones.items()):  # reveal after every attack, before scoring
            r, author = key.split(":", 1)
            if int(r) == state.round and not ks.revealed:
                salt = stakes.keystone_salt(state.seed, state.round, author)
                n = len(stakes.own_challenges(state.challenges, state.round, author))
                index = stakes.open_keystone(ks.commit, salt, n)  # the seal logs only the commit
                self._checkpoint(state, ev.KEYSTONE_REVEALED, agent=author, round=state.round, index=index, salt=salt)
        delta = score_round(
            state.round,
            [a.name for a in state.agents],
            state.challenges.values(),
            state.validations,
            state.attacks,
            state.rules,
            stakes.keystone_targets(state, state.round),
        )
        points = capture_points(state.round, state.challenges.values(), state.validations, state.attacks, state.rules)
        lines = heat_of_state(state, state.round)
        extra = {"heat": lines} if lines else {}
        self._checkpoint(state, ev.ROUND_SCORED, round=state.round, delta=delta, points=points, **extra)


def _with_terrain(text: str, ground: str | None) -> str:
    return text if ground is None else f"{text} [terrain={ground}] {terrain.describe(ground)}"
