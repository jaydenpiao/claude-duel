"""Match orchestration: drives the lifecycle; every change is an event, appended then applied."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Mapping

from claude_duel.app import agents
from claude_duel.app.budget import CostBudget
from claude_duel.app.observers import IsolatedObserver
from claude_duel.domain import contract, findings, memory, policy
from claude_duel.domain import events as ev
from claude_duel.domain.errors import DeadlineExceededError, MalformedOutputError
from claude_duel.domain.flags import assert_no_flag, derive_flag, redact
from claude_duel.domain.models import (
    CAPTURED,
    Attack,
    AttackAttempt,
    Challenge,
    ChallengeSpec,
    Event,
    Finding,
    MatchState,
    SandboxLimits,
    Stage,
    Validation,
)
from claude_duel.domain.scoring import match_stats, score_round
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
GATE = "gate"  # provider key of the difficulty-gate probe (see Rules.difficulty_gate)
SOLVER_TIME_FACTOR = 4  # an author's solver may search; it gets 4x the wall and CPU of a challenge run
GATE_REJECTION = "too easy: a cheap probe model solved it on its first try; make the attacker reason through more steps"
REF_OUTPUT_CHARS = 200  # program output echoed back to the author when the reference run fails


def challenge_id(match_id: str, round_no: int, author: str, index: int) -> str:
    raw = f"{match_id}:{round_no}:{author}:{index}"
    return f"r{round_no}-{author}-{index}-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"


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
        snapshot leaves ``last_seq == -1`` with a non-empty log (round-2 N2).
        """
        events = self._log.read(state.match_id)
        if not events:
            self._checkpoint(state, ev.MATCH_CREATED, **ev.created_data(state))
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
        """Validate by applying to a copy, then append durably, then apply for real (round-2 N3).

        An event that can't be applied never reaches the log, so it can't poison resume or replay.
        """
        event = Event(v=ev.SCHEMA_VERSION, seq=state.last_seq + 1, type=kind, trace_id=current_trace_id(), data=data)
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
        """Single choke point for paid calls: budget check, call, charge, checkpoint."""
        self._budget.check(state)
        if state.usage.elapsed_ms >= state.rules.match_deadline_s * 1000:
            raise DeadlineExceededError(
                f"deadline: {state.usage.elapsed_ms / 1000:.1f}s of provider time >= {state.rules.match_deadline_s}s"
            )
        started = time.monotonic()
        completion = self._providers[agent].complete(request)
        elapsed = time.monotonic() - started
        self._metrics.timing("provider.seconds", elapsed, agent=agent, purpose=request.purpose)
        model = self._models[agent]
        usd = self._budget.charge(state.match_id, model, completion)
        self._checkpoint(
            state,
            ev.CALL_CHARGED,
            agent=agent,
            model=model,
            tokens=completion.total_tokens,
            usd=usd,
            elapsed_ms=round(elapsed * 1000),
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
                spec, reason = self._author(state, cid, agent.name, index)
                ch = Challenge(id=cid, round=state.round, author=agent.name, spec=spec, forfeit_reason=reason)
                self._checkpoint(state, ev.CHALLENGE_BUILT, challenge=ch.model_dump(mode="json"))

    def _author(self, state: MatchState, cid: str, agent: str, index: int) -> tuple[ChallengeSpec | None, str | None]:
        """Repair loop: a malformed or invalid answer is fed back (with the reason) for up to
        ``rules.max_format_retries`` more paid, budgeted calls. Provider errors propagate and fail the
        stage (resumable). Each rejection is checkpointed, so a resumed build continues from the last retry.
        The last invalid answer is kept, so validation records why it failed.
        """
        errors = state.build_errors.get(cid, [])
        last: ChallengeSpec | None = None
        while len(errors) <= state.rules.max_format_retries:
            error = errors[-1] if errors else None
            prompt = agents.build_prompt(
                agent, state.round, index, state.rules, error, last, self._memory(state, agent)
            )
            completion = self._call(state, agent, self._request(state, prompt, "build"))
            try:
                last = agents.parse_challenge(completion.text)
                last, problem = self._shape(state, last)
                if problem is None:
                    last, problem = self._assess(state, cid, agent, last)
            except MalformedOutputError as exc:
                self._metrics.incr("model.malformed", purpose="build")
                problem = f"malformed: {exc}"
            if problem is None:
                return last, None
            if not problem.startswith("malformed"):
                self._metrics.incr("model.invalid", purpose="build")
            self._checkpoint(state, ev.BUILD_REJECTED, challenge_id=cid, error=problem)
            errors = state.build_errors[cid]
        return (last, None) if last is not None else (None, errors[-1])

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

    def _assess(self, state: MatchState, cid: str, agent: str, spec: ChallengeSpec) -> tuple[ChallengeSpec, str | None]:
        """Solver, policy and runs, difficulty gate; then the tamper detector on the answer (logged only)."""
        spec, problem = self._solve_reference(state, cid, spec)
        problem = problem or self._problem(state, cid, state.round, spec) or self._gate(state, cid, spec)
        policy_reason = problem if problem and policy.reason_code(problem) != "other" else None
        hits = findings.scan_build(agent, state.round, (spec.title, spec.description), policy_reason)
        self._flag(state, hits, derive_flag(state.secret, cid))
        return spec, problem

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
            assert_no_flag(text, derive_flag(state.secret, cid), "memory")
        return text

    def _request(self, state: MatchState, prompt: str, purpose: Purpose) -> CompletionRequest:
        return CompletionRequest(system=agents.system_prompt(state.framing_text), prompt=prompt, purpose=purpose)

    def _gate(self, state: MatchState, cid: str, spec: ChallengeSpec) -> str | None:
        """Difficulty gate: one paid, budgeted attack by the cheap ``GATE`` probe; a capture means too easy."""
        if not state.rules.difficulty_gate:
            return None
        prompt = agents.attack_prompt(GATE, cid, spec.title, spec.description, spec.source, 1, [])
        completion = self._call(state, GATE, self._request(state, prompt, "attack"))
        solved = self._attempt(state, GATE, spec, derive_flag(state.secret, cid), completion).captured
        self._metrics.incr("gate.solved" if solved else "gate.held")
        return GATE_REJECTION if solved else None

    def _solve_reference(self, state: MatchState, cid: str, spec: ChallengeSpec) -> tuple[ChallengeSpec, str | None]:
        """If the author gave a solver, run it (same policy and sandbox, empty stdin): its stdout is the payload."""
        if not spec.solver:
            return spec, None
        program, cap = spec.solver, state.rules.max_source_bytes(state.round)
        if state.rules.challenge_form == "check":
            program, cap = contract.solver_program(spec.source, spec.solver), 2 * cap
        rejected = policy.check_source(program, cap)  # the whole program that runs, definitions included
        if rejected:
            return spec, f"solver: {rejected}"
        lim = state.limits
        slow = lim.model_copy(
            update={
                "wall_seconds": lim.wall_seconds * SOLVER_TIME_FACTOR,
                "cpu_seconds": lim.cpu_seconds * SOLVER_TIME_FACTOR,
            }
        )
        out = self._run_challenge(program, "", derive_flag(state.secret, cid), state, "solve_reference", slow)
        payload = out.stdout.strip()
        if not payload or out.exit_code or out.timed_out:
            status = "timeout" if out.timed_out else f"exit {out.exit_code}"
            return spec, f"solver failed ({status}); it printed {payload[:REF_OUTPUT_CHARS]!r}"
        return spec.model_copy(update={"reference_payload": payload}), None

    def _problem(self, state: MatchState, cid: str, round_no: int, spec: ChallengeSpec) -> str | None:
        """Why a challenge is invalid (policy, then sandboxed reference and empty-input runs), or None."""
        if spec.solver and not spec.reference_payload.strip():
            return "no reference input: the solver never printed one"
        rejected = policy.check(spec, state.rules.max_source_bytes(round_no))
        if rejected:
            return rejected
        flag = derive_flag(state.secret, cid)
        ref = self._run_challenge(spec.source, spec.reference_payload, flag, state, "validate")
        if flag not in ref.stdout:
            seen = redact(ref.stdout, (flag,)).strip()[:REF_OUTPUT_CHARS]
            return (
                "reference payload does not reveal flag"
                + (" (timeout)" if ref.timed_out else "")
                + (f" (exit {ref.exit_code}: it crashed or raised)" if ref.exit_code else "")
                + (f"; it printed {seen!r}" if seen else "; it printed nothing")
            )
        empty = self._run_challenge(spec.source, "", flag, state, "validate")
        if flag in empty.stdout:
            return "trivial: empty input reveals flag"
        return None

    # -- stage 3: attack --------------------------------------------------
    def _attack(self, state: MatchState) -> None:
        for ch in state.round_challenges(state.round):
            if not state.validations[ch.id].valid or ch.spec is None:
                continue
            attacker = state.opponent(ch.author)
            key = f"{ch.id}:{attacker}"
            if key in state.attacks:
                continue
            attack = self._solve(state, ch, key, attacker)
            self._checkpoint(state, ev.CHALLENGE_ATTACKED, key=key, attack=attack.model_dump(mode="json"))

    def _solve(self, state: MatchState, ch: Challenge, key: str, attacker: str) -> Attack:
        """Attempts are checkpointed one by one; a resumed attack replays history instead of re-paying."""
        spec = ch.spec
        assert spec is not None, "only valid challenges are attacked"
        flag = derive_flag(state.secret, ch.id)
        while True:
            history = state.attempts.get(key, [])
            tokens = sum(a.tokens for a in history)
            if history and history[-1].captured:
                return Attack.of(ch.id, attacker, history, solved=True, reason="flag captured")
            if len(history) >= state.rules.max_attempts:
                return Attack.of(ch.id, attacker, history, solved=False, reason="attempts exhausted")
            if tokens >= state.rules.attack_token_budget:
                return Attack.of(ch.id, attacker, history, solved=False, reason="token budget exhausted")
            pairs = [(a.payload, a.output) for a in history]
            prompt = agents.attack_prompt(
                attacker,
                ch.id,
                spec.title,
                spec.description,
                spec.source,
                len(history) + 1,
                pairs,
                self._memory(state, attacker),
            )
            assert_no_flag(prompt, flag, "attack prompt")
            self._checkpoint(state, ev.ATTACK_STARTED, key=key, try_no=len(history) + 1)
            completion = self._call(state, attacker, self._request(state, prompt, "attack"))
            attempt = self._attempt(state, attacker, spec, flag, completion)
            self._checkpoint(state, ev.ATTACK_ATTEMPTED, key=key, attempt=attempt.model_dump(mode="json"))

    def _attempt(
        self, state: MatchState, agent: str, spec: ChallengeSpec, flag: str, completion: Completion
    ) -> AttackAttempt:
        try:
            payload = agents.parse_attack(completion.text).payload
        except MalformedOutputError as exc:
            self._metrics.incr("model.malformed", purpose="attack")
            return AttackAttempt(payload=MALFORMED, output=str(exc), tokens=completion.total_tokens)
        self._flag(state, findings.scan_payload(agent, state.round, payload), flag)
        run = self._run_challenge(spec.source, payload, flag, state, "attack")
        output = CAPTURED if flag in run.stdout else redact(run.stdout, (flag,))[:500]
        return AttackAttempt(payload=payload, output=output, tokens=completion.total_tokens)

    # -- stage 4: score ---------------------------------------------------
    def _score(self, state: MatchState) -> None:
        if state.round in state.scored_rounds:
            return  # idempotent resume
        delta = score_round(
            state.round,
            [a.name for a in state.agents],
            state.challenges.values(),
            state.validations,
            state.attacks,
            state.rules,
        )
        self._checkpoint(state, ev.ROUND_SCORED, round=state.round, delta=delta)
