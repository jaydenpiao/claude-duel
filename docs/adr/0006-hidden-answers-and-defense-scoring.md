# ADR 0006: Hidden answers, solvers, a difficulty gate and defense-first scoring
Status: accepted.

**Problem.** In live matches every valid challenge fell on the first try. The accepting input sat in the source as plain text (`if data == "open-sesame"`), so the attacker just read it off. Defenses never happened, and the scoring (6 per defense against 10 per capture) gave authors no reason to try harder.

**Decision.**
1. *Hidden answer* (`policy.answer_exposed`). The accepting input must be at least 4 characters long. It must not appear in any string or bytes literal, or in the literals joined together, in any of these forms: verbatim, reversed, hex, base64 without padding, or the payload's own base64/hex decoding. It also must not appear as a list of int constants that equals its char codes, forward or reversed, up to a constant offset or XOR. The check is static and cheap, and it is deliberately limited to *obvious* encodings. Real obfuscation is the author's job, and the difficulty gate below catches whatever still slips through.
2. *Solver* (`ChallengeSpec.solver`, optional, never shown to the attacker). Low-tier models cannot compute their own answers in their heads. In live tuning, most invalid challenges came from a wrong reference payload. An author may therefore give a solver program instead. It runs under the same policy and the same sandbox with empty stdin, and its stdout becomes `reference_payload`. The challenge is then validated exactly as before, so validity is still proven by running the code.
3. *Difficulty gate* (`Rules.difficulty_gate`, off by default and in tests). After a challenge validates, a probe agent (`gate`) makes one attack using `Settings.gate_model`, which defaults to gpt-5-nano. That call costs roughly $0.0005 and is budgeted like any other call. If the probe captures the flag, the challenge is sent back to the repair loop as "too easy". We chose reject-and-repair over a score discount because it keeps scoring a pure function of the event log.
4. *Scoring*. `defense_points` (10) is at least `solve_points` (10), so a defense is worth at least as much as any capture of the same challenge (property-tested).
5. *Feedback the author can act on.* A challenge that crashes reports `error: <ExceptionType> at line N` and nothing else, because a message could quote FLAG. Rejections name a fix (for example "use sys.exit()"). `sys.exit` and a bare `_` are allowed because neither gives access to anything new.

**Consequences.** Captures now have to be earned, and authors are pushed toward state machines, VMs and checksums. Validity is the open problem: low-tier authors often write solvers that disagree with their own challenge. See the run report in `docs/runs/` and the follow-up issue on an arena-side search solver. Every match summary (`--format json`) and the `match finished` log now carry `valid_rate`, `capture_rate`, `captures` and `defenses`.

## Addendum: gate as a score (#78)
`rules.gate_mode = "score"` keeps a challenge the difficulty-gate probe solves on its first try, instead of
sending it back to repair. The arena marks it `Challenge.probe_solved`; authors cannot set this, since the
arena overwrites it. Its defense award is halved. The default, `"reject"`, is unchanged.

## Addendum: defender-side penalty lines (2026-10-29, ledger)
Modes may charge the defender *penalty lines* (keystone captured, novelty heat), provided:
1. Each is logged as its own line, never folded into `defense_award` or a capture award. Heat is recorded in
   `round_scored.heat` (challenge id -> `heat_penalty`) and replay recomputes it.
2. `defense_award` stays >= the largest capture award for that challenge; the inequality is evaluated on awards,
   before penalty lines.
3. Each line has a documented cap: heat <= `heat_max_pct`% (50) of that challenge's defense award,
   `min(D*max//100, D*(h-thr)//(100-thr))`; keystone captured = `base // 2`.
4. No line credits the opponent or raises a capture above `solve_award`.
5. Lines are pure functions of the event log. Heat similarity is a pre-order AST token LCS over logged source;
   the interpreter version that parsed it is recorded in `match_created.python`.

The shared property test (`TestScorerInvariants`) asserts 2-4 for every scorer, heat included.

## Addendum: vendor refusal (ADR 0016)
A model refusal is scored as a forfeited turn, not a weak attack: a refused build pays
`invalid_penalty` and a refused attack ends without a capture, so a valid challenge still
pays its defense award. See ADR 0016.

