# ADR 0010: the check challenge form

Status: accepted (closes #70).

## Context
Rejection reasons across 195 framing-experiment matches and the hard-* runs (2,126 build rejections):

| Count | Cause |
|---|---|
| 745 | the solver failed (285 printed nothing; most of the rest were NameErrors on names defined in the challenge, plus timeouts) |
| 438 + 108 | the reference payload did not reveal the flag |
| 273 | empty input revealed the flag |
| 91 | the reference payload was shorter than 4 characters |
| 110 | FLAG was looked up via `__dict__` / `sys.modules` |
| 62 | malformed JSON, mostly missing `difficulty` / `reference_payload` |

Most of these are I/O plumbing errors (FLAG, stdin, solver scope), not puzzle design.

## Decision
- `rules.challenge_form = "check"` (preset `checked`). Authors write only `def check(s)` plus helpers.
- `domain/contract.compose` appends the arena's `WRAPPER`, which reads stdin and prints FLAG when `check` is
  True. Authors never touch FLAG, `sys` or stdin; using them is rejected with a reason.
- A solver runs after the author's definitions (`solver_program`), so it can call `check`. That whole program
  is policy-checked and gets `SOLVER_TIME_FACTOR` (4x) the wall and CPU limits.
- Prompts give 3 compact, policy-checked examples (`CHECK_EXAMPLES`, each tested end to end) and say FLAG is
  already a global.
- `repair_hint` adds one concrete next step for each of the top causes, in both forms.
- Build answers may omit `reference_payload` / `difficulty`; they default to `""` / 2.
- The `program` form is unchanged and stays the default (prompt snapshot unchanged).

## Consequences
In a live smoke test (nano vs mini, 3 rounds, checked) validity rose from about 10-33% to 83% (5/6 in 4 of 6
matches), and defenses still happen. The attacker sees the composed source, wrapper included.

## Addendum: variety (#79)
Live check-form matches reused one design for every challenge. With `rules.variety` (on in `checked`), each
build prompt gets a seeded theme from `variety.THEMES` (different for each of an agent's challenges until the
8 themes wrap) and a list of the author's past titles with technique tags. A repeated title, or a source
identical up to whitespace, is rejected through the normal repair loop.
