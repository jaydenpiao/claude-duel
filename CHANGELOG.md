# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0]
First public release.

### Added
- Ledger heat (design R5): the comparison set now includes the opponent's earlier valid challenges in the match and the same model's valid challenges from its last 3 matches (frozen into `match_created.heat_refs`, so replay is exact). Penalty line, cap and ADR 0006 addendum unchanged.
- `duel run` / `resume` / `replay`: staged agent-vs-agent CTF matches (build, validate, attack, score) with an
  event-sourced, hash-chained log; replay recomputes every score and reports mismatches.
- Sandboxes: subprocess (rlimits) and Docker (no network, read-only, pids/memory caps; gVisor runtime).
- Cost circuit breaker: per-match, per-run and lifetime caps over a shared spend ledger; low-cost model allowlist,
  premium models opt-in.
- Rules and modes: framings, wallets, clocks, handicaps, difficulty gate, between-round memory.
- `duel series`, `duel tournament` (round robin and Swiss), `duel ratings` (Elo and Bradley-Terry with bootstrap
  CIs), `duel bench`, `duel estimate`, `duel card` (SVG summary) and a live arena view.
- Flag-leak and tamper findings, flag redaction in every stored text.
