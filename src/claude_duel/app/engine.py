"""Match orchestration: drives the lifecycle, checkpointing every unit of work."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Mapping

from claude_duel.app import agents
from claude_duel.domain import policy
from claude_duel.domain.errors import MalformedOutputError, ProviderError
from claude_duel.domain.flags import assert_no_flag, derive_flag, redact
from claude_duel.domain.models import Attack, Challenge, ChallengeSpec, Event, MatchState, Stage, Validation
from claude_duel.domain.scoring import add_scores, score_round
from claude_duel.domain.state_machine import TERMINAL, next_stage, transition
from claude_duel.observability import current_trace_id, log, trace
from claude_duel.ports import CompletionRequest, LLMProvider, MatchStore, Metrics, Sandbox

_LOG = logging.getLogger(__name__)


def challenge_id(match_id: str, round_no: int, author: str, index: int) -> str:
    raw = f"{match_id}:{round_no}:{author}:{index}"
    return f"r{round_no}-{author}-{index}-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"


class MatchEngine:
    def __init__(
        self, providers: Mapping[str, LLMProvider], sandbox: Sandbox, store: MatchStore, metrics: Metrics
    ) -> None:
        self._providers = providers
        self._sandbox = sandbox
        self._store = store
        self._metrics = metrics

    # -- lifecycle -------------------------------------------------------
    def run(self, state: MatchState) -> MatchState:
        with self._store.lock(state.match_id), trace():
            while state.stage not in TERMINAL:
                started = time.monotonic()
                try:
                    self._execute_stage(state)
                except Exception as exc:
                    state.failure = f"{type(exc).__name__}: {redact(str(exc))}"
                    transition(state, Stage.FAILED)
                    self._emit(state, "match_failed", reason=state.failure)
                    self._store.save(state)
                    raise
                target = next_stage(state)
                self._emit(state, "stage_completed", stage=state.stage.value, next=target.value)
                transition(state, target)
                self._store.save(state)
                self._metrics.timing("stage.seconds", time.monotonic() - started, stage=state.stage.value)
            log(
                _LOG,
                logging.INFO,
                "match finished",
                match_id=state.match_id,
                stage=state.stage.value,
                scores=state.scores,
            )
        return state

    def _execute_stage(self, state: MatchState) -> None:
        handler = {
            Stage.PENDING: lambda s: None,
            Stage.BUILD: self._build,
            Stage.VALIDATE: self._validate,
            Stage.ATTACK: self._attack,
            Stage.SCORE: self._score,
        }[state.stage]
        handler(state)

    def _emit(self, state: MatchState, kind: str, **data: object) -> None:
        state.events.append(Event(seq=len(state.events), type=kind, trace_id=current_trace_id(), data=data))

    def _checkpoint(self, state: MatchState, kind: str, **data: object) -> None:
        self._emit(state, kind, **data)
        self._store.save(state)
        self._metrics.incr(f"unit.{kind}")

    # -- stage 1: build ---------------------------------------------------
    def _build(self, state: MatchState) -> None:
        for agent in state.agents:
            for index in range(state.rules.challenges_per_round):
                cid = challenge_id(state.match_id, state.round, agent.name, index)
                if cid in state.challenges:
                    continue  # idempotent resume
                spec, reason = self._author(state, agent.name, index)
                state.challenges[cid] = Challenge(
                    id=cid, round=state.round, author=agent.name, spec=spec, forfeit_reason=reason
                )
                self._checkpoint(
                    state,
                    "challenge_built",
                    challenge_id=cid,
                    author=agent.name,
                    forfeit=reason,
                    difficulty=spec.difficulty if spec else None,
                )

    def _author(self, state: MatchState, agent: str, index: int) -> tuple[ChallengeSpec | None, str | None]:
        error: str | None = None
        for _ in range(state.rules.max_format_retries + 1):
            prompt = agents.build_prompt(agent, state.round, index, state.rules, error)
            try:
                completion = self._providers[agent].complete(
                    CompletionRequest(system=agents.SYSTEM, prompt=prompt, purpose="build")
                )
                return agents.parse_challenge(completion.text), None
            except MalformedOutputError as exc:
                error = str(exc)
                self._metrics.incr("model.malformed", purpose="build")
            except ProviderError as exc:
                return None, f"provider: {type(exc).__name__}"
        return None, f"malformed: {error}"

    # -- stage 2: validate ------------------------------------------------
    def _validate(self, state: MatchState) -> None:
        for ch in state.round_challenges(state.round):
            if ch.id in state.validations:
                continue
            ok, reason = self._check(state, ch)
            state.validations[ch.id] = Validation(challenge_id=ch.id, valid=ok, reason=reason)
            self._checkpoint(state, "challenge_validated", challenge_id=ch.id, valid=ok, reason=reason)

    def _check(self, state: MatchState, ch: Challenge) -> tuple[bool, str]:
        if ch.spec is None:
            return False, ch.forfeit_reason or "forfeit"
        rejected = policy.check(ch.spec, state.rules.max_source_bytes(ch.round))
        if rejected:
            return False, rejected
        flag = derive_flag(state.secret, ch.id)
        ref = self._sandbox.run(ch.spec.source, ch.spec.reference_payload, {"FLAG": flag}, state.limits)
        if flag not in ref.stdout:
            return False, "reference payload does not reveal flag" + (" (timeout)" if ref.timed_out else "")
        empty = self._sandbox.run(ch.spec.source, "", {"FLAG": flag}, state.limits)
        if flag in empty.stdout:
            return False, "trivial: empty input reveals flag"
        return True, "ok"

    # -- stage 3: attack --------------------------------------------------
    def _attack(self, state: MatchState) -> None:
        for ch in state.round_challenges(state.round):
            if not state.validations[ch.id].valid or ch.spec is None:
                continue
            attacker = state.opponent(ch.author)
            key = f"{ch.id}:{attacker}"
            if key in state.attacks:
                continue
            state.attacks[key] = self._solve(state, ch, ch.spec, attacker)
            a = state.attacks[key]
            self._checkpoint(
                state,
                "challenge_attacked",
                challenge_id=ch.id,
                attacker=attacker,
                solved=a.solved,
                attempts=a.attempts,
                tokens=a.tokens_used,
                reason=a.reason,
            )

    def _solve(self, state: MatchState, ch: Challenge, spec: ChallengeSpec, attacker: str) -> Attack:
        flag = derive_flag(state.secret, ch.id)
        history: list[tuple[str, str]] = []
        tokens = 0

        def result(solved: bool, reason: str) -> Attack:
            return Attack(
                challenge_id=ch.id,
                attacker=attacker,
                solved=solved,
                attempts=len(history),
                tokens_used=tokens,
                reason=reason,
            )

        while len(history) < state.rules.max_attempts:
            if tokens >= state.rules.attack_token_budget:
                return result(False, "token budget exhausted")
            prompt = agents.attack_prompt(
                attacker, ch.id, spec.title, spec.description, spec.source, len(history) + 1, history
            )
            assert_no_flag(prompt, flag, "attack prompt")
            try:
                completion = self._providers[attacker].complete(
                    CompletionRequest(system=agents.SYSTEM, prompt=prompt, purpose="attack")
                )
            except ProviderError as exc:
                return result(False, f"provider: {type(exc).__name__}")
            tokens += completion.total_tokens
            try:
                payload = agents.parse_attack(completion.text).payload
            except MalformedOutputError as exc:
                self._metrics.incr("model.malformed", purpose="attack")
                history.append(("<malformed>", str(exc)))
                continue
            run = self._sandbox.run(spec.source, payload, {"FLAG": flag}, state.limits)
            if flag in run.stdout:
                history.append((payload, "<flag captured>"))
                return result(True, "flag captured")
            history.append((payload, redact(run.stdout, (flag,))[:500]))
        return result(False, "attempts exhausted")

    # -- stage 4: score ---------------------------------------------------
    def _score(self, state: MatchState) -> None:
        delta = score_round(
            state.round,
            [a.name for a in state.agents],
            state.challenges.values(),
            state.validations,
            state.attacks,
            state.rules,
        )
        state.scores = add_scores(state.scores, delta)
        self._emit(state, "round_scored", round=state.round, delta=delta, totals=state.scores)
