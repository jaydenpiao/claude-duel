# ADR 0006: Hidden answers, solvers, a difficulty gate and defense-first scoring
Status: accepted.

**Problem.** In live matches every valid challenge fell on the first try. The accepting input sat in the source as plain text (`if data == "open-sesame"`), so the attacker just read it off. Defenses never happened, and the scoring (6 per defense against 10 per capture) gave authors no reason to try harder.

**Decision.**
1. *Hidden answer* (`policy.answer_exposed`). The accepting input must be at least 4 characters long. It must not appear in any string or bytes literal, or in the literals joined together, in any of these forms: verbatim, reversed, hex, base64 without padding, or the payload's own base64/hex decoding. It also must not appear as a list of int constants that equals its char codes, forward or reversed, up to a constant offset or XOR. The check is static and cheap, and it is deliberately limited to *obvious* encodings. Real obfuscation is the author's job, and the difficulty gate below catches whatever still slips through.
2. *Solver* (`ChallengeSpec.solver`, optional, never shown to the attacker). Low-tier models cannot compute their own answers in their heads. In live tuning, most invalid challenges came from a wrong reference payload. An author may therefore give a solver program instead. It runs under the same policy and the same sandbox with empty stdin, and its stdout becomes `reference_payload`. The challenge is then validated exactly as before, so validity is still proven by running the code.
3. *Difficulty gate* (`Rules.difficulty_gate`, off by default and in tests). After a challenge validates, a probe agent (`gate`) makes one attack using `Settings.gate_model`, which defaults to gpt-6-luna. That call costs roughly $0.0005 and is budgeted like any other call. If the probe captures the flag, the challenge is sent back to the repair loop as "too easy". We chose reject-and-repair over a score discount because it keeps scoring a pure function of the event log.
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

## Addendum: host work bound for heat similarity
Heat's LCS runs in the engine process. New (rules v2) heat-on matches pin `heat_compare=full`, an explicit
`heat_vs_opponent`, `heat_ref_cap` (ceiling 48; the in-match window is kept first), `heat_metric=lcs-v1` and
`heat_history_matches` (default 3). They omit `heat_token_cap`, which only the v1 `prefix` regime uses; a v1
reader of a prefix or full-heat log that carries the token cap still accepts it. Sources over 65536 bytes
are not parsed. Replay of a pinned log uses that pin, so the logged heat stays exact. A log that does not
pin the caps was scored with the full walk: replay recomputes it only when every compared source still fits
the ceiling, and otherwise reports that round as `heat legacy-unreplayable`. It does not substitute the prefix score,
and it does not call the round tampered. A parser overflow on deep nesting is an empty token sequence, the
same as a source that does not parse. Logs written between #238 and #279 may omit `heat_vs_opponent`; replay
of those is a reported heat mismatch when the inferred scope is wrong, not a silent rescoring.

Whole-source heat (`heat_compare=full`, new matches) never fails a match on work. A pair that is not a
subsequence gets the exact LCS from a bit-parallel algorithm (Hyyro: one add/and/or per token over a
`len(b)`-bit integer), inside a per-call work budget that real rounds do not reach. The multiset ceiling is
only a cold-skip filter: if it is below the threshold, the exact LCS is too and the pair is not compared.
Every source under the 65536-byte parse ceiling keeps its full token walk. A pair the call cannot compare exactly (budget spent) is
`heat over budget`: no penalty is charged on it, it is logged in `round_scored.heat_unchecked` (omit if empty),
and it is shown as `heat over budget`. A challenge is charged only on an exact similarity at or over the
threshold. An upper bound (such as the multiset ceiling, which ran 9-33 points above the LCS on unrelated
modules, S13-4) is never charged or shown as the similarity. Replay recomputes the same result. Admission caps sources so every pair a legal round can admit fits the work budget (and never past the 65536-byte parse ceiling).

## Addendum: vendor refusal (ADR 0016)
A refused build pays `invalid_penalty`. A refused attack is one spent try; the
attack continues with the remaining tries, and a valid challenge that was not
captured still pays its defense award. A refused difficulty-gate probe is
inconclusive, not held. See ADR 0016.

