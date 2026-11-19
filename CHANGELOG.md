# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- An attack sandbox wall or signal kill refunds the try and logs `challenge_timeout`; after two timeouts the challenge is voided as too slow (author forfeit). No version bump: this joins the unreleased RULES 2 / SCHEMA 3 from #292 (no v3 log from a real match exists yet), so `challenge_timeout` and `AttackAttempt.timeout` are part of v3. `junk_inputs` also tweaks the first character and one per-match seeded index.
- Repair hints cover junk-input and too-slow validation rejections; the forge prompt, README and app engine docs now say empty and junk input must fail in bounded time.
- `junk_inputs` lives in `domain/probes.py`. Sandbox `killed_reason` is a `Literal` with named constants, so a renamed reason cannot silently skip the too-slow check.
- image: upgrade zlib1g for CVE-2026-76844 / CVE-2026-85091 via https://deb.debian.org with apt SRV records off (official python:3.12-slim is not rebuilt yet; the runner proxy returns 500 for apt SRV lookups).
- Security: new matches pin `heat_token_cap` (256) and `heat_ref_cap` (48) once in config; a legacy log past that ceiling is `heat legacy-unreplayable` and `score not checked`, not rescored with a different similarity.
- New matches pin `heat_compare=full` and charge only an exact whole-source LCS; a pair past the work budget is `heat over budget`, logged in `round_scored.heat_unchecked` and charged 0.
- Heat-on ledger logs written before `LedgerRule.heat_vs_opponent` replay as scored: own-only (#198) by default, opponent heat (#238) when `heat_refs` are logged, and replay retries the opponent reading (with a note) when neither tells (M35). The author filter runs before the reference cap, and config load pins an explicit `heat_vs_opponent` bool (the mode preset's value, else off) (S13-5).
- Attack refusal is a typed `AttackAttempt.refused` field (omitted when false); challenge stdout can no longer spoof the engine's refusal or capture sentinels. A refused try is spent and the attack continues; a refused difficulty-gate probe is inconclusive, not held (ADR 0016).
- Ledger: missing stakes for an attacker skip its attacks (stake 0), not leave them uncapped (#255).
- Side labels use each agent's configured name when both sides share a model id (no more hard-coded A/B that hid names like `fake-1-s2` / `fake-1-s5`).
- Ledger resume no longer re-pays allocate after a crash between the charge and STAKES_SET (#254).
- Replay no longer flags ledger logs written before #199 (no round points) as tampered.

### Changed
- Combined `RULES_VERSION` 1→2 and `SCHEMA_VERSION` 2→3 (#258). New heat-on logs require `heat_compare=full`, an explicit `heat_vs_opponent`, `heat_ref_cap`, `heat_metric=lcs-v1` and `heat_history_matches`, and omit `heat_token_cap` (prefix-only; v1 logs that carry it still replay). This also records earlier rule-meaning changes that stayed on version 1: the ledger preset, SV-1, H6 ledger defaults (keystone/terrain/heat off unless the preset), the #247 system prompt, the #267 program-form prompt, and the #266 per-agent output cap. Logs written between #238 and #279 may omit `heat_vs_opponent` (I15-1 found likely zero of those); replay of such a log is a reported heat mismatch, not a silent rescoring. Refusal (ADR 0016) needed no bump.
- Default OpenAI model and difficulty-gate probe are `gpt-6-luna` (cheaper active alternative to the named `gpt-5-nano` replacement `gpt-5.6-luna`; nano shutdown 2026-12-11). Unset effort is `low` on luna; pinned nano and mini stay `minimal`. Standard price stays $0.10/$0.50.
- Image pin lives in `DEFAULT_IMAGE` (`sandbox_image.py`); the Dockerfile `ARG PYTHON_IMAGE` default must match, CI pulls that constant, and Dependabot ignores `python` digest and version bumps so a one-file bump cannot fail CI. Keeps the existing `e695ab1` digest (`5e2bdb1` is the same linux/amd64 image and does not fix zlib 1+b1, so the upgrade step stays).
- Heat pins (both caps, `full`, opponent heat) are set once when config loads, so series, tournament and pool children use the logged contract and never pin again; resume of a log written without the caps keeps those rules. An invalid or partial pin in a log is a corrupt-log error, not an unverified round. Heat state lives in a `HeatScorer` the engine, replay and arena own (no module caches).
- With #279 merged here, the author filter runs before the reference cap, and config load pins `heat_vs_opponent` to a bool (the mode preset's value, else off) beside the caps.
- Heat display names: a work-budget skip is `heat over budget`; a legacy log past the ceiling is `heat legacy-unreplayable`. The logged field stays `round_scored.heat_unchecked` so old logs replay.
- `engine._allocate` split into row/move/keystone helpers; `ghost_verify` and `require_tls` similarly (H8, M29, M30).
- Config refuses API keys from TOML; env-only secrets (#242).
- Warning when configured agent wallets exceed the match spend cap (#123).
- Tournament bootstrap skips absent entrants (#124).
- Results DB inserts are chunked under the 65,535-parameter limit (#252).
- OpenAI: request reasoning summaries (`reasoning.summary="auto"`); if the org is not verified, retry once without them and stop asking.
- Security (SV-3): fixed junk inputs (space, "a", "0", newline, same-length filler, the answer with one char changed, first/middle/last same-class tweaks, plus one per-match seeded index) must not reveal the flag, so degenerate acceptance is rejected for free. Empty and junk probes share a time budget (`VALIDATION_TIME_FACTOR` wall seconds); a hang or kill is rejected as too slow.
- The system prompt names the arena "LLM Duels", not a vendor (prereg control 1); prereg states model names are display-only (#247).
- Upgrade note: matches saved before this change with a ledger but non-ledger scoring now fail validation on resume, snapshot and backup; finish or export them first (no migration is provided).
- Ledger keystone, terrain and heat default off in `LedgerRule` and are on in the `ledger` preset, so older ledger logs replay as written.
- Program-form build prompt now says the solver runs alone (it cannot call the source's check or helpers), matching the engine; the NameError repair hint says the same. Composing was rejected: program sources run top-level stdin/FLAG code.
- Prophecy templates take technique tags from `memory.TECHNIQUES` and derive `all_in` from `LedgerRule.attack_tokens`; export view names checked against `PUBLIC_VIEWS`; sync run status typed; one role query for sync/export checks; psycopg moved to the optional `db` extra; export name in one constant (quality L23-L28, L30).
- `duel estimate` prices an agent's own `max_output_tokens` (wallet cap still lowers it), so thinking-model runs are not understated.
- `[[agents]] max_output_tokens` sets one agent's per-call output cap (thinking models), overriding `[rules] max_output_tokens`; the wallet cap still lowers it.
- CI runs pytest in parallel with pytest-xdist (4 workers); results-db tests share one worker (from #177).
- CI: stacked PRs (base not main) run the 3.12 check only; PRs into main and main pushes run 3.11, 3.12 and 3.13, which merge.sh requires. Retargeting a PR reruns the check.
- Base and sandbox image python:3.12-slim re-pinned for apt 3.0.4 (CVE-2026-109518).
- The system prompt names the arena "LLM Duels", not a vendor (prereg control 1); prereg states model names are display-only.
- Security (SV-2): a reference payload that shares 8+ characters in a row with the flag digest is rejected, so flag fragments never reach the logs or backups.
- Security (SV-1): an author's reference solver now runs with a decoy flag, and the reference payload must reveal the flag under two different flags, so a challenge can no longer key its answer on FLAG.
- Vendor metadata moved to `domain/vendors.py`; `adapters/providers/registry.py` keeps only the factories, so config and the budget no longer import HTTP adapters (quality M11).
- `scoring = "ledger"` is available via the `ledger` / `ledger_deep` presets (#192, #208 refusal lifted).
- `gpt-4.1-nano` dropped from the allowlist; gpt-5-nano is the low default (#209).
- CI: 30-minute job timeouts, and a new push to a PR cancels its older ci and security runs.
- Default model is gpt-5-nano; the code default lifetime cap is $33.
- Hypothesis runs without a per-example deadline in CI.
- Spend line shows the lifetime cap; every config profile caps lifetime spend at $33.
- Shared driver helpers for match, series and tournament drivers.
- Public-facing repo docs (security policy, code owners, issue templates).
- Review-code gate is case-insensitive and covers `docs/`.
- Results index is Cloud SQL only (`results_sql`, IAM via cloud-sql-proxy Unix socket); Neon dropped (#156).
- Provider registry: vendors and factories behind one table (#154).
- gpt-4.1-nano removed from the allowlist (#209).
- SECURITY.md: contact and branch-protection note (#215, #227).

### Added
- `claude-fable-5-2` on the allowlist at $8/$40 per 1M input/output tokens (standard list from Anthropic pricing, 2026-11-13), premium like `claude-fable-5-1`. Defaults and launch prereg are unchanged.
- Model refusals (Anthropic `stop_reason=refusal`, OpenAI content-filter / `message.refusal`) are a typed outcome, logged on `call_charged.refused` and `AttackAttempt.refused` only when true so older logs stay byte-identical (ADR 0016).
- `ledger` mode: the Sealed Ledger arena with stake allocation and stake-bounded `duel estimate` (#192).
- `duel backup`: finished matches' event logs and state (never `*.secret`, flag-scanned) to a private GCS bucket, `DUEL_REPLAY_BUCKET` (#213).
- Ledger heat (design R5): the comparison set now includes the opponent's earlier valid challenges in the match and the same model's valid challenges from its last 3 matches (frozen into `match_created.heat_refs`, so replay is exact). Penalty line, cap and ADR 0006 addendum unchanged.
- Arena: a live lane per side (phase, challenge preview, last actions with tokens/$/seconds, try inputs and results, solvability verdicts, reasoning summary). `call_charged` now records the call purpose and a short, flag-redacted reasoning summary when the vendor returns one.
- Arena, commentary and match card label each side by its model name (with (A)/(B) when both sides run the same model) instead of agent names.
- `duel replay <id> --view arena [--pace S]` re-watches a finished match in the arena view from its log ($0).
- Deep layer phase 1: Ghost Codex replay (`ledger_deep` preset, `ghost_run` events, ghost captures pay max(1, full // 4); #210).
- Arena ticker logs each terrain draw and each heat penalty line (#211).
- `duel backup` refuses a bucket without public access prevention enforced, and also scans for octal, binary, base85 and base64/zlib-encoded flags before upload.
- Ledger `round_scored` logs each staked capture's points, and replay checks them (#199).
- `duel sync`, `duel db migrate` and `duel export` for the results index (#151).
- Ledger rejection of malformed stakes (#208).
- Launch preregistration `docs/runs/launch-prereg.md` and `launch.toml` (#149).
- `open_match` helper for drivers (#185).
- PROPHECY core (#214; engine wiring follows).
- Driver locks: a second series/tournament driver on the same id fails fast (`MatchLockedError`).
- Series ids from entrant indexes, so look-alike names never share a series log.
- Flag-form detection covers split and shifted encodings in the redactor.
- Published image digests: release builds never reuse a branch-seeded base or layer cache.
- Difficulty-gate probe is exempt from per-agent wallets.
- Tournament entrant models pinned in `tournament_created`.
- CI requires real-Docker sandbox tests (`DUEL_REQUIRE_DOCKER=1`).
- `duel experiment`: stakes experiment arms with a run-cap override (finite values only).
- Match caps: a match-scoped cap hit ends the match `CAPPED` (void, not resumable; ADR 0014).
- Sealed Ledger scorer: stake-discounted captures (#192).
- Ledger keystone bet (#193), seeded terrain (#194), novelty heat (#198).
- gpt-5 family routed via the Responses API with reasoning tokens counted.
- Integrity control and attacker solver parity (ADR 0015).
- Results index on Cloud SQL: `duel sync`, `duel db migrate` and `duel export` (ADR 0012, #151).
- Provider registry: vendors declared once, shared HTTP error mapping, Anthropic adapter (ADR 0013, #154).
- SECURITY.md interim private contact (#215).
- Ledger keystone commit-reveal: each author seals one challenge (#193).
- Ledger seeded terrain: a public stdin transform per challenge, drawn before allocation (#194).
- Ledger novelty heat: an author's challenge close to their own recent ones earns less defense (#198).

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
