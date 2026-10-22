---
name: add-challenge-type
description: Use when changing what agents may author as challenges (new allowed module, field, or kind).
---
Today there is one challenge kind: a Python script reading stdin that must print the env `FLAG` only for the right input (ADR 0002). There is no type registry yet.
1. Model: extend `ChallengeSpec` in `domain/models.py` (give new fields defaults so old state loads).
2. Policy: update `domain/policy.py` (`ALLOWED_IMPORTS`, `BANNED_NAMES`, `BANNED_ATTRS`, `_check_node`). Every loosening needs a security review.
3. Prompts/parsing: update `app/agents.py` `build_prompt` / `parse_challenge`.
4. Validation stays in `engine.py:_check`: reference payload must reveal the flag, empty input must not.
5. FakeProvider templates in `adapters/providers/fake.py` should exercise the new kind.
6. Tests: policy accept/reject cases, engine validation, replay consistency. If you add a real type abstraction, write an ADR first.
