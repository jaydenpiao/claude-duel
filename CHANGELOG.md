# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Check form: `check()` may not import `hashlib`, `zlib`, `binascii` or `struct` (design rec R5; the program form keeps them), and the answer check also catches affine char-code lists (`a*c+b`) and position-to-character dicts.
- Check form (preset `checked`): a solvability band (design rec R1). The author's solver may call `check()` at most 2,000 times (200,000 with `attacker_solver`), and at most 1 in 64 seeded random inputs shaped like the answer may pass; otherwise the build goes back to repair. `solver_checks`, `density_ppm` and `log2_space` are logged on `challenge_validated`.
- Security (SV-5): with `attacker_solver` off, a reference solver that needs more than the 1x wall time (plus 1s for sandbox start) is rejected as "solver too slow"; solver time is recorded as `solver.seconds`.
- Security (SV-4): `hash()` is banned, the sandbox fixes str hashing (PYTHONHASHSEED=0; `-s -P -S` instead of `-I` so the fixed env is honoured), and the reference must print the same output on 3 runs.
- Security (SV-3): fixed junk inputs (space, "a", "0", newline, same-length filler, the answer with one char changed, its halves) must not reveal the flag, so degenerate acceptance is rejected for free.
- Security (SV-2): a reference payload that shares 8+ characters in a row with the flag digest is rejected, so flag fragments never reach the logs or backups.
- Security (SV-1): an author's reference solver now runs with a decoy flag, and the reference payload must reveal the flag under two different flags, so a challenge can no longer key its answer on FLAG.
- CI: 30-minute job timeouts, and a new push to a PR cancels its older ci and security runs.

### Added
- Driver locks: a second series/tournament driver on the same id fails fast (`MatchLockedError`).
- Series ids from entrant indexes, so look-alike names never share a series log.
- Flag-form detection covers split and shifted encodings in the redactor.
- Published image digests: release builds never reuse a branch-seeded base or layer cache.
- Difficulty-gate probe is exempt from per-agent wallets.
- Tournament entrant models pinned in `tournament_created`.
- CI requires real-Docker sandbox tests (`DUEL_REQUIRE_DOCKER=1`).

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
