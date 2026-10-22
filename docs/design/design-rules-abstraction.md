# ADR 0007 (draft): Modes, one rules abstraction with pluggable scoring, clocks and budgets
Status: proposed (design only, 2026-10-22). Builds on ADR 0001 (hexagonal), 0004 (event-sourced replay) and **0006 (hidden answers, defense-first scoring, on the in-flight `harder-challenges` worktree)**.

> **Lead decisions (2026-10-22, 1:08 AM PDT), now binding:** game clocks are measured in tokens, and real latency is recorded only as a metric; cross-vendor efficiency is scored in dollars, with tokens shown alongside; the default match deadline is 900 s; per-agent wallets sit under the existing cost caps and the strictest limit wins; the build order is approved and starts after ADR 0006 merges.

## Context
- Today one hard-coded mode exists: `Rules` (domain/models.py) + `score_round` / `solve_award` / `defense_award` (domain/scoring.py) + `BudgetLimits` (app/budget.py). The state machine's only knob is `rules.rounds`.
- The backlog wants time controls (#13), between-round memory (#19), token-efficiency duels, and match formats. Making each a copy of `Rules` + `score_round` would break the "no duplicated logic" gate (pylint duplicate-code) and the replay guarantee.
- ADR 0006 sets `defense_points >= solve_points` (property-tested), adds `Rules.difficulty_gate`, `ChallengeSpec.solver` and `match_stats`, and keeps scoring **a pure function of the event log**. That invariant is the anchor of this ADR.

## Decision
1. **`Mode` is data, not code.** `Rules` gets `mode: str = "classic"` plus nested, frozen, optional sections. No subclass per mode:
   ```python
   class ScoringRule(_Frozen):          # which pure scorer + its parameters
       kind: Literal["classic", "decay", "efficiency", "timed"] = "classic"
       solve_points: int = 10; defense_points: int = 10; invalid_penalty: int = 5   # moved from Rules (aliases kept)
       round_weight: Literal["linear", "flat"] = "linear"
       decay: DecayParams | None = None; efficiency: EfficiencyParams | None = None; timed: TimedParams | None = None
   class Clock(_Frozen):                 # design-time-controls.md
       call_deadline_s: float = 60; match_deadline_s: float = 900          # safety, wall time (metric only)
       bank_tokens: int | None = None; increment_tokens: int = 0;           # game clock, tokens only kind: Literal["none", "fischer", "bronstein"] = "none"
   class MemoryRule(_Frozen):            # design-memory-strategy.md
       kind: Literal["none", "summary", "summary+scratchpad"] = "none"; max_chars: int = 1500
   class Rules(_Frozen):
       mode: str = "classic"; rounds: int = 3; ...existing fields...
       scoring: ScoringRule = ScoringRule(); clock: Clock = Clock(); memory: MemoryRule = MemoryRule()
       token_cap: TokenCap | None = None   # design-efficiency-modes.md (wallet; strictest of wallet/match/run/lifetime wins)
       framing: FramingRule = FramingRule() # design-stakes-modes.md (prompt framing as a measured variable)
   ```
   A named mode is a preset in `domain/modes.py`: `MODES: dict[str, Rules]` (`classic`, `blitz`, `efficiency`, `adaptive`, ...). Config chooses `[rules] mode = "blitz"` and may override any field; `load_settings` merges preset then overrides, then validates. Presets are just `Rules` instances, so there is nothing to copy-paste.
2. **Scoring is a registry of pure functions** in `domain/scoring.py`:
   `Scorer = Callable[[RoundFacts, ScoringRule], dict[str, int]]` with `SCORERS: dict[str, Scorer]`.
   `RoundFacts` is a frozen view built only from state that was itself built from events (challenges, validations, attacks, attempts, and the recorded `tokens`/`elapsed_ms` per attempt). `score_round` becomes `SCORERS[rules.scoring.kind](facts(state, round), rules.scoring)`. `classic` is today's code (ADR 0006 numbers) unchanged; other scorers **compose** `classic` (they call `solve_award`/`defense_award` and add a modifier term) instead of re-deriving it.
   Integer points only, so replays compare exactly (no float drift).
3. **Invariants every scorer must satisfy** (one shared property test parametrised over `SCORERS`):
   - pure and deterministic: same facts give the same delta, regardless of dict order;
   - ADR 0006: for the same challenge, defense award >= the max capture award;
   - an invalid/forfeit challenge never scores positively for its author;
   - zero-sum is **not** required (classic is not zero-sum);
   - output keys == the two agent names.
4. **Clocks and budgets are enforced in the app layer, recorded as events, scored from the record.** The engine's single choke point `_call` checks `CostBudget.check` and (new) `MatchClock.check`. Elapsed times are measured once and written into `call_charged` (`elapsed_ms`) **as a metric and for the 900 s safety deadline only**. Game clocks and scoring use recorded tokens and $ (lead decision). Wall-clock never enters scoring.
5. **`match_created` carries the full resolved `Rules`** (it already dumps rules via `created_data`), so replay uses the exact mode, even if presets change later. Add `rules_version: int = 1` so old logs fold with old defaults.
6. Seeds: `match_created` records `seed` (default derived `sha256(match_id)`), used for the fake provider, challenge ordering ties and tournament pairing. Real-model calls send `temperature` from config and `seed` where the provider supports it, but **replay never re-calls a model**: recorded completions are the inputs (ADR 0004).

## Consequences
- New modes are a preset plus at most one scorer function and one params model. The engine, state machine and event types stay the same, except for the additive `elapsed_ms`, `seed` and the memory events in the memory spec.
- Moving `solve_points`, `defense_points` and `invalid_penalty` under `scoring` is a breaking config change. Keep top-level aliases for one release (a pydantic `model_validator(mode="before")` moves them).
- Rejected alternatives: one Rules subclass per mode (duplicated validation, branching all through the engine); a scorer plugin entry point (overkill for a single repo, harder to replay); scoring that reads wall-clock (non-deterministic).

## Fit with the current code
- `domain/models.py`: add the section models; `Rules` stays frozen with `extra="forbid"`.
- `domain/scoring.py`: add `RoundFacts`, `SCORERS`; keep `solve_award`/`defense_award`/`add_scores`/`match_stats` (from 0006) as the shared base.
- `app/engine.py::_score` changes by one line. `app/replay.py` rescoring automatically uses the recorded rules.
- `config.py`: `DUEL_MODE` env var plus preset merge.

## Cost
Zero by itself. Every preset must declare an expected cost per match, computed by `duel estimate --mode X` (new, offline) as calls x avg tokens x `DEFAULT_PRICES`. With gpt-4.1-nano ($0.10/$0.40 per MTok) and ~2k input + 0.4k output tokens per call, a call costs ~$0.00036. A classic match (<= 80 calls by `match_max_calls`) is <= ~$0.03. All modes inherit `match_max_usd` (dev $0.25) and lifetime $40 < $50 key.

## Acceptance criteria
- [ ] `Rules()` with no args scores byte-for-byte like `harder-challenges` HEAD: same commentary snapshot, same `round_scored` deltas.
- [ ] `[rules] mode="blitz"` loads a preset, field overrides win, and unknown modes are rejected with a list of known ones (like `check_model_allowed`).
- [ ] Every scorer passes the shared invariant property test (hypothesis).
- [ ] `duel replay` on a log from before this change still verifies (`rules_version` default).
- [ ] No new duplicate-code findings; complexity <= 10.

## Test ideas
- Property: for random `RoundFacts`, `SCORERS[k]` is order-invariant (shuffle dicts) and satisfies the 0006 inequality.
- Golden: one recorded fake match per preset under `tests/snapshots/modes/`; replay must equal the snapshot.
- Config: preset + env override precedence; legacy top-level `solve_points` alias.
- Replay with a mutated preset table: it still uses the logged rules.
