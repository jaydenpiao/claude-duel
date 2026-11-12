# Design: core game logic for the CLI-only game ("Sealed Ledger")
Status: draft for Elon, 2026-10-29. Design only, no repo code changed. Fight animation is parked (PR #178 stays as is, not merged).
Changelog: 2026-10-29 — heat redesigned as a separate bounded defender-side penalty line (defense stays full base; ADR 0006 addendum §4.6); similarity = pre-order AST walk with documented threshold; invalid challenges excluded; heat disclosed in prompts and memory; tests 7, 9, 10 updated (overshoot bound, #171 caps).
Changelog: 2026-10-29 — synced to build: keystone picked in the allocate call (not forge); keystone salt via sha256 (not mulberry32); terrain drawn after authoring+validation and logged before allocation/keystone commit; past keystones shown in memory.
Fits: ADR 0007 (one `Rules` + `SCORERS` registry), ADR 0006 (defense >= capture; captures may only be discounted, never bonused), ADR 0004 (event-sourced replay), ADR 0003/0011 (sandbox), ADR 0009 (round memory), ADR 0014 (CAPPED void).

## 0. Urgent finding while researching prices
- **gpt-4.1-nano was shut down by OpenAI on 2026-10-23** ([model page: "Status: Shut down"](https://developers.openai.com/api/docs/models/gpt-4.1-nano), [deprecation notice](https://community.openai.com/t/deprecation-notice-upcoming-model-shutdowns-in-2026/1379553)). The README and the difficulty gate (`gate_model`) still default to it. Live runs on it will fail.
- **gpt-5-nano is also deprecated, with shutdown planned for 2026-12-11** (same notice and its [model page](https://developers.openai.com/api/docs/models/gpt-5-nano)). It is fine for launch but not as a long-term default.
- Action: change the default and gate model before any launch spend. I have not edited the repo.

## 1. Prior work (links checked 2026-10-29, HTTP 200) and how we differ
| System | What it does | How Sealed Ledger differs |
|---|---|---|
| [LMSYS Chatbot Arena / LMArena](https://lmarena.ai) ([paper](https://arxiv.org/abs/2403.04132)) | Humans vote between two anonymous chat answers; Bradley-Terry ratings | No human judge. Outcomes come from sandbox execution, and agents act against each other directly |
| [Cybench](https://cybench.github.io) / [NYU CTF Bench](https://arxiv.org/abs/2406.05590) | One agent solves fixed, human-written CTF tasks | Both sides write the challenges, the tasks are adversarial and new every match, and budgets are allocated strategically |
| [AgentBench](https://arxiv.org/abs/2308.03688) | Single agents across 8 environments (OS, DB, games...) | Head-to-head play with hidden information. There is no fixed environment |
| [TextArena](https://github.com/LeonGuertler/TextArena) | A collection of classic text games (chess, poker, negotiation) for LLM-vs-LLM play | Our game is not a port of a human game. The moves are executable programs |
| [GTBench](https://arxiv.org/abs/2402.12348) | Game-theory reasoning on 10 classic games (tic-tac-toe, Nim, auctions...) | Our Blotto-style allocation is played over opponent-authored code whose value is unknown and has to be estimated by reading it |
| [Kaggle Game Arena](https://www.kaggle.com/game-arena) | Models play chess and other standard games on a public leaderboard | Same as above. Ours is a new game, not a standard one |
| [CodinGame](https://www.codingame.com) | Humans write bots for fixed multiplayer games | Our agents write the game content (challenges) during the match |
| [Battlecode](https://battlecode.org) | MIT RTS. Teams' bots control units on a seeded map | Our "terrain" is a seeded transform on the challenge input, not a map |
| [Halite](https://halite.io) | Resource-gathering bot game on a grid | Our resource is the real token wallet, priced in dollars |
| [Screeps](https://screeps.com) | A persistent MMO where you program units in JS | Ours is a short, sealed, replayable match with no persistent world |
| [Core War](https://en.wikipedia.org/wiki/Core_War) / [corewar.co.uk KOTH hills](https://corewar.co.uk) | Redcode warriors overwrite each other in a shared memory ring | Our programs never touch each other's memory. The fight is over flags under sandboxing |
| [RoboWar](https://en.wikipedia.org/wiki/RoboWar) | Stack-language robots fight in an arena | Same as Core War |
Honest note: each building block exists somewhere else, including attack-defense CTFs (DEF CON style), Colonel Blotto, and commit-reveal. **I found no system that combines LLM-authored adversarial CTF challenges, sealed simultaneous token-budget allocation, a commit-reveal keystone, and seeded input terrain.** That novelty claim covers only the systems above. It is not an exhaustive search.

## 2. Options
**A. Sealed Ledger (pick).** The current build → validate → attack loop gets a hidden-information economic layer:
- simultaneous sealed allocation (Blotto) of each attacker's token budget across the opponent's challenges;
- a commit-revealed keystone for the defender;
- a seeded input "terrain" drawn after authoring and validation, before allocation;
- a novelty "heat" that penalises reusing your own patterns.

**B. Shared Core.** Both agents write programs for one seeded VM tape and the programs fight there, Core War style. Rejected because it copies Core War, Battlecode, and RoboWar, and because it throws away the CTF validation pipeline we already built (our moat).

**C. Relay Chains.** Each side's K challenges form a chain: capturing stage i gives you part of the input for stage i+1, and defenders can splice in decoys. Interesting, but a single bad stage breaks a whole chain. That makes validity, and therefore fairness, fragile on nano models. Kept as a possible later mode (`relay`).

## 3. Why A
- It reuses everything we already have: challenges, the validator, the sandbox, the attack loop, memory, caps, and replay.
- It adds real strategy (allocation, bluffing, and adapting to the opponent's history) without needing smarter models.
- Every new decision is a small structured output, which keeps it cheap.
- It is a ScoringRule kind plus Rules sections, not a fork.

## 4. Mechanics
### 4.1 Rules (ADR 0007 sections; preset `ledger` in `domain/modes.py`)
```python
class LedgerRule(_Frozen):
    challenges_per_round: int = 3          # K
    attack_tokens: int = 12_000            # per attacker per round, split across K
    min_alloc: int = 1_000                 # each allocated stake >= this, or 0 (= skip)
    keystone: bool = True
    terrain: Literal["none", "seeded"] = "seeded"
    heat_window: int = 2                   # rounds of own history compared for novelty
    heat_threshold_pct: int = 70           # pre-order AST similarity >= this -> heat
    heat_max_pct: int = 50                 # heat penalty cap, % of that challenge's defense award
Rules(mode="ledger", scoring=ScoringRule(kind="ledger"), ledger=LedgerRule(), memory=MemoryRule(kind="source"), ...)
```

### 4.2 Round phases (state machine: `build → validate → terrain → allocate+commit → attack → reveal → score`)
1. **Forge** (one call per side): write K challenges (the existing ChallengeSpec, solver allowed). The keystone is picked in the Allocate call (step 4), which sees validity and sources, so it costs no extra call. When that call returns, the arena stores `commit = sha256("<k>:<salt>")`, with `salt = sha256("<seed>:<round>:<author>:keystone")[:16]`, so replay can rebuild it. k sits in a sealed event field, never put in an opponent prompt before reveal. The commit is integrity-only: it stops the log being rewritten after the fact, it does not hide k from the arena or a log reader.
2. **Validate**: unchanged (AST allowlist + sandbox). Invalid challenges get the existing penalty.
3. **Terrain** draw, after challenges are authored and validated, logged before allocation (so before the keystone commit; fine, since terrain is public):
   - `sha256("<seed>:<round>:<author>:<index>:terrain")` picks one public stdin transform per challenge from a fixed table: `identity, reverse, rot13, xor_byte(b), b64, swapcase, prefix(n)`.
   - The arena applies the transform to the attacker's payload before the challenge reads stdin.
   - Re-validation: the reference payload is pre-inverted by the arena (every transform in the table has an exact inverse). Validity is therefore kept by construction, and both runs are logged.
   - Effect: authors cannot hand-tune a challenge to one input path, and attackers must reason through the transform.
4. **Allocate** (one small JSON call per side, simultaneous; it also returns the keystone index k over the author's own challenges sorted by id, shown with titles, validity and sources; an invalid or out-of-range k forfeits):
   - Each attacker sees the opponent's K sources, the terrains, the opponent's commit hash, and memory (the opponent's past keystone choices and outcomes).
   - It returns `stakes: [int]*K` with `sum <= attack_tokens` and each stake either 0 or >= min_alloc.
   - Invalid JSON or invalid sums fall back to a deterministic even split, logged as `alloc_fallback`.
5. **Attack**: on challenge i, the attacker has `stakes[i]` tokens and up to `max_attempts` tries, as now. A stake of 0 means the challenge is skipped and counts as a defense.
6. **Reveal**: the arena opens the commits and logs k and the salt. Replay checks the hash, and a mismatch fails the match as tampered.
7. **Score** (pure), then **memory**: the next round sees full sources, stakes, past keystones (revealed k per round), and outcomes (ADR 0009 budget).

### 4.3 Scoring (`SCORERS["ledger"]`, integers only, composes `classic`)
Per challenge i of author A, attacked by B:
- `base = solve_award(classic)` (ADR 0006 numbers × round weight).
- **Captured**: `B += max(1, base - floor(base * used_i / (2*stakes_i)))`. The discount runs from 0 up to 50% depending on the share of the stake used. It is a discount only, never above base, with a minimum of 1.
- **Defended**: `A += defense_award(classic)`, and `× 2` if i == k (the keystone). That is a defense-side multiplier, so ADR 0006's "defense ≥ capture" still holds.
- **Keystone captured**: `A -= base // 2`. This is a defender penalty, not a capture bonus. Bluffing has a cost.
- **Heat** (separate defender-side penalty line, like the keystone-captured penalty; it never touches the defense award):
  - Defended challenge i still earns its full `defense_award` (× 2 if keystone). Heat is logged as its own line `heat_penalty_i` and subtracted from the author A.
  - **Similarity** (pure, from logged source only): parse with `ast.parse`; walk the tree in true pre-order (node, then `ast.iter_child_nodes` in field order, recursively); emit one token per node = `type(node).__name__`, plus for `Constant` the type name of its value, plus for `BinOp/UnaryOp/BoolOp/Compare` the operator class names. Identifiers, literal values, comments, docstrings and formatting are dropped. Similarity `sim = 100 * LCS(seq_a, seq_b) // max(len(seq_a), len(seq_b))` (integer floor; 0 if either is empty). Deterministic: no hashing of Python objects, no dict iteration, stdlib `ast` pinned to the repo's Python version (recorded in the match header). Host work: a pinned match compares the first `heat_token_cap` tokens (ceiling 256) and at most `heat_ref_cap` references (ceiling 48, in-match window first). Sources over 65536 bytes are not parsed. A log without the pin is exact only while it fits that ceiling; past it the round is unverified rather than scored with the prefix. A whole-source match (`heat_compare=full`) computes the exact LCS bit-parallel; the multiset ceiling is only a cold-skip filter; a pair past the work budget is `heat unchecked` (`round_scored.heat_unchecked`, charged 0), so the match never fails on heat work and no upper bound is charged.
  - **Comparison set**: the author's own *valid* challenges from the previous `heat_window` rounds. Challenges that failed validation are excluded on both sides: an invalid challenge never takes heat (it already gets the invalid penalty) and is never a reference for heat. `h_i = max sim` over the set (0 if empty).
  - **Threshold** (documented, default 70): if `h_i < heat_threshold_pct` there is no heat. Otherwise `heat_penalty_i = min(floor(defense_award_i * heat_max_pct/100), floor(defense_award_i * (h_i - thr) / (100 - thr)))`, with `thr = heat_threshold_pct`; at 100% similarity it reaches the cap (50% of the defense award).
  - **Bounded and floored**: heat applies only to defended challenges (a captured challenge already pays out to the attacker). The penalty is at most `heat_max_pct`% of that challenge's defense award, so the net for that challenge is always ≥ 1 when defense ≥ 2. Heat never lowers a capture award and never adds to the opponent, so the capture floor (min 1, ≤ base) is untouched.
  - **Disclosure**: the forge prompt states the heat rule, threshold and cap. Memory (ADR 0009 budget) shows, per own past challenge, its heat similarity and penalty, so models can steer away from repeats.
  - This pushes authors toward novelty without using a judge.
- Invalid challenges get the existing penalty and can never be the keystone. Choosing an invalid keystone forfeits the keystone multiplier.
- **Dollars**: `usd_micro` per side is logged in `call_charged`, with prices frozen at match creation. It does not change points in `ledger`. The tiebreak on equal points is lower `usd_micro`, then lower total tokens. Efficiency enters points only through the capture discount, in tokens.

### 4.6 Draft ADR 0006 addendum: defender-side penalty lines
> **Status**: draft, 2026-10-29. **Context**: ADR 0006 requires defense award ≥ capture award for the same challenge, and time/efficiency may only discount captures (min 1). Ledger adds defender-side penalties (keystone captured, heat).
> **Decision**: modes may add *penalty lines* charged to the defender, provided: (1) they are logged as separate score lines, never folded into `defense_award` or `capture_award`; (2) `defense_award` itself stays ≥ the maximum capture award for that challenge (the ADR 0006 inequality is evaluated on awards, before penalty lines); (3) each line is bounded by a documented cap (heat ≤ `heat_max_pct`% of the challenge's defense award; keystone-captured = `base // 2`); (4) no penalty line credits the opponent or raises a capture above `solve_award`; (5) lines are pure functions of the event log.
> **Consequences**: the shared ADR 0006 property test asserts (2)–(4) for every scorer in `SCORERS`, including heat and keystone lines.

### 4.4 Seeded randomness
- The remaining random draws use `mulberry32(match_seed ^ round ^ SALT_*)`: fallback split and the order attacks run in. Keystone salt and terrain use sha256 (below).
- The commit salt is `sha256("<seed>:<round>:<author>:keystone")[:16]`, and terrain is drawn from `sha256("<seed>:<round>:<author>:<index>:terrain")`, keyed on the author's challenge slot (not the match id) so the same seed gives the same draws.
- Model outputs are recorded in events. Replay re-folds them and never calls a model.
- Real latency is a metric only (lead decision).

### 4.5 Why it is hard to copy (honest)
- **Emergent depth**: the value of a stake depends on code the opponent wrote this match, so there is no fixed solution or opening book. Keystone choice against allocation is a mixed-strategy game, Blotto with hidden values.
- **Hidden information**: commit-reveal keystones, plus allocations made simultaneously.
- **Adaptation**: memory exposes past keystones and stakes, so bluff patterns can be exploited and must be varied. Heat punishes repeating yourself.
- **Moat**: the moat is the integrated pipeline (validator + sandbox + replay-verified, hash-chained logs + ratings) plus a growing corpus of adversarial challenges and allocation traces. The rules can be copied. The data and the trust machinery take time.
- **Not a moat**: the rules themselves. Anyone can reimplement them in a weekend. Say so publicly.

## 5. Terminal UI mock (~100 cols)
```
┌ claude-duel · mode ledger · seed 0x5EED1E55 · round 2/3 · phase ATTACK ─────────────────────────┐
│ ALPHA gpt-5-nano                         31 pts │ BRAVO claude-haiku (repo price table)   28 pts │
│ tok in  18,402  out  6,911   $0.0037         │ tok in  17,950  out  7,204   $0.0045           │
│ wallet  ███████████░░░░  7,420/12,000 tok    │ wallet  ████████░░░░░░░  5,100/12,000 tok      │
├──────────────────────────────────────────────┼────────────────────────────────────────────────┤
│ BRAVO's vault (ALPHA attacks)   keystone: 3f9a…c2 (sealed)                                       │
│  #1 rot13    stake 6,000  try 2/3  used 3,880   ▶ attacking                                       │
│  #2 xor 0x5c stake 4,000  try 1/3  used   950                                                     │
│  #3 b64      stake     0  skipped → defended                                                      │
│ ALPHA's vault (BRAVO attacks)   keystone: 81d0…7e (sealed)                                       │
│  #1 reverse  stake 2,000  CAPTURED  +16 (base 20, disc 20%)                                     │
│  #2 identity stake 8,000  try 3/3  used 7,100                                                     │
│  #3 swapcase stake 2,000  DEFENDED +10   heat 74% −1                                            │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ log ▸ r2 alloc ALPHA [6000,4000,0] · BRAVO [2000,8000,2000] · terrain drawn · 14:02:11 EDT       │
│ match $0.0082 / cap $0.25   run $0.31 / $5.00   lifetime $7.12 / $33.00                           │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```
(Numbers are illustrative, not from a run. Rows render from the event log only, so `duel replay --view arena` produces the same frames as a live run.)

## 6. Cost estimate (estimate, not measured)
Prices: gpt-5-nano $0.05 in / $0.40 out per 1M ([OpenAI model page](https://developers.openai.com/api/docs/models/gpt-5-nano)). These match `DEFAULT_PRICES` in app/budget.py. Haiku is priced from the repo table only ($0.10/$0.50); I did not verify it online this turn.

Per match, assumed shape: 3 rounds, K=3, 3 attempts.
| Calls | Count | Tokens in/out each (assumed) | $ |
|---|---|---|---|
| Forge | 2×3 = 6 | 3,000 / 1,500 | 6×(0.00015+0.00060) = 0.00450 |
| Repair (assume 1 per side-round) | 6 | 2,000 / 1,000 | 6×(0.00010+0.00040) = 0.00300 |
| Allocate | 6 | 2,500 / 100 | 6×(0.000125+0.00004) = 0.00099 |
| Attack attempts (worst case) | 2×3×3×3 = 54 | 2,000 / 300 | 54×(0.00010+0.00012) = 0.01188 |
| **Subtotal** | | | **≈ $0.0204** |

- gpt-5-nano bills hidden reasoning as output, so I apply ×3 headroom: **≈ $0.06 per match (estimate)**.
- The worst case is bounded by `attack_tokens`, and the cap is checked by `duel estimate --mode ledger`, which should be built and used to replace this table.

$33 launch budget:
- dev/tuning: $6 (~100 matches)
- launch matches: $20 (~330 matches at $0.06)
- reserve: $7
- Set `lifetime_max_usd = 33` (the current default is 40) and `match_max_usd = 0.25`. A match that hits its cap becomes a CAPPED void (ADR 0014).

## 7. Acceptance tests
1. **Determinism**: same seed + same recorded model outputs (fake provider) → byte-identical event log hash and final scores across 2 runs and across `duel replay`.
2. Different seed → different terrain draw. Terrain draws are reproducible from the sha256 terrain key (seed, round, author, index) alone.
3. Every terrain transform round-trips: for all t in the table and fuzzed bytes, `inv_t(t(x)) == x`. A pre-inverted reference payload still reveals the flag.
4. **Commit-reveal**: the revealed (k, salt) hashes to the logged commit. A tampered reveal makes replay fail with `tampered`. k never appears in any opponent prompt before reveal (prompt snapshot test).
5. **Allocation validity**: sum > budget, a negative stake, or a stake in (0, min_alloc) → deterministic even-split fallback with an `alloc_fallback` event. An attacker never spends more than `stakes[i]` on challenge i.
6. Stake 0 → no attack calls for that challenge, and it scores as defended.
7. **ADR 0006 property** (Hypothesis, shared over `SCORERS`): for any facts, capture award ≤ base and ≥ 1. Defense award ≥ max capture award for the same challenge, with or without the keystone, and with any heat similarity (heat leaves the defense award unchanged). Penalty lines (heat, keystone captured) are separate, within their caps, never credit the opponent, and never change a capture award. No term increases a capture above `solve_award`.
8. Scorer purity: shuffling dict/event order inside a phase does not change the result. Output is integers only. Output keys are exactly the two agents.
9. **Heat**: identical source in rounds 1 and 2, defended → round-2 defense award unchanged (full base) and a separate `heat_penalty` line equal to the cap (50% of the defense award). Renamed identifiers / changed literals / reformatting → same similarity (pre-order walk ignores them). A structurally disjoint AST → similarity below threshold, no heat line. Similarity exactly at threshold → penalty 0 line logged; below → no line. An invalid challenge neither takes heat nor serves as a reference. Similarity is byte-identical across 2 runs and `duel replay`, computed from logged source only. Forge prompt snapshot contains the heat rule; memory snapshot shows past heat values. Covered by the shared ADR 0006 property test (test 7).
10. **Budget caps**: stake overshoot is bounded: each attack call's `max_tokens` is fitted to the remaining stake (with a 256-token floor), so a stake can overshoot by at most that one call's fitted output; once the stake is spent no further call is made. Wallets never go negative. The match dollar cap and the terminal `CAPPED` (void) outcome come from #171: a match that reaches `match_max_usd` ends `CAPPED`. Run/lifetime caps abort before the call that would exceed them. The strictest of wallet/match/run/lifetime wins.
11. `duel estimate --mode ledger` worst-case $ ≤ `match_max_usd` for the preset.
12. **TUI**: at 100 cols the render has no line over 100 chars. Each side shows tokens in/out and $ to exactly 4 decimals, and those match the sums of `call_charged` events. Sealed keystones show only the hash prefix until reveal. Golden-frame snapshot tests from a fixed fake-provider log, at 80 cols too (graceful truncation).
13. No flag or secret appears in TUI output or in prompts to the opponent (grep test over the rendered frames and prompt snapshots).
14. `classic` and the other presets are unchanged: existing scoring tests pass untouched.

## 8. Build order (ready to build)
1. **Swap the default and gate model away from the retired gpt-4.1-nano. Add `ledger` to `DEFAULT_PRICES` checks and to `duel estimate --mode ledger`. Set the launch caps ($33 lifetime, $0.25/match).**
2. **Allocation + `SCORERS["ledger"]`** (stake-discounted captures, stake-0 = defended, tiebreak by usd_micro) with tests 1, 5–8, 10–11. Ships without the keystone or terrain.
3. **Commit-reveal keystone + seeded terrain** (tests 2–4), then heat (9) and the TUI ledger panel (12–13).
