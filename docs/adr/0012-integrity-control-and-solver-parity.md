# ADR 0012: integrity control and attacker solver parity

Status: accepted (closes #150).

## Context
A launch result must rule out two non-skill explanations. First, the harness could leak the
flag (for example through the environment or a runner bug) so that any program "captures".
Second, authors may ship a solver that runs in the sandbox, but attackers could only send a
literal payload, so attackers had less compute than the authors they faced.

## Decision
- `integrity_control` (off by default): every attack payload also runs, with the same flag and
  limits, on `CONTROL_SOURCE`, a program that reads stdin and never prints the flag. A capture
  there records a `control-capture` finding (category `integrity`, severity `critical`) and
  bumps `control.capture`. Scores never change; the launch pre-registration voids the run instead.
  It costs no tokens, only one extra sandbox run per try.
- `attacker_solver` (off by default): the attack schema gains `solver`. A non-empty solver runs
  like the author's solver (same source policy, 4x time, empty stdin, the check-form preamble),
  but with a throwaway flag derived per agent and round, never the challenge's. Its stdout is the
  payload. A rejected or failing solver is a failed try whose reason the attacker sees next time.
  Author and attacker solvers share one runner (`MatchEngine._run_solver`).

## Consequences
Both are opt-in, so default prompts and schemas stay byte-identical. A solver lets an attacker
search, which may raise capture rates. The A/B under docs/runs/ measures that effect.
