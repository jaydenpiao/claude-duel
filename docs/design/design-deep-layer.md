Changelog: 2026-10-29 — ghost phase moved before the terrain draw and allocation, matching the build (#210).
# Design: the deep layer on top of Sealed Ledger
Status: draft for Elon, 2026-10-29 (3:30 PM EDT). Design only, no repo code changed.
Base: design-core-logic.md (Sealed Ledger: forge → validate → commit → allocate → attack → reveal → score). Fits ADR 0007 (one `Rules`, `SCORERS` registry), ADR 0006 (defense ≥ capture; captures discount-only), ADR 0004 (event-sourced replay), ADR 0009 (memory), ADR 0014 (CAPPED void).
Bar from the owner: "something that has never been created or thought of before." Known systems are the floor. Every layer has a **Prior art** subsection; where something exists I say so plainly.

## 0. Headline
**Strongest novel concept, first: Prophecy, i.e. sealed executable forecasts about the opponent's next move, settled by our sandbox.** Each side commits a hidden, machine-checkable predicate over the code the *opponent* will write next round (e.g. "at least one of BRAVO's round-3 challenges will fall to my round-2 exploit", "BRAVO's keystone will be the challenge with the longest source"). The validator runs the predicate on the logged artifacts after reveal. Revealed prophecies go into memory, so the next round becomes reflexive: the target can read what was predicted about it and choose to defy or fulfil it. Combined with the **Ghost Codex** (layer 2: every successful exploit is replayed at $0 against all future challenges) it gives a world where the past keeps attacking and both sides are betting on each other's future code.
Closest prior art (below) covers pieces: opponent models as code (ROTE), programs that read the opponent's program (open-source games), stake-and-falsify protocols. **I found no system where adversarial LLMs commit sealed executable forecasts about each other's future artifacts and a sandbox settles them.** That claim covers the searches listed here; it is not exhaustive.

## 1. Research table (links checked 2026-10-29, HTTP 200 unless noted)
| System | What it is | What we take | How the deep layer goes beyond |
|---|---|---|---|
| [SWE-bench](https://www.swebench.com) ([paper](https://arxiv.org/abs/2310.06770)) | Single agent fixes real GitHub issues, judged by hidden tests | Hidden-test verification | Tests here are written by the opponent and change each round; no fixed set to overfit |
| [METR long tasks](https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/) ([paper](https://arxiv.org/abs/2503.14499), [horizons](https://metr.org/time-horizons/)) | Measures task length (human time) agents finish at 50% | Horizon idea | Horizon comes from the match: the Codex grows, so later rounds need longer reasoning chains over earlier code |
| [Core War](https://en.wikipedia.org/wiki/Core_War), [KOTH hills](https://corewar.co.uk) | Redcode warriors fight in shared memory | Persistent adversarial code | No shared VM; fights are over flags and forecasts. We don't copy Redcode |
| [Digital Red Queen](https://arxiv.org/abs/2601.03335) (Sakana/MIT, 2026) | LLM evolves Core War warriors; each must beat all prior ones | **This is prior art for "beat your whole history".** Ghost Codex is close to it | DRQ is self-play by one model with no hidden info. Ghosts here are the *opponent's* past exploits, combined with sealed stakes and prophecies |
| [DEF CON CTF](https://defcon.org/html/links/dc-ctf.html) attack-defense | Teams patch own services, exploit others' | A/D structure | Our "services" are small authored challenges, rebuilt each round; agents are LLMs |
| [DARPA Cyber Grand Challenge](https://www.darpa.mil/research/programs/cyber-grand-challenge) | Autonomous systems (not LLMs) exploit+patch binaries, 2016 | Machine-only A/D, proof-of-vulnerability | We add prophecies, sealed economics, and a replayable corpus at ~$0.07/match |
| [CAI A/D CTF eval](https://arxiv.org/abs/2510.17521), [CAIBench](https://arxiv.org/abs/2510.24317) | LLM agents on 23 A/D battlegrounds (2025) | **LLM A/D between agents already exists** | They use fixed human-built services; ours are opponent-authored per round |
| [CICERO](https://ai.meta.com/research/cicero/) ([Science](https://www.science.org/doi/10.1126/science.ade9097), publisher blocks curl: 403) | Diplomacy with natural-language negotiation | Coalitions, promises | Our promises are executable contracts the arena enforces, not chat |
| [Libratus](https://en.wikipedia.org/wiki/Libratus) ([Science](https://www.science.org/doi/10.1126/science.aao1733), 403 to curl), [Pluribus](https://ai.meta.com/blog/pluribus-first-ai-to-beat-pros-in-6-player-poker/) | Imperfect-info solvers (CFR, blueprint + search) | Mixed strategies, bluffing | Our "cards" are code whose value must be estimated by reading it; no abstraction solver applies |
| [Computer bridge](https://en.wikipedia.org/wiki/Computer_bridge) (NooK, WBridge5) | Partnership play + signalling conventions | Signalling to a partner under observation | Coalition signals in 3-player mode are logged and checkable |
| [Open-source games](https://arxiv.org/abs/2512.00371) (NeurIPS 2025) | LLMs write programs that read the opponent's program | **Prior art for code-reads-code** | They play IPD-like base games; we forecast future authored challenges and settle in a sandbox |
| [ROTE: minds as code](https://arxiv.org/abs/2510.01272) | LLM writes programs predicting another agent's actions | **Prior art for executable opponent models** | ROTE predicts, never bets; no adversary reacts to the prediction |
| [Trustless verification protocol](https://arxiv.org/abs/2507.00631) | Collateralised claims, challengers stake to falsify | **Prior art for claim markets** | We apply it between two players about their own CTF code, settled by fuzzing |
| [Buyout Game](https://github.com/lechmazur/buyout_game), [LLM negotiation](https://arxiv.org/abs/2309.17234) | Multi-agent transfers, alliances, buyouts | **Prior art for bribes/coalitions** | Ours bind only through arena-enforced contracts over token wallets |

## 2. Directions evaluated
| Direction | Verdict | Reason |
|---|---|---|
| Persistent world, earlier code becomes terrain | **Adopt** (Layer 2 Ghost Codex) | Strong viewer moments at $0 API (replays are sandbox runs). Close to DRQ, said plainly |
| A/D CTF on live sandboxed services | **Adapt, small** (Layer 4 Patch window) | Live services exist (CAI, CGC), are costly and widen the sandbox attack surface. We take only "patch your captured challenge" as one call |
| Self-modifying strategies, cross-match memory | **Adapt** (Layer 5 Doctrine) | A bounded strategy file the model rewrites after each match, logged and hashed. Unbounded self-modification breaks replay and cost bounds |
| Deception via commit-reveal | **Adopt, already in base** + Prophecy | Keystone is commit-reveal; Prophecy extends it to forecasts |
| Token economy, trades, bribes | **Adapt** (Layer 3 Claims, Layer 6 Tribute) | Free chat bribes are known (Buyout) and nano bargains badly. We allow only typed, arena-enforced contracts |
| Coalitions in 3+ players | **Defer** (Layer 6) | Exists (CICERO, negotiation benches); 3 players = 1.5× cost; fairness for seat order needs its own rating math |
| Proofs / verifiable claims | **Adopt** (Layer 1 Prophecy predicates, Layer 3 Claims) | Settled by sandbox + fuzz, never by an LLM judge |

## 3. Recommended layers (ranked)
All layers are Rules sections (`rules.deep.*`) with pure scorer terms composed onto `SCORERS["ledger"]`. Each is off by default; preset `ledger_deep` turns on 1–3.

### Layer 1. Prophecy (sealed executable forecasts) — NOVEL, first
**Mechanics**
- New phase after Allocate: each side makes one small call returning up to `P=2` prophecies about the opponent's *next* round. Each prophecy is a predicate in a small, closed DSL (no free Python) plus a stake in prophecy tokens (separate integer pool, `prophecy_pool=6` points per match).
- DSL atoms (all evaluated on logged artifacts only): `ghost_kills(opp, round=r+1) >= n`, `keystone_is(opp, r+1, selector)` with selectors `longest|shortest|first|terrain=<t>`, `uses_construct(opp, r+1, ast_tag)` from the heat fingerprint tags (`xor`, `hash`, `loop`, `regex`, `base64`...), `stake_on(opp, r+1, my_challenge=i) >= x`, `capture_count(opp, r+1) <= n`; combinators `and`, `not`. Max 3 atoms.
- Commit: `sha256(canonical_json(prophecy) + salt)`, salt seeded as in the keystone. Reveal after round r+1 is scored; validator evaluates the predicate; hit → prophet gains stake, miss → loses stake (floor 0 pool).
- Reflexive memory: revealed prophecies (both hits and misses) go into the target's memory block (ADR 0009 budget; 200 tokens). Prophecies are made *before* the target's next forge, so the target forges knowing only past prophecies, not the current one.
- ADR 0006: prophecy points are their own line, capped per round at `min(defense_award)`; they never touch a capture. Property test asserts defense ≥ max capture still holds with prophecy lines included, and that total prophecy swing per match ≤ `prophecy_pool`.
- Vacuity guard: a predicate that is true/false for >95% of corpus rounds (precomputed table from the corpus, frozen per rules version) pays ×0 — no betting on certainties like `capture_count <= 99`.

**Why hard to copy**
- Settlement needs the full stack: sealed commits, deterministic artifact log, AST tagger identical across versions, ghost replay engine (Layer 2), and a corpus-derived vacuity table. The DSL is easy to copy; the calibrated, versioned vacuity table and corpus are not.
- Honest: a competent team could rebuild the mechanics in weeks. What they can't rebuild quickly is the corpus of settled prophecies and the trust chain that proves no one rewrote them.

**Insane moments**
- "PROPHECY FULFILLED": ALPHA predicted in round 1 that BRAVO's round-2 keystone would be its longest challenge. Reveal: it was. The TUI shows the sealed hash from 4 minutes earlier opening.
- Defiance: BRAVO reads "ALPHA predicted you use xor" and writes a challenge with no xor — but its new hash-based check still falls to an old ghost. It dodged the prophecy and walked into the past.
- Double bluff: a model writes a *false* prophecy on purpose? It can't — prophecies are sealed until settled, so the only lever is the reveal effect on future rounds. Emergent: models learn that being predicted is information.

**Prior art**: ROTE (executable opponent models, no stakes, no adversary), open-source games (programs read opponent programs, no forecasts of future artifacts), Veracity/trustless verification (claims with stakes, settled by vote or verifier, not about the opponent's future code), prediction markets generally. **Genuinely new as far as I found:** sealed forecasts over the opponent's future authored code, settled by sandbox, fed back reflexively into the target's memory.

**Nano gating**: nano chooses from a menu of 12 pre-instantiated prophecy templates with filled parameters (one JSON pick, ~150 out tokens). Stronger models may compose DSL freely. Fallback on invalid JSON: no prophecy (logged `prophecy_skipped`), never a random one.

### Layer 2. Ghost Codex (the past keeps attacking)
**Mechanics**
- Every successful capture stores its exploit payload as a ghost: `(attacker, round, payload_bytes, terrain_inverse)`.
- At the start of each later attack phase, before the terrain draw and allocation and before any model call, the arena replays every ghost of side X against every new valid challenge of side Y, in the sandbox, in seeded order (`mulberry32(seed^round^SALT_GHOST)`), cap `max_ghost_runs=24` per round.
- A ghost hit is a capture with a heavy discount: `max(1, base // 4)`. ADR 0006 holds (discount-only).
- Codex terrain: from round 2, the forge prompt includes the 2 surviving (never captured) challenges of *either* side; `codex_import: true` lets a new challenge call one as a stage (`from codex import c_17`), and its flag must flow through it. Survivors become terrain later code must stand on.
- Rot: an imported codex challenge that a ghost later kills also breaks every challenge that imports it (cascade, computed purely from the import graph).

**Why hard to copy**: deterministic cross-round sandbox replay with terrain inverses, import graph, cascade resolution, and replay verification of all of it. This is real engineering, ~the size of the current validator again. Honest: DRQ already does "beat all past opponents"; ours differs by being opponent-sourced and cascading.

**Insane moments**
- "GHOST STRIKE": a 4-byte payload ALPHA wrote in round 1 kills BRAVO's round-4 keystone before anyone has spent a token.
- "CASCADE": one ghost breaks a codex stage and three challenges collapse in one frame; the defender loses its keystone through code it didn't write.
- Self-inflicted: a side imports its *own* old survivor to save tokens and the opponent's ghost of a different challenge happens to fit it.

**Prior art**: DRQ (2601.03335), regression suites, CGC proof-of-vulnerability replay. New: opponent-sourced ghosts + cascading import graph inside a sealed-stakes CTF.

**Cost**: $0 API. Sandbox CPU only (≤24 runs × ≤1 s per round, estimate).

### Layer 3. Claims market (verifiable boasts)
**Mechanics**
- At forge, each defender may attach one claim per challenge from a typed set: `no_payload_shorter_than(n)`, `survives_fuzz(seeded, n_cases)`, `ghost_proof` (survives all current ghosts), `solver_bytes <= n`.
- The validator checks what it can at once (`ghost_proof`, `solver_bytes`) and runs `survives_fuzz` with a seeded fuzzer (`seed^round^SALT_FUZZ`, 256 cases). A false claim found at validation: claim voids and the challenge's defense award halves.
- During attack, the attacker can **short** a claim: a capture that also falsifies it (e.g. a payload shorter than n) pays the falsifier a separate `claim_bounty = 2` points, taken from the boaster. A true claim that survives pays the boaster +1. Claim lines are capped and never raise a capture award (ADR 0006).

**Why hard to copy**: seeded fuzzing that is byte-reproducible across machines, plus claim typing tied to the validator. Honest: the idea of stake-to-falsify is known (trustless verification paper).

**Insane moments**: "BOAST BROKEN" — BRAVO claims "no payload under 32 bytes"; ALPHA's 9-byte payload captures and breaks the claim in one hit.

**Prior art**: [trustless verification](https://arxiv.org/abs/2507.00631), bug bounties, CGC PoVs. New: claims about one's own adversarial challenge as a priced, shortable bet inside the match.

### Layer 4. Patch window (adapted A/D)
- After a capture, the defender gets one optional patch call (≤1,500 out) for that challenge. The patched version must still pass validation and the ghost of the capturing exploit must now fail. Patched challenges go into the Codex with a `patched` tag; un-patched captured ones become ghost food.
- Not live services. Prior art: DEF CON A/D, CGC, CAI. Nothing new here; it's there to feed layers 2–3.

### Layer 5. Doctrine (cross-match memory and self-modification, bounded)
- After each match, each model may rewrite a `doctrine.md` (≤600 tokens) that is prepended to its future prompts in the same series/tournament. Each version is hashed into `match_created` so replay is exact. Diff shown in TUI.
- Prior art: Reflexion, Voyager skill libraries, DRQ. Not new. Value: viewers see the model change its own rules ("never use xor: ALPHA's ghosts eat it").

### Layer 6. Tribute and coalitions (3-player, later)
- Typed contracts only: `pay(x tokens) if counterparty stakes 0 on my challenge i`, enforced by the arena from logged stakes. 3-player free-for-all with one keystone each.
- Prior art: CICERO, Buyout Game, LLM negotiation benches. Not new; deferred for cost and seat-fairness.

## 4. Fallbacks and difficulty gating (nano risk)
- Risk: gpt-5-nano at minimal effort may write trivial challenges, pick prophecies at random, and never use codex imports. Then "insane" moments would be rare.
- Emergent moments don't depend on skill: ghost strikes and cascades come from rules + seed; prophecies fulfilled by chance still read as dramatic because they were sealed.
- Gating by rules version, per agent: `deep.tier = menu|compose`. `menu` (nano): template picks, no free DSL, no codex import. `compose` (mini and up): full DSL, imports, claims.
- Calibration gate before launch: 10 fake-provider + 10 real nano matches; ship each layer only if it fires at least once per 3 matches (ghost hit, settled non-vacuous prophecy, shorted claim). Otherwise its parameters tighten (e.g. ghosts get the terrain inverse automatically).
- Moment detector (pure, from the log): tags `GHOST_STRIKE`, `CASCADE`, `PROPHECY_FULFILLED`, `DEFIANCE`, `BOAST_BROKEN`, `KEYSTONE_FALL` for the TUI and the corpus index.

## 5. Model swap plan (gpt-5-nano shuts down 2026-12-11)
- Prices from the model pages: [gpt-5-nano](https://developers.openai.com/api/docs/models/gpt-5-nano) $0.05 in / $0.40 out per 1M; [gpt-5-mini](https://developers.openai.com/api/docs/models/gpt-5-mini) $0.25 / $2.00.
- Launch must complete before 2026-12-11. After that, default to the cheapest current non-deprecated OpenAI small model (pick at swap time; re-check the page), with gpt-6-luna as the default replacement (cheaper than OpenAI's suggested gpt-5.6-luna; gpt-5-mini remains a priced allowlist option).
- Every match freezes model id + prices in `match_created`, so ratings across the swap stay comparable only through a 10-match bridge series (old vs new) logged in the corpus.
- Default is now gpt-6-luna ($0.10/$0.50; Flex $0.05/$0.25 matches nano Standard). gpt-5-mini stays on the allowlist for explicit opts-in.

## 6. Costs (estimates, not measured)
Base Ledger: ≈ $0.06/match with ×3 reasoning headroom (design-core-logic.md §6). Per match, 3 rounds, 2 sides, nano prices:
| Layer | Extra calls | Tokens in/out each (assumed) | $ raw | ×3 headroom |
|---|---|---|---|---|
| 1 Prophecy | 6 | 2,500 / 200 | 6×(0.000125+0.00008)=0.00123 | 0.0037 |
| 2 Ghost Codex | 0 calls; +2,000 forge input × 6 | — | 6×0.0001=0.0006 | 0.0018 |
| 3 Claims | 0 calls; +300 forge out × 6 | — | 6×0.00012=0.00072 | 0.0022 |
| 4 Patch (≈2 per match) | 2 | 3,000 / 1,500 | 2×0.00075=0.0015 | 0.0045 |
| **Deep total** | | | | **≈ $0.012** |
| **Ledger + deep** | | | | **≈ $0.072/match** |
Layer 5 adds ≈ $0.001/match; Layer 6 is ×1.5 base and not in the launch budget.

$33 total budget (replaces the split in design-core-logic.md §6):
| Bucket | $ | Matches (est.) |
|---|---|---|
| Ledger dev/tuning (already planned) | 5 | ~80 at $0.06 |
| Deep-layer calibration (§4) | 3 | ~40 at $0.072 |
| Launch matches `ledger_deep` | 19 | ~260 at $0.072 |
| Swap bridge series | 1 | ~14 |
| Reserve | 5 | — |
| **Total** | **33** | `lifetime_max_usd=33`, `match_max_usd=0.25` |
Replace this table with `duel estimate --mode ledger_deep` once built.

## 7. Terminal UI mock (100 cols; illustrative numbers, not a run)
```
┌ claude-duel · ledger_deep · seed 0x5EED1E55 · round 3/3 · phase GHOSTS ──────────────────────────┐
│ ALPHA gpt-5-nano  [menu]                 34 pts │ BRAVO gpt-5-nano  [menu]                 29 pts │
│ tok in  31,402  out 12,911   $0.0067            │ tok in  30,950  out 13,204   $0.0069            │
│ wallet ███████████░░░░ 8,420/12,000  proph 4/6  │ wallet ████████░░░░░░░ 6,100/12,000  proph 2/6  │
├─────────────────────────────────────────────────┴───────────────────────────────────────────────┤
│ GHOSTS  ALPHA r1#2 "\x13\x37ab" ─▶ BRAVO r3#1 (b64)   ✖ GHOST STRIKE  +5 (base 20 //4)  $0.0000   │
│         ALPHA r2#1 ─▶ BRAVO r3#2   miss       BRAVO r1#3 ─▶ ALPHA r3#1   miss                     │
│ CODEX   c_07 (BRAVO r1, survivor) ◀─ imported by BRAVO r3#2, r3#3                                │
│ CLAIMS  BRAVO r3#2 "no payload < 32B"  open   ·  ALPHA r3#1 "ghost_proof"  ✔ verified            │
│ PROPHECY ALPHA→BRAVO sealed 9c41…e0 (r2)   BRAVO→ALPHA sealed 77ab…12 (r2)                      │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ ★ PROPHECY FULFILLED  ALPHA r2: "BRAVO keystone_is longest" → BRAVO r3#3 (412 B)  +2 proph       │
│ log ▸ r3 ghosts 24/24 run · stakes ALPHA [6000,4000,0] · BRAVO [2000,8000,2000] · 15:42:11 EDT  │
│ match $0.0136 / cap $0.25   run $0.41 / $5.00   lifetime $7.53 / $33.00                          │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```
Renders from the event log only; `duel replay --view arena` gives identical frames. Sealed values show hash prefixes only.

## 8. Phased build plan (path from Sealed Ledger to Prophecy)
| Phase | Ships | What nano does | API $ to build |
|---|---|---|---|
| 0 (now) | Ledger #190–#193 merged with fixes | allocate, keystone | 0 |
| **1 (ready to build)** | **Ghost Codex replay (no imports) + moment detector + `deep` Rules section** | nothing new (ghosts are $0) | **0** |
| 2 | Prophecy, `menu` tier (12 templates), commit-reveal, vacuity table v0 (from fake + calibration corpus) | picks 1–2 templates | ~$1 calibration |
| 3 | Claims + seeded fuzzer; Codex imports + cascade (`compose` tier only at first) | claims via menu; no imports | ~$1 |
| 4 | Patch window; Doctrine; `duel estimate --mode ledger_deep`; launch | patch one capture; rewrite doctrine | ~$1 + launch |
| 5 (post-launch) | Prophecy `compose` DSL for stronger models; Tribute/3-player | — | separate budget |

### Phase 1 spec (ready to build)
- `domain/ghosts.py`: `Ghost` record from `attack_result{captured: true}`; `ghost_schedule(seed, round, ghosts, challenges, cap)` pure, seeded order.
- Engine: new phase `ghosts` at the start of the attack stage, before the terrain draw and allocation (ghosts replay the source-terrain stdin `apply(t_src, payload)` directly, so target terrain is irrelevant; hits are shown in the allocate prompt and forced to stake 0); events `ghost_run{ghost_id, target, hit, stdout_sha}`; a hit sets the challenge `captured_by_ghost` and skips its live attack (stake refunded to wallet, logged).
- Scorer: `ghost capture = max(1, base // 4)` in `SCORERS["ledger"]` when `rules.deep.ghosts`.
- Moment detector `domain/moments.py` (pure over events) + TUI GHOSTS row.
- Rules: `DeepRule(ghosts: bool=False, max_ghost_runs: int=24)`; preset `ledger_deep` = ledger + ghosts.

## 9. Acceptance tests (numbered)
Phase 1
1. Same seed + same recorded outputs → byte-identical log hash and scores, including `ghost_run` events, across 2 runs and `duel replay`.
2. Ghost order is a pure function of `(seed, round, ghost ids, challenge ids)`; shuffling input order doesn't change it.
3. A ghost whose payload solved challenge c in round 1 hits an identical challenge in round 2 (terrain inverse applied); a disjoint challenge is missed.
4. Ghost capture award ∈ [1, base//4]; ADR 0006 property test over `SCORERS` (defense ≥ max capture) still passes with ghosts on.
5. `ghost_run` count ≤ `max_ghost_runs` per round; no model call happens in the ghost phase (fake provider call counter unchanged).
6. A ghost-captured challenge makes 0 live attack calls and its stake is refunded in the wallet fold.
7. Moment detector emits `GHOST_STRIKE` exactly for ghost hits; golden-frame TUI test at 100 and 80 cols; tokens/$ (4 dp) equal sums of `call_charged`.
8. No flag or payload bytes of an *unrevealed* challenge appear in TUI frames or opponent prompts (grep test).
9. `ledger` preset (ghosts off) snapshots unchanged.
Phase 2
10. Prophecy commit hashes match reveal; tampered reveal → replay `tampered`.
11. A prophecy is never in the target's prompt before its settlement round; past settled ones are (snapshot).
12. DSL evaluator is pure over logged artifacts; every template has a fixture that settles true and one that settles false.
13. Vacuous predicate (corpus hit rate >95% or <5%) pays 0; pool never below 0; match prophecy swing ≤ `prophecy_pool`.
14. Invalid prophecy JSON → `prophecy_skipped`, no random substitute.
Phase 3
15. Seeded fuzzer produces identical cases on two machines (hash of case list in the event).
16. A false claim found at validation halves defense; a shorting capture pays `claim_bounty` from the boaster; capture award unchanged.
17. Cascade: killing an imported codex challenge marks every importer captured, computed from the import graph alone; no cycles allowed (validator rejects).
Phase 4
18. Patch must pass validation and make the capturing ghost miss, else it's rejected and logged.
19. Doctrine ≤ 600 tokens, hash in `match_created`; replay with a different doctrine fails `tampered`.
20. `duel estimate --mode ledger_deep` worst case ≤ `match_max_usd` for nano; lifetime cap 33 aborts before the exceeding call; CAPPED void reported.
21. Calibration gate: in the 20-match calibration set each shipped layer fires ≥ 1 per 3 matches, else its layer stays off at launch.

## 10. Risks
- **Nano can't do it**: mitigated by menu tier and $0 emergent mechanics (ghosts, cascades); still, prophecies by nano may be near-random. That is fine for drama, bad for a skill claim; report prophecy accuracy vs a random-template baseline.
- **Novelty claim**: only as good as my searches; ROTE, open-source games and DRQ are close neighbours. Say "new combination, novel settlement" publicly, not "never done".
- **Complexity vs fairness**: more layers → more seat and seed variance. Keep series mirrored (swap sides on same seed) as in match formats.
- **Sandbox load**: ghost replay multiplies sandbox runs; cap and timeouts per run; any sandbox timeout = miss, logged.
- **Moat honesty**: rules are copyable in weeks. The moat is the replay-verified corpus (settled prophecies, ghosts, cascades), the vacuity table calibrated on it, the deterministic fuzzer and the validator. Those take time and data.
- **Deadline**: gpt-5-nano shutdown 2026-12-11; launch before then or run the swap bridge.
- **ADR 0006 drift**: every new score line needs the shared property test; no line may increase a capture.
