"""Prompt construction and robust parsing of model output."""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from claude_duel.domain.errors import MalformedOutputError
from claude_duel.domain.models import ChallengeSpec, Rules
from claude_duel.domain.policy import ALLOWED_IMPORTS

SYSTEM = (
    "You are a contestant in Claude Duel, a sandboxed CTF arena. Respond with a single JSON object and nothing else."
)
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
M = TypeVar("M", bound=BaseModel)


class AttackMove(BaseModel):
    model_config = ConfigDict(extra="ignore")
    payload: str = Field(max_length=4096)


def build_prompt(agent: str, round_no: int, index: int, rules: Rules, error: str | None) -> str:
    lines = [
        "TASK: BUILD",
        f"AGENT: {agent}",
        f"ROUND: {round_no}",
        f"INDEX: {index}",
        "Write a Python 3 program that reads stdin and prints os.environ['FLAG'] only for the right input.",
        "Your opponent will see the source and description and must find that input.",
        f"Max source bytes: {rules.max_source_bytes(round_no)}. Allowed imports: {', '.join(sorted(ALLOWED_IMPORTS))}.",
        "No file, network, process or eval access. Harder rounds score more; escalate difficulty.",
        'Return JSON: {"title", "description", "source", "reference_payload", "difficulty" (1-3)}.',
    ]
    if error:
        lines.append(f"Your previous answer was rejected: {error}. Fix it.")
    return "\n".join(lines)


def attack_prompt(
    agent: str,
    challenge_id: str,
    title: str,
    description: str,
    source: str,
    attempt: int,
    history: list[tuple[str, str]],
) -> str:
    lines = [
        "TASK: ATTACK",
        f"AGENT: {agent}",
        f"CHALLENGE: {challenge_id}",
        f"ATTEMPT: {attempt}",
        f"Title: {title}",
        f"Description: {description}",
        "Source:",
        source,
        "Find the stdin payload that makes the program print the flag.",
    ]
    for i, (payload, out) in enumerate(history, 1):
        lines.append(f"Attempt {i} payload={payload!r} stdout={out!r}")
    lines.append('Return JSON: {"payload": "<stdin>"}')
    return "\n".join(lines)


def parse(text: str, model: type[M]) -> M:
    m = _JSON_BLOCK.search(text)
    if not m:
        raise MalformedOutputError("no JSON object found")
    try:
        return model.model_validate(json.loads(m.group(0)))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise MalformedOutputError(str(exc).splitlines()[0][:200]) from exc


def parse_challenge(text: str) -> ChallengeSpec:
    return parse(text, ChallengeSpec)


def parse_attack(text: str) -> AttackMove:
    return parse(text, AttackMove)
