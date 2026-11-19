# Wiring the Prophecy core into the engine (design-only)

Status: spec, Oct 31 2026. Author: Duel Game Designer. Cost to write: $0.
Code read: `origin/main` (#214 `src/claude_duel/domain/prophecy.py`, merged) and `origin/ledger-deep` (#210, which already carries the Ledger stack 192–199 merged in; see status-current.md).
SHAs: origin/main 29d40a0, origin/ledger-deep 1d863a5.
All `engine.py`/`models.py`/`events.py`/`scoring.py`/`memory.py` line numbers below are from `origin/ledger-deep`. `prophecy.py` line numbers are from `origin/main`.

## 0. TL;DR
- **Seal** inside the existing **allocate call** of round r (no new call). It goes right after the keystone seal, so the order in `_allocate` is keystone → prophecy → stakes. The target is the **opponent's round r+1**. There's no seal in the last round.
- **Reveal and settle** in `_score` of round r+1. This happens after the keystone reveal and before `score_round`, and the result is a **separate `prophecy` line** in `round_scored`. Settlement is pure evaluation over logged artifacts. It needs no sandbox run and no API call, so `replay --verify` costs $0.
- Only the **commit** is logged at seal time. The reveal is opened by brute force over the menu (12 templates × 3 stakes = 36 candidates per slot), the same way `open_keystone` works.
- Prophecy is its own line: ±stake, per-round cap, match pool 6, never below 0. It never touches captures, defenses, the keystone ×2, heat or ghost awards, so ADR 0006 holds.
- Config: `DeepRule.prophecy: ProphecyRule | None = None`, off by default, with no new preset.
- **RULES_VERSION:** fold it into the one pending combined bump (1→2) with the Ledger stack, not a second bump. **SCHEMA_VERSION** does need 2→3 for the new event kinds and fields.
- Cost: about +$0.0002 per match raw (+$0.0006 with ×3 headroom), versus the $0.0037 budgeted in §6 for a separate call. The calibration gate is about $0.75 (10 fake at $0 + 10 nano) from the existing "deep calibration $3" bucket. It needs **jayden's go**.

## 1. Current turn loop (what we hook into)
`Stage` handlers: `engine.py:175-183` (BUILD → VALIDATE → ATTACK → SCORE per round).
ATTACK, `engine.py:526-556`:
1. `_ghosts` (`:527`, `:578-600`). The ghost hits are public in the allocate prompt and force stake 0.
2. `_draw_terrain` (`:529`, `:558-576`). It's public and identical for both sides, and it's drawn before the keystone commit.
3. `_allocate` per agent (`:531`, `:618-673`). This is one paid call that returns `{"stakes": [...], "keystone": i}` (`agents.py:319`, `:366`). Then `KEYSTONE_SEALED` (`:668-672`) and `STAKES_SET` (`:673`).
4. Live attacks or ghost/skip records (`:533-556`).

SCORE, `engine.py:769-793`: keystone reveal (`:772-778`), `score_round` (`:779-788`), `capture_points`, `heat_of_state` (`:789-791`), then `ROUND_SCORED{delta, points, heat}` (`:793`; payload `events.py:212-216`).

## 2. Hook points and ordering

### 2.1 Round r, ATTACK stage: seal
Order inside `_attack` for round r (new step marked ★):
```
ghosts(r) → terrain(r) → for each agent: allocate call(r) ─┬─ settle stakes
                                                            ├─ KEYSTONE_SEALED(r)
                                                            ├─ ★ PROPHECY_SEALED(r→r+1) ×≤2  or  PROPHECY_SKIPPED
                                                            └─ STAKES_SET(r)
→ attacks(r)
```
- **Why the allocate call:** it's the precedent the keystone set ("sealed with the allocation: same call, so it costs nothing extra", `engine.py:668`). It runs after ghosts and terrain, so the prophet has seen this round's board. It also avoids the separate call priced in deep-layer §6.
- **Prompt addition** (`agents.allocate_prompt`): a PROPHECY block appears only when `rule.deep.prophecy` is set and `r < rounds`. It holds the 12 template names with one plain line each (generated from `TEMPLATES`, `prophecy.py:101-114`), the prophet's remaining pool, and the reply shape. The JSON reply grows one *optional* key: `{"stakes": [...], "keystone": i, "prophecies": [{"template": "...", "stake": 1-3}]}` (max 2).
- **Parsing is independent:** `parse_allocation` keeps `stakes` and `keystone` required. It passes the raw `prophecies` sub-object to `prophecy.pick` (`prophecy.py:117-125`) after `json.dumps`. A bad prophecy never makes the stakes fall back, and a bad allocation never voids a valid prophecy (if the stakes fall back, the prophecy still seals if it parsed).
- **Salt:** `prophecy_salt(seed, r, agent, slot)` must be domain-separated from `keystone_salt` (prefix `"prophecy:"`). Otherwise the keystone reveal in round r would publish the prophecy's salt a round before its settlement.
- **Commit:** `prophecy.commit(p, salt)` (`:89-90`). Only the commit is logged.
- **Idempotent resume:** the call is already guarded by `stakes` (`:622-623`). Seal events go before `STAKES_SET`, so a resume after `STAKES_SET` never re-calls. A crash between them is handled as for the keystone: on resume the whole allocate re-runs.

### 2.2 Round r+1, SCORE stage: reveal and settle
```
attacks(r+1) done → KEYSTONE_REVEALED(r+1) → ★ PROPHECY_REVEALED (prophecies sealed in r)
→ ★ build RoundArtifacts(target, r+1) → ★ prophecy.settle → PROPHECY_SETTLED
→ score_round → ROUND_SCORED{delta (incl. prophecy), points, heat, prophecy}
```
- Reveal comes **after** the keystone reveal, because `keystone_is` atoms need `keystone_targets(state, r+1)`.
- `open_prophecy(commit, salt)` iterates `TEMPLATES × {1,2,3}` in sorted order and returns the unique match. No match means `TamperedLogError`, as `open_keystone` does.
- **Artifacts builder** (new, pure, `domain/prophecy_state.py` or in `prophecy.py`): `artifacts_of(state, target, round) -> RoundArtifacts` (`prophecy.py:47-54`), built only from the log:
  - `challenges`: the target's **valid** round-(r+1) challenges sorted by id (the same positional order as stakes; "first" = index 0). `size = len(source.encode())`, `tag = memory.technique(source)` (`memory.py:20-36`).
  - `keystone`: the revealed keystone challenge id or None (`stakes.keystone_targets`).
  - `captures`: the target's **live** solved attacks in r+1 (`attack.ghost is None`).
  - `ghost_kills`: how many of the target's r+1 challenges are in `ghosts.round_hits(state, r+1)`.
  - `stakes`: the target's `STAKES_SET(r+1)`, or () if it's missing.

### 2.3 Visibility
| Who / when | Round r (sealed) | Round r+1 before settle | After settle |
|---|---|---|---|
| Prophet's prompts | its own picks (in its own allocate reply) | memory line: "you sealed: <template> stake k" | settled: template, hit/miss, ±points |
| Target's prompts | **commit hash only, or nothing** (recommend nothing: a hash is noise to a model) | **nothing** (test 11) | template + hit/miss in memory (`memory._round_block`, `memory.py:65-69`, the same place revealed keystones go) |
| Log / TUI | commit (`9c41…e0`) | commit | full prophecy, salt, hits |
| Flag guard | `assert_no_flag` already covers the allocate prompt (`engine.py:653-654`) | — | — |

A target that defies or fulfils a prophecy can do so only from round r+2 on, using the settled history. That reflexive loop is the novelty claim (deep-layer §1, line 8).

## 3. Events and log fields (all payloads `extra="forbid"`, `events.py` style)
| Event | Fields (type) | Example |
|---|---|---|
| `prophecy_sealed` | agent: str, round: int (sealed in), target_round: int, slot: int (0-1), commit: str (64 hex) | `{"agent":"a","round":1,"target_round":2,"slot":0,"commit":"9c41…e0"}` |
| `prophecy_skipped` | agent: str, round: int, reason: Literal["malformed","truncated","no_reply","allowance","pool_empty"] | `{"agent":"b","round":2,"reason":"malformed"}` |
| `prophecy_revealed` | agent: str, round: int (= target_round), slot: int, template: str, stake: int, salt: str | `{"agent":"a","round":2,"slot":0,"template":"keystone_longest","stake":2,"salt":"…"}` |
| `prophecy_settled` | agent: str, round: int, hits: list[bool], vacuous: list[bool], void: bool, delta: int | `{"agent":"a","round":2,"hits":[true],"vacuous":[false],"void":false,"delta":2}` |
| `round_scored` (+field) | `prophecy: dict[str,int]` agent → delta, default `{}`, **included in `delta`** (like `heat`) | `"prophecy":{"a":2,"b":-1}` |

`MatchState` gains `prophecies: dict[str, ProphecySeal]` (key `"<target_round>:<agent>:<slot>"` → commit, revealed template/stake/salt) and `prophecy_points/prophecy_swing: dict[str,int]` (running totals for `settle(points=, swing=)`). Apply handlers mirror `_sealed` for keystones.
There's no `prophecy_sealed` in the last round, so `round == rounds` never seals.

### 3.1 Replay and `duel replay --verify` ($0)
- **Replay** (ADR 0004) recomputes the following and raises `tampered` on any mismatch: the salt from the seed, `open_prophecy(commit, salt)` against `prophecy_revealed`, `artifacts_of` from the log, `settle(...)` against `prophecy_settled.delta/hits`, and the `round_scored.prophecy` line.
- **`--verify`** (extend `app/ghost_verify.py:17` into a `verify_*` list) adds `verify_prophecies(state, events)`. It reports: a seal without a reveal for a round that was scored, a reveal without a seal, a settled delta that differs, and a `prophecy_skipped` alongside a seal for the same agent and round. There are no sandbox runs because the predicates read logged artifacts only, so it really is $0, unlike the ghost re-runs.

## 4. Scoring
Defaults (`ProphecyRule`): `max_per_round=2`, stakes 1–3 (`prophecy.py:38`), `pool=6` per agent per match, `round_cap=3`.
- `delta = settle(...)` (`prophecy.py:132-151`): hit +stake, miss −stake, vacuous 0. It's clamped to ±round_cap, never takes the running prophecy points below 0, and keeps |swing| ≤ pool.
- **Effective round cap** = `min(rule.round_cap, min defense_award of the target's valid r+1 challenges)` (deep-layer §4 line 49). This keeps a prophecy from ever being worth more than the cheapest defense.
- **ADR 0006:** captures stay in [1, base] and defense ≥ capture. Prophecy is a separate line on the prophet's score and never edits `_captures` (`scoring.py:79-89`) or defense. Property test: for every generated round, the inequalities hold with `prophecy` lines included, and the match swing is ≤ pool.
- **Keystone ×2:** never applied to prophecy. A `keystone_is` hit pays only the stake.
- **Heat:** independent. Heat is charged to the author, and prophecy is credited to the prophet.
- **Ghosts (base//4, `ghosts.py:62-64`):** independent. A ghost capture counts in `ghost_kills`, not `captures`.
- **Void:** the target has zero valid r+1 challenges, or the match ends (cap or failure) before r+1 is scored. The prophecy pays 0 with `void: true` and the pool isn't consumed. This blocks `no_hash` or `no_captures` winning by default against an empty round.
- **Tiebreak (#203, `series.game_winner`: fewer dollars, then fewer tokens):** unchanged. Prophecy points are inside `delta` and so inside scores, and prophecy tokens are inside the allocate call's charge. No new tiebreak key.
- **Vacuity table:** `rates` comes from a versioned constant `PROPHECY_RATES[rates_version]`. v0 is `{}`, so nothing is vacuous until calibration produces v1. `rates_version` sits in `ProphecyRule`, so replay is deterministic.

## 5. Mode and rules abstraction (ADR 0007)
```python
class ProphecyRule(_Frozen):
    # a `tier` field ("menu" or "compose") arrives with phase 5; one tier needs no field
    max_per_round: int = Field(default=2, ge=1, le=2)
    pool: int = Field(default=6, ge=1, le=20)
    round_cap: int = Field(default=3, ge=1, le=6)
    rates_version: int = 0


class DeepRule(_Frozen):  # models.py:126-130
    ...
    prophecy: ProphecyRule | None = None  # off by default
```
- A validator requires `deep is not None` (which already implies a ledger) whenever `deep.prophecy` is set.
- There's **no new preset** and no copy of `ledger_deep` (`modes.py:41`). Turn it on with a rules override (`--rules deep.prophecy={}` or a config file). Calibration uses the same override.
- `all_in` hardcodes `n=6_000` (`prophecy.py:112`). It should become `attack_tokens // 2` at template instantiation so it tracks `LedgerRule.attack_tokens`, or the change gets pinned in a test. With the default 12,000 tokens the result is the same.

## 6. RULES_VERSION and SCHEMA_VERSION
- The rule in `models.py:133` is "bump when a Rules default changes meaning". Prophecy defaults to **off**, so on its own it **doesn't require** a bump.
- **Decision: one combined bump.** The Ledger stack's pending bump (1→2, queued after #247 in the status file) should land once, and prophecy wiring should be in or before it. If the wiring slips past the bump, **don't bump again**, because default-off adds no change in meaning. A second bump is needed only if a later PR turns prophecy on in `ledger_deep`.
- **SCHEMA_VERSION 2→3 is required** (`events.py:61`). Four new event kinds plus `RoundScored.prophecy` would be rejected by a v2 reader under `extra="forbid"`. The Ledger stack also added `keystone_*`, `terrain_drawn`, `ghost_run` and `RoundScored.points/heat` without a schema bump. Recommend one 2→3 covering both, with the same timing as the combined RULES_VERSION bump.

## 7. Cost (gpt-5-nano, Responses API, minimal effort)
Prices are as used in deep-layer §6: about $0.05/M in and $0.40/M out.
| Item | Tokens | $ per match |
|---|---|---|
| Menu block in 4 allocate prompts (2 sides × rounds 1-2 of 3) | +350 in each = 1,400 | 0.00007 |
| Prophecy JSON in the reply | +40 out each = 160 | 0.000064 |
| Settled-prophecy memory lines in later prompts (about 10 prompts × 100) | 1,000 in | 0.00005 |
| **Total raw / ×3 headroom** | | **≈ $0.0002 / $0.0006** |

The ×3 figure is below §6's $0.0037 (separate call), so the ≈ $0.072 per match ledger+deep estimate stays unchanged or drops slightly. The $0.25 match cap isn't affected.
**Calibration gate** (deep-layer §4 line 112): 10 fake-provider matches ($0, they run in CI and seed `PROPHECY_RATES` v0 sanity) plus 10 real nano matches at about $0.073 ≈ **$0.75**. That's from the "Deep-layer calibration $3" row (§6 line 137) and leaves $2.25 for phases 3-4. The $33 table is unchanged. Pass if, in at least 1 of every 3 matches, a non-void, non-vacuous prophecy settles, and nano's malformed rate is ≤ 20%. Report hit rate versus a random-template baseline (§9 risk). **Needs jayden's own go** (status: hard limits).

## 8. Failure modes
| Case | Behaviour |
|---|---|
| Malformed or out-of-menu `prophecies` (bad JSON, unknown template, stake outside 1-3, >2 items) | `prophecy_skipped{reason:"malformed"}`, no random substitute (test 14). Stakes and keystone unaffected. |
| `prophecies` key absent | A deliberate no-prophecy: log nothing, because it's a legitimate choice. |
| Truncated reply or provider timeout/error after retries | The existing allocate fallback (even split) plus `prophecy_skipped{"truncated"|"no_reply"}`. |
| Allowance empty (`allowance.empty`, `engine.py:657`) | `prophecy_skipped{"allowance"}` |
| Pool exhausted (swing == pool) | The menu is omitted and the reply is ignored: `prophecy_skipped{"pool_empty"}` |
| The opponent never produces the target (all r+1 challenges invalid or the build fails, or the match ends before r+1 is scored) | `void`, 0 points, pool untouched |
| Last round | No menu, no seal |
| Tampered reveal | Replay raises `tampered` (test 10) |
| **Self-fulfilling forecasts** | Prophecies are about the *opponent*, so the prophet can only fulfil one through its own play. In the current menu that only helps when the play is good anyway (e.g. `no_captures` by writing harder challenges, `ghost_kill` from its own earlier captures). **Invariant (test):** no template's hit is made more likely by the prophet *losing* points. So never add `capture_count_ge`, "opponent defends ≥ n" or "opponent's stake on my challenge ≤ n". The swing is also tiny (pool 6) next to a round's defense awards. |
| Collusion (both sides predict "no_captures" and both write impossible challenges) | Blocked by the solvability gate (SV-5, R1) and by defense ≥ capture. Flag it in the moment detector if both sides hit `no_captures` in the same round. |

## 9. Acceptance criteria
1. With `deep.prophecy=None`, every existing snapshot, replay and test is byte-identical, and so is the allocate prompt.
2. With it on: there's no extra provider call (`calls` per match unchanged, which a fake-provider count test checks), and each sealing round logs ≤2 `prophecy_sealed` or one `prophecy_skipped` per agent.
3. The target's prompts in round r+1 never contain the template text or the commit (deep-layer test 11, a substring check across all prompts).
4. Seal → reveal → settle round-trips. Replay recomputes `round_scored.prophecy` exactly, and `--verify` flags each injected tamper (bad salt, changed stake, edited delta, deleted reveal).
5. ADR 0006 property test with prophecy lines; total swing ≤ pool; points never below 0; effective round cap ≤ min defense award.
6. A void case pays 0 and doesn't consume pool.
7. Malformed or truncated prophecy JSON leaves stakes and keystone untouched, and the reverse also holds.
8. The salt is domain-separated: keystone and prophecy salts differ for the same (seed, round, agent).

## 10. Test ideas
- `test_prophecy_wiring.py` (fake provider): a scripted 3-round match. A seals `keystone_longest` in r1, and B's r2 keystone is the longest, so the result is +2. B sends malformed JSON and gets `prophecy_skipped`. There's no seal in r3.
- Parametrized open/brute force: all 36 (template, stake) pairs open uniquely and a wrong salt raises.
- Artifacts builder: invalid challenges are excluded, ghost captures are counted in `ghost_kills` and not `captures`, and stakes are missing when the target fell back.
- Hypothesis: random rounds plus random prophecies, checking the ADR 0006 inequalities and the pool and floor bounds.
- Snapshot: the allocate prompt with the PROPHECY block, and a memory block with a settled prophecy for both prophet and target.
- `--verify`: corrupted logs, one per tamper kind.
- Self-fulfilment invariant: a table test over `TEMPLATES` showing that no atom's hit rises when the prophet's own capture or defense outcome worsens.

## 11. PR breakdown
| PR | Content | Depends on |
|---|---|---|
| **P1 prophecy-domain-glue** | `ProphecyRule` in `DeepRule`; `prophecy_salt`, `open_prophecy`, `artifacts_of`, void rule, effective round cap; 4 payloads + `RoundScored.prophecy` + apply handlers + `MatchState` fields; `all_in` from `attack_tokens`; `PROPHECY_RATES` v0 `{}`. Pure, no engine change. | #214 (merged), #210 (`DeepRule`, ghosts) → based on `ledger-deep` |
| **P2 prophecy-engine** | Allocate prompt block + optional `prophecies` parse; seal in `_allocate`; reveal, settle and line in `_score`; memory lines (`memory._round_block`); fake provider picks a deterministic template. | P1, #203 (tiebreak; for unchanged winner tests) |
| **P3 prophecy-verify-tui** | `verify_prophecies` in `--verify`; arena ticker lines `PROPHECY … sealed` / `★ PROPHECY FULFILLED` (deep-layer §7 mock); moment detector `PROPHECY_FULFILLED`, `DEFIANCE`. | P2, #211 (deep-tui) |
| **P4 prophecy-calibration** | Script to run 10 fake + 10 nano and compute rates v1 and the gate report; the nano half runs only on jayden's go. | P2 |
| (bump) | RULES_VERSION 1→2 + SCHEMA_VERSION 2→3, one PR, after #247 and the Ledger stack | the Ledger stack, then P1 if possible |

The order follows the earlier build list: the Ledger stack → #203 → #210 → P1 → P2 → (#211) → P3 → P4.

## 12. Open questions
1. **Template conflict with the variety design.** The variety design (R5 static bans) recommends banning hash libraries in challenge code. That would make `uses_hash` and `no_hash` vacuous (0% or 100% hit). Swap them for `uses_maze` and `uses_state_machine` (tags already exist, `memory.py:23-24`) in P1. jayden or Elon should confirm, because it edits #214's menu.
2. **Seed in the log.** `match_created.seed` is logged, so a log reader (the TUI viewer) can derive salts and brute-force the 36 candidates before settlement. The same is true of keystones today. Models never see the log, so the game is sound. The only loss is TUI suspense for someone reading the raw JSONL. Accept it, or add a per-match secret salt revealed at match end (that changes replay inputs, so the safest choice is to defer).
3. Should the target see the commit hash in its prompt (theatre) or nothing (recommended)?
4. Do we want **stronger models on the compose tier** before launch? Recommend no (phase 5).
5. Is the defense award small enough in round 1 that `min(defense_award)` makes the cap 1? That would mute prophecy early. Check against a fake-match snapshot in P1 and, if so, use `max(1, …)` with `round_cap` as given.
