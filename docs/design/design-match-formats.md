# Design: match formats (best-of-N, multi-round, handicaps, tournaments, ratings)
Depends on: design-rules-abstraction.md. Lowest priority per lead; specs here are ready but sequenced last.

## Research basis
- Fighting games: sets are Bo3 ("first to 2"), top-3 sets Bo5; games themselves have rounds. Evo rules: https://www.evo.gg/rules/
- Elo: https://en.wikipedia.org/wiki/Elo_rating_system; Glicko-2 (rating deviation, volatility; good for few games): https://www.glicko.net/glicko/glicko2.pdf
- Bradley-Terry with bootstrap CIs, as used by Chatbot Arena: https://arxiv.org/abs/2403.04132, https://lmsys.org/blog/2023-12-07-leaderboard/, https://en.wikipedia.org/wiki/Bradley%E2%80%93Terry_model
- Swiss pairing: https://en.wikipedia.org/wiki/Swiss-system_tournament
- CTFd dynamic scoring (value decays with solve count): https://docs.ctfd.io/docs/custom-challenges/dynamic-value/ (code: https://github.com/CTFd/CTFd/tree/master/CTFd/plugins/dynamic_challenges)

## Layering (key decision)
A **match** stays exactly what it is today: N rounds, one event log, one `MatchState`. Formats live **above** the match, in a new `Series` aggregate with its own hash-chained log (`<store>/<series>.series.jsonl`, reusing `event_log.py`). This means no state-machine changes and no risk to match replay.
- **Best-of-N series:** `Series(format="bo3", match_ids=[...])`. Each game is a normal match with seed `sha256(series_seed, game_no)`. Sides alternate (agent A builds first / is `agents[0]` in odd games). It ends early when someone reaches ceil(N/2) wins. Match win = higher total score; a tie → tiebreak by fewer attack tokens, then a sudden-death 1-round match.
- **Multi-round:** already exists (`rules.rounds`, escalating `max_source_bytes`, `round_no` weight).
- **Handicaps:** `AgentSpec.handicap: Handicap(attempts_delta, token_budget_mult, bank_mult, score_offset)` is applied by the engine when reading limits per agent. Used for cross-tier pairings (e.g. gpt-5-mini vs. gpt-4.1-nano). Recorded in `match_created`.
- **Tournaments:** round robin for <= 6 entrants, Swiss for more (pair by score, avoid rematches, seeded deterministic tie-breaks). Entrants = (provider, model, prompt-version).
- **Ratings:** `duel ratings` folds **all completed match logs** offline: Elo (K=32) for display, plus Bradley-Terry MLE with bootstrap 95% CIs as the reported number (Arena-style; order-independent, unlike Elo). Glicko-2 is optional. A pure function in `domain/ratings.py`, no extra API cost.
- **Dynamic challenge value (CTFd-style), optional scorer `decay`:** in tournaments, a challenge's capture value decays with how many entrants captured it (CTFd's `linear`/`logarithmic` formulas), applied at series level since a single match has only one attacker.

## Built (#38, first PR)
- Handicaps: `Handicap` on `AgentSpec`, recorded in `match_created`, shown in commentary (`handicap alice: tries -1, ...`); `score_offset` applies to the result via `MatchState.final_scores()`, not to replayed round scores.
- Series: `domain/series.py` + `app/series.py`, `duel series --games N`; logs as `<id>.series.jsonl`. Sudden death: a game tied on score and attack tokens is decided by one extra 1-round match (`<id>-g<n>-sd`); drawn only if that ties too.

## Built (#39, first PR)
- Ratings: `domain/ratings.py`, `duel ratings [--boot N] [--format json]`. Elo order is `match_id` order. Tournaments: `domain/tournament.py` + `app/tournament.py`, `duel tournament` (second PR). Decay (lead decision, 2026-10-22): an opt-in shared challenge pool (`[tournament] pool = true`) where every entrant attacks every other entrant's valid challenges after the pairings; CTFd decay applies only there and breaks ties on series points.

## Cost
A Bo3 is <= 3 matches, <= $0.09. A 6-entrant round robin Bo1 is 15 matches, ~$0.45; double round robin ~$0.90. A Swiss of 8 entrants x 5 rounds is 20 matches, ~$0.60. Add `series_max_usd` (default $2) checked before each game against the ledger.

## Acceptance criteria
- [x] A series log replays: every child match verifies, and the series winner recomputes.
- [x] Bo3 stops after 2-0.
- [x] Sides alternate; seeds derive deterministically.
- [x] Ratings are identical regardless of log file order (BT), with Elo order documented.
- [x] Handicaps are recorded and show in commentary.

## Test ideas
- Fake-provider round robin of 3 skills (1, 2, 3): BT ranking must be monotone in skill with n=30 (free).
- Swiss pairing property: no rematches while avoidable; deterministic for a given seed.
- Ratings golden test against hand-computed Elo for 3 games.
