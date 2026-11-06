"""Deterministic fake provider used for tests, CI smoke matches and demos.

Builders pick from a fixed template library; attackers try the payloads they
"know" (a per-agent, skill-sized subset) one per attempt. No randomness.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass

from claude_duel.domain import terrain
from claude_duel.domain.policy import MAZE_SOURCE
from claude_duel.ports import Completion, CompletionRequest


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    source: str
    payload: str
    difficulty: int


_HEAD = "import sys\n\ndata = sys.stdin.read().strip()\n"
_SHA_DUEL = hashlib.sha256(b"duel").hexdigest()
TEMPLATES: tuple[Template, ...] = (
    Template("password", "Maze", MAZE_SOURCE, "DDRR", 1),
    Template(
        "reverse",
        "Mirror",
        _HEAD + "codes = [ord(c) for c in data]\n"
        "if len(data) == 5 and data == data[::-1] and data.islower() and data[2] == 'd'\\\n"
        "        and sum(codes) == 522 and codes[0] > codes[1]:\n    print(FLAG)\n",
        "radar",
        1,
    ),
    Template(
        "sum",
        "Arithmetic gate",
        _HEAD + "parts = data.split()\nif len(parts) == 2 and all(p.isdigit() for p in parts):\n"
        "    a, b = int(parts[0]), int(parts[1])\n    if a + b == 1337 and a > b > 0:\n"
        "        print(FLAG)\n",
        "1000 337",
        2,
    ),
    Template(
        "hash",
        "Preimage",
        "import hashlib\n" + _HEAD + f'if hashlib.sha256(data.encode()).hexdigest() == "{_SHA_DUEL}":\n'
        "    print(FLAG)\n",
        "duel",
        3,
    ),
    Template(
        "b64",
        "Knock",
        "import base64\nimport binascii\nimport json\n"
        + _HEAD
        + "try:\n    knock = json.loads(base64.b64decode(data))\n"
        "except (binascii.Error, ValueError):\n    knock = {}\n"
        "if isinstance(knock, dict) and knock.get('x', 0) * 3 == knock.get('y', 0) + 1 > 1:\n    print(FLAG)\n",
        base64.b64encode(b'{"x": 5, "y": 14}').decode(),
        2,
    ),
)
_TEMPLATE_SOURCES = frozenset(t.source for t in TEMPLATES)


def is_template(source: str) -> bool:
    """True only for this provider's own fixed challenge sources."""
    return source in _TEMPLATE_SOURCES


_AGENT = re.compile(r"^AGENT: (\S+)$", re.M)
_ROUND = re.compile(r"^ROUND: (\d+)$", re.M)
_INDEX = re.compile(r"^INDEX: (\d+)$", re.M)
_ATTEMPT = re.compile(r"^ATTEMPT: (\d+)$", re.M)
_BUDGET = re.compile(r"^BUDGET: (\d+)$", re.M)
_CHALLENGES = re.compile(r"^CHALLENGES: (\d+)$", re.M)


def _stable(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


def _field(pattern: re.Pattern[str], text: str, default: str) -> str:
    m = pattern.search(text)
    return m.group(1) if m else default


class FakeProvider:
    name = "fake"

    def __init__(self, skill: int = 2, malformed_first: int = 0) -> None:
        self._skill = skill
        self._malformed_left = malformed_first

    def complete(self, request: CompletionRequest) -> Completion:
        if self._malformed_left > 0:
            self._malformed_left -= 1
            return self._wrap(request, "sure! here is my answer, not json")
        agent = _field(_AGENT, request.prompt, "anon")
        if request.purpose == "allocate":  # even split, all of it
            k = int(_field(_CHALLENGES, request.prompt, "1"))
            body: dict[str, object] = {"stakes": [int(_field(_BUDGET, request.prompt, "0")) // k] * k, "keystone": 0}
        elif request.purpose == "build":
            idx = _stable(agent, _field(_ROUND, request.prompt, "1"), _field(_INDEX, request.prompt, "0"))
            t = TEMPLATES[idx % len(TEMPLATES)]
            body = {
                "title": t.title,
                "description": f"Find the input that reveals the flag ({t.key}).",
                "source": t.source,
                "reference_payload": t.payload,
                "difficulty": t.difficulty,
            }
        else:
            known = sorted(TEMPLATES, key=lambda t: _stable(agent, t.key))[: self._skill]
            attempt = int(_field(_ATTEMPT, request.prompt, "1"))
            payload = known[attempt - 1].payload if attempt <= len(known) else f"guess-{attempt}"
            ground = re.search(r"\[terrain=([a-z0-9:]+)\]", request.prompt)
            if ground:  # a skilled fake reasons through the transform
                payload = terrain.invert(ground.group(1), payload) or payload
            body = {"payload": payload}
        return self._wrap(request, "```json\n" + json.dumps(body) + "\n```")

    @staticmethod
    def _wrap(request: CompletionRequest, text: str) -> Completion:
        return Completion(text=text, input_tokens=len(request.prompt) // 4, output_tokens=len(text) // 4)
