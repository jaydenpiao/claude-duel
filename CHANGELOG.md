# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Driver locks: a second series/tournament driver on the same id fails fast (`MatchLockedError`).
- Series ids from entrant indexes, so look-alike names never share a series log.
- Flag-form detection covers split and shifted encodings in the redactor.
- Published image digests: release builds never reuse a branch-seeded base or layer cache.
- Difficulty-gate probe is exempt from per-agent wallets.
- Tournament entrant models pinned in `tournament_created`.
- CI requires real-Docker sandbox tests (`DUEL_REQUIRE_DOCKER=1`).
- `duel experiment`: stakes experiment arms with a run-cap override (finite values only).
- Match caps: a match-scoped cap hit ends the match `CAPPED` (void, not resumable; ADR 0014).
- Sealed Ledger scorer: stake-discounted captures, keystone and spend tiebreak (arena wiring follows).
- gpt-5 family routed via the Responses API with reasoning tokens counted.
- Integrity control and attacker solver parity (ADR 0015).

### Changed
- Default model is gpt-5-nano; the code default lifetime cap is $33.
- Hypothesis runs without a per-example deadline in CI.
- Spend line shows the lifetime cap; every config profile caps lifetime spend at $33.
- Shared driver helpers for match, series and tournament drivers.
- Public-facing repo docs (security policy, code owners, issue templates).
- Review-code gate is case-insensitive and covers `docs/`.

## [0.1.0]
First public release.

### Added
- `duel run` / `resume` / `replay`: staged agent-vs-agent CTF matches (build, validate, attack, score) with an
  event-sourced, hash-chained log; replay recomputes every score and reports mismatches.
- Sandboxes: subprocess (rlimits) and Docker (no network, read-only, pids/memory caps; gVisor runtime).
- Cost circuit breaker: per-match, per-run and lifetime caps over a shared spend ledger; low-cost model allowlist,
  premium models opt-in.
- Rules and modes: framings, wallets, clocks, handicaps, difficulty gate, between-round memory.
- `duel series`, `duel tournament` (round robin and Swiss), `duel ratings` (Elo and Bradley-Terry with bootstrap
  CIs), `duel bench`, `duel estimate`, `duel card` (SVG summary) and a live arena view.
- Flag-leak and tamper findings, flag redaction in every stored text.
