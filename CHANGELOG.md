# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security
- A vendor `cyber_policy` block on one call is scored as that agent's refusal (spends the try, no forfeit, $0) instead of turning the whole ladder step into `blocked` (S17-3). Opponent text that baits the classifier no longer voids a step's decided boards or steps both sides down. `call_charged` logs `policy_blocked` and `opponent_text` (whether the blocked prompt carried fenced opponent text). The ladder pauses, keeping the step's verdict, after `POLICY_BLOCKS_TO_PAUSE` (3) blocks in one step; two `blocked` steps in a row still pause it.
- Timed-out provider tries are all ledgered (S17-1): `RetryingProvider` no longer retries `ProviderTimeoutError`; the engine retries it (`TIMEOUT_RETRIES` 2, so 3 tries), each try under its own reservation, kept at its worst case on timeout, with the caps re-checked before the next try. Before, timeouts swallowed by the wrapper never reached the ledger, so the hard stops undercounted.
- Arena output units use the vendor's pinned `Rules.arena_ratio`, not the call's own input bytes per token (S17-2). The opponent's text in a prompt (for example whitespace padding in its source) multiplied a side's stake and clock debit 2.5-5.6x. Logged units replay unchanged; `RULES_VERSION` stays 2.

### Fixed
- Findings: dropped the unreachable `dunder-string` severity entry and the escape-attribute names that `policy.INTROSPECTION_ATTR` already matches (no behavior change).
- Ladder preset stakes (pilot 2026-11-26): `min_alloc` 1000 -> 6000 and `attack_tokens` 6000 -> 24000. An attack's `max_tokens` is cut to its stake, so at effort high the hidden reasoning used the whole 1000-token stake and 55% of attack replies truncated. Ladder-final worst case $2128.62 -> $2421.88; typical ($67.32) and calibrated ($585.79) are unchanged because they use measured reply sizes. `RULES_VERSION` stays 2: only the preset's values change, not what a `Rules` field means, and every match logs its full rules, so old logs replay unchanged.
- Claude calls log the effort they sent on `call_charged` (it was always empty); `None` still means the API default.
- Provider timeouts scale with each reply's output cap: the configured timeout, sized for the slowest model in the plan, is the wait for a 24000-token reply, and smaller caps wait proportionally less, never under 60 s (allocate 60 s, attack 100-160 s at the pilot's 300 s). The rule is the same for every model. In the pilot, 11 of about 225 calls stalled for the full 300 s and held their board.
- `duel duplicate` no longer stops a fake 12-board match at `run calls 200/200`: one duplicate invocation now allows `2 x boards_max x match_max_calls` calls (never fewer than `run_max_calls`); dollars stay capped by the match and run USD caps.
- OpenAI Responses requests send the system prompt as the first `developer` input item instead of top-level `instructions`. From GPT-6.2 on, `instructions` is appended after `input` (the model read its rules after the task) and only a leading developer item is eligible for prompt caching.
- Duplicate play: the dead void and replay path is removed (`fault_replays`, `void_share_max_pct`, `Board.void`/`fault`/`replays`, `replay_seed`, `-r<n>` half ids, the `void` result). No board is ever void or replayed, so the worst-case dry run prices one try per board (a third of before). `build_fault`/`attack_fault` are now `build_truncated`/`attack_truncated`.
- Spend ledger and hard stops: the cap check and the reservation append share one exclusive lock (two racers cannot both pass); after each charge the settled total is re-checked against the hard caps, and a reply that reports more tokens than its reservation (every request character plus schema and a 64-token framing allowance in, `max_tokens` out) is charged, then rejected, stopping the run. A torn, unreadable, negative or non-finite `usd` ledger line is corruption and refuses spend instead of being skipped or subtracted. Each paid attempt has an idempotency key (match, calls so far, exact request): a resume replaces the attempt's stale reservation, and a reply charged before a crash is reused from `replies/` instead of calling and charging again. The fake provider never bills past `max_tokens`.
- Duplicate play: a build forfeited on truncation is also typed (`BuildRejected.truncated`, logged only when true), so neither build nor attack voids read any text.
- Duplicate play: a challenge that prints the truncation sentinel can no longer void a board. A twice-truncated attack reply is the typed `AttackAttempt.truncated` (engine-set, logged only when true, like `refused` in ADR 0016) and the void rule reads only that field, never challenge stdout. Joins the unreleased SCHEMA 3 (duplicate play is unreleased; no real duplicate log exists yet).
- Duplicate board clip scales with the largest round weight (the last round's number under linear weights), so a multi-round classic match is no longer clipped as if every round weighed 1. The `ladder` preset (flat, 1 round) is unchanged.
- `install-id` follows `store_dir` from the TOML config: the composition root passes `settings.store_dir` to the provider factory, and the OpenAI adapter no longer reads `DUEL_STORE_DIR` itself.
- Docker sandbox liveness: a challenge that ptrace-stops the in-container `timeout` watchdog can no longer hang a match or leak its container. The container runs Docker's default seccomp profile minus `ptrace`, `process_vm_readv`/`process_vm_writev` and `personality` (`adapters/sandbox/seccomp.json`), and `execute` now runs the cleanup (`docker rm -f`) right after the group kill and before waiting on the client or its stdout reader, with every wait bounded, since a `sudo -n docker` client survives the group kill. Regression tests: the ptrace probe gets EPERM and returns promptly with no container left, and with the profile removed the host deadline still ends the run and removes the container.
- App image: upgrade `libzstd1` to the fixed Debian build (CVE-2026-124851, HIGH) so the image scan passes again.
- CI: the test job's limit is 60 minutes (was 30). With 4 runners sharing the VM the 3.11/3.12 suites hit 30 minutes and were cancelled, blocking merges.
- Tamper findings (ruleset 2): jail-escape attempts are reported as `sandbox-escape` high again instead of low `policy-reject` noise. That covers introspection attributes (`__subclasses__`, `__globals__`, `__bases__`, frame and generator attributes, `_getframe`, `modules`), dunder-string rejections (`'__import__'`), and imports of OS, process, FFI, import-machinery or introspection modules outside the allowlist, including any private `_` module (`posix`, `_posixsubprocess`, `fcntl`, `mmap`, `_io`, `runpy`, `code`, `_thread`, `sysconfig`). Enforcement is unchanged: all of these were and are rejected.
- OpenAI requests send a stable pseudonymous `safety_identifier` (sha256 of a per-install id, never a name or other personal data). The id lives in the store dir (never HOME). An OpenAI response whose parsed `error.code` is `cyber_policy` is a non-retryable `CyberPolicyError` and stops the run, instead of a generic 4xx client error. It is never a forfeit or a spent try: the match fails resumably and a series or tournament pauses without recording the game, until an explicit rerun.
- An attack sandbox wall or signal kill refunds the try and logs `challenge_timeout`; after two timeouts the challenge is voided as too slow and scores 0 for both sides (no invalid penalty; a validation-time too-slow keeps it). `duel estimate` counts the refunded timeout in its upper bound. Validation also probes one max-length (4096-char) same-class junk input, and only attack timeouts on stdin no longer than that are refunded or count toward the void; a longer one is a spent try (S15-1). No version bump: this joins the unreleased RULES 2 / SCHEMA 3 from #292 (no v3 log from a real match exists yet), so `challenge_timeout` and `AttackAttempt.timeout` are part of v3. `junk_inputs` also tweaks the first character and one per-match seeded index.
- Repair hints cover junk-input and too-slow validation rejections; the forge prompt, README and app engine docs now say empty and junk input must fail in bounded time.
- `junk_inputs` lives in `domain/probes.py`. Sandbox `killed_reason` is a `Literal` with named constants, so a renamed reason cannot silently skip the too-slow check.
- image: upgrade zlib1g for CVE-2026-76844 / CVE-2026-85091 via https://deb.debian.org with apt SRV records off (official python:3.12-slim is not rebuilt yet; the runner proxy returns 500 for apt SRV lookups).
- Security: new matches pin `heat_ref_cap` (48) and whole-source `full` compare in config (v1 logs could also pin `heat_token_cap` 256); a legacy log past that ceiling is `heat legacy-unreplayable` and `score not checked`, not rescored with a different similarity.
- New matches pin `heat_compare=full` and charge only an exact whole-source LCS; a pair past the work budget is `heat over budget`, logged in `round_scored.heat_unchecked` and charged 0.
- Heat-on ledger logs written before `LedgerRule.heat_vs_opponent` replay as scored: own-only (#198) by default, opponent heat (#238) when `heat_refs` are logged, and replay retries the opponent reading (with a note) when neither tells (M35). The author filter runs before the reference cap, and config load pins an explicit `heat_vs_opponent` bool (the mode preset's value, else off) (S13-5).
- Attack refusal is a typed `AttackAttempt.refused` field (omitted when false); challenge stdout can no longer spoof the engine's refusal or capture sentinels. A refused try is spent and the attack continues; a refused difficulty-gate probe is inconclusive, not held (ADR 0016).
- Ledger: missing stakes for an attacker skip its attacks (stake 0), not leave them uncapped (#255).
- Side labels use each agent's configured name when both sides share a model id (no more hard-coded A/B that hid names like `fake-1-s2` / `fake-1-s5`).
- Ledger resume no longer re-pays allocate after a crash between the charge and STAKES_SET (#254).
- Replay no longer flags ledger logs written before #199 (no round points) as tampered.

### Changed
- Ladder guards for the final eval (design round 11): a fixed-board match whose mean board spend, times its scheduled boards, is over its cap stops as `capped` from board 4 on (`duplicate_capped` reason `projected_over_cap`; the dearest-next-board stop is `next_board_over_cap`), so a mispriced pairing wastes ~4/34 of its cap instead of most of it. Two `blocked` (vendor policy) steps in a row log `ladder_paused` and stop the run; rerunning resumes. The worst case prices one try per board (no replay path since truncations are scored). ladder-a/b/c are marked superseded.
- Final eval setup (jayden, 2026-11-26): ladder-final, the pilot and both controls play 34 fixed boards (the 200-board control 200) at effort `high` for all 8 models. `[budget.pair_max_usd]` raises the match cap for named pairings only; ladder-final keeps $25 a match and gives the 10 dearer (fable, astra) pairings 1.25x their calibrated case, with ladder_max_usd $675 (1.15x the calibrated dearest path, $585.79). The calibrated basis scales each model's measured output from the effort it was logged at to the plan's (`calibration.EFFORT_OUTPUT`, low 0.5, medium 1, high 2: an assumption until the pilot logs haiku and luna at high). New controls examples/ladder-control-200.toml (haiku vs luna, 200 boards); ladder-pilot-self.toml is now 34 boards. Pilot plus controls are hard-capped at $15 ($2.50 x 2 + $2 + $8). configs/eval.toml raises lifetime_max_usd to $1450 ($82.06 spent + $15 + 2 x $675). MAX_BOARDS is 256.
- Ladder plans send one reasoning effort to every model: `[caps] effort = "low" | "medium" | "high"` (default `medium`, or `--effort`) sets `Rules.reasoning_effort`, so gpt-6-luna no longer runs on its small-model `low` default while Claude and the larger GPT models run on vendor defaults. Effort is part of the plan digest.
- Ladder matches name agents by seat in everything a model sees: `Rules.neutral_ids` (on in the `ladder` preset) makes prompts (`AGENT: p1` / `p2`, where p1 leads that half), the memory scoreboard and challenge ids (`r1-p1-0-…`) carry no agent, model or vendor name. Logs, display and challenge authors keep the real names. Other presets keep name-based ids, so older logs replay unchanged.
- Ladder timeouts are sized for the slowest model in the plan and are the same for every model: a new `[limits]` block (`provider_timeout_s`, `call_deadline_s`, `match_deadline_s`, or `--provider-timeout`/`--call-deadline`/`--match-deadline`) defaults to the measured slowest call with headroom (`app/latency.py`; floors 300 s / 600 s / 3600 s), and a set value below that size is refused. The final plan sizes to 900 s per request, 1800 s per call and 6660 s per half (gpt-6-astra's slowest logged call was 782 s against the old 60 s default); `ladder-pilot.toml` pins 300 / 600 / 3600.
- A call that times out now ledgers its full reservation instead of $0, since vendors generally bill tokens generated after the client disconnects. Other provider failures still release it.
- Vendor-neutral token accounting for the ladder: `Rules.accounting = "arena"` (on in the `ladder` preset) counts stakes, the token clock and wallets in arena units (input = request bytes / 4; output = vendor output tokens x that call's own bytes-per-token ratio), so the same text costs Claude and GPT the same however each tokenizes it (Claude reports about 1.59x GPT's tokens on identical prompts). Output caps are arena units sent as `ceil(cap / ratio)` vendor tokens (`Rules.arena_ratio`, Claude 0.63, to be re-pinned from the logged per-call ratio after the pilot). `call_charged` logs `input_bytes`, `units_in` and `units_out` only in arena units, so older logs replay byte-identical. Claude's worst-case output reservations rise about 59% at the same caps; dollar caps are unchanged.
- Ladder: an OpenAI `cyber_policy` block is an outcome of that pairing (`blocked`: no verdict, never agreement in `ladder-compare`, both sides step down) and the ladder goes on, instead of pausing with no result on a block that a resume would likely hit again. `verify_ladder` checks a blocked step's duplicate match is unfinished.
- Heat history is scoped to the current pairing: each duplicate half scores novelty only against that duplicate match's own earlier halves, never another pairing, step, pilot or run in the same store (a model that had just stepped down no longer started with less history than its opponent).
- Ladder plans can fix the board count per pairing (`[match] boards = N`, or `--boards`): every pairing plays exactly N boards with no early stop. A duplicate match the money cap stops before its scheduled boards is now `capped` (no verdict, excluded from claims, and the ladder stops) instead of being called on the boards played. New budget basis `calibrated` prices each model from its measured mean tokens per call on real matches (`app/calibration.py`, regenerated by `scripts/calibrate_usage.py`; unmeasured models use a named proxy or are refused), and the dry run shows it next to the worst and typical cases. `ladder-final`, `ladder-pilot` and `ladder-pilot-self` use it.
- Duplicate play scores a twice-truncated reply like a refusal (a build forfeit, a spent attack try) instead of voiding and replaying the board. Truncation rates differ by vendor (verbosity and tokenizer), so a void handed the truncating side a re-roll of boards it was likely losing. Each board records `truncated` per agent (typed flags only, so challenge stdout still cannot add to it) and `duel duplicate` reports the totals. The void and replay path stays for harness faults; none reaches a finished half today.
- Findings: policy rejections are a separate, lower-severity `policy-reject` category; `sandbox-escape` (high) is kept
  for imports of escape modules such as `os` or `subprocess`.
- `duel replay` prints recomputed/recorded scores keyed by model label instead of internal agent names.
- Combined `RULES_VERSION` 1→2 and `SCHEMA_VERSION` 2→3 (#258). New heat-on logs require `heat_compare=full`, an explicit `heat_vs_opponent`, `heat_ref_cap`, `heat_metric=lcs-v1` and `heat_history_matches`, and omit `heat_token_cap` (prefix-only; v1 logs that carry it still replay). This also records earlier rule-meaning changes that stayed on version 1: the ledger preset, SV-1, H6 ledger defaults (keystone/terrain/heat off unless the preset), the #247 system prompt, the #267 program-form prompt, and the #266 per-agent output cap. Logs written between #238 and #279 may omit `heat_vs_opponent` (I15-1 found likely zero of those); replay of such a log is a reported heat mismatch, not a silent rescoring. Refusal (ADR 0016) needed no bump.
- Default OpenAI model and difficulty-gate probe are `gpt-6-luna` (cheaper active alternative to the named `gpt-5-nano` replacement `gpt-5.6-luna`; nano shutdown 2026-12-11). Unset effort is `low` on luna; pinned nano and mini stay `minimal`. Standard price stays $0.10/$0.50.
- Image pin lives in `DEFAULT_IMAGE` (`sandbox_image.py`); the Dockerfile `ARG PYTHON_IMAGE` default must match, CI pulls that constant, and Dependabot ignores `python` digest and version bumps so a one-file bump cannot fail CI. Keeps the existing `e695ab1` digest (`5e2bdb1` is the same linux/amd64 image and does not fix zlib 1+b1, so the upgrade step stays).
- Heat pins (`heat_ref_cap`, `full`, `heat_metric`, `heat_history_matches`, opponent heat) are set once when config loads, so series, tournament and pool children use the logged contract and never pin again; resume of a log written without the caps keeps those rules. An invalid or partial pin in a log is a corrupt-log error, not an unverified round. Heat state lives in a `HeatScorer` the engine, replay and arena own (no module caches).
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
- `duel harness`: the $0 no-fluke harness. Scripted bots with a fixed capture chance per try play duplicate matches through the real engine and sandbox, and it reports how often the stronger bot wins and the first-seat share (side bias).
- The ladder validator uses the real allowlist gate (`check_model_allowed`: premium and route checks), so `duel ladder --dry-run` refuses a premium plan without `allow_premium = true` exactly as the real run would, before any step is paid for.
- Duplicate stop rule `margin` (the default): the winner is called on the summed seat-paired board margin with an exact sign-flip test, not the win count. Early stop every 2 boards past boards_min at p <= 0.005, else the call at boards_max at p <= 0.035, else `inconclusive` (too close to call). Defaults are 12-48 boards: resampling 1,273 measured harness boards, an 80% vs 50% bot gap is decided 96.8% of the time (never for the weaker side, 28 boards on average) and equal bots get a false winner 4.5% of the time. examples/ladder-final.toml uses 12-38 so its dearest match stays under the $25 cap on typical cost (92.2% decided at 80/50). `stop = "sprt"` keeps the older win-count rule. `duel harness --save-margins` and `--repro --boards-from FILE [--symmetric]` measure and resample real board margins.
- `duel harness --repro`: $0 two-run agreement under the real stop rule, per matchup (`--p-win`, `--p-tie`) or per ladder plan (`--plan`, `--strengths`). The duplicate stop rule and verdict are now pure functions (`stop_now`, `verdict`); boards_max may go up to 64.
- Ladder budget `basis = "typical"` (`--basis`): the dry run checks the caps against expected cost, and a per-call hard stop refuses any call whose worst case (its ledger reservation) could take the step past match_max_usd or the ladder past ladder_max_usd; the run stops cleanly and resumes after a cap is raised (money is no longer part of the plan digest). Ladder halves are bounded by dollars: run cap = ladder cap, per-half token and call counts raised. `duel ladder-compare RUN1 RUN2` marks each matchup and the final ordering `agree` or `too close to call`. No-fluke commands moved to `cli_nofluke.py` (cli.py stays under the heat test's module size). `duel ladder --watch` prints live progress (halves, builds, attacks, truncations, refusals, timeouts, running spend). examples/ladder-final.toml (Plan A); examples/ladder-pilot.toml and ladder-pilot-self.toml (real pilot on the two cheapest models, $10 in total).
- `duel ladder PLAN [--dry-run]`: the no-fluke ladder of duplicate matches from a TOML plan (examples/ladder-a/b/c/fake.toml) with flag overrides. The validator names each broken rule and retires gpt-5-nano from 2026-12-11 by the plan's `run_date`. The $0 dry run prices the worst-case path and refuses before any call if a match or the ladder could pass its cap; a real run always dry-runs first. `Rules.call_caps` (per-purpose output caps) is new in the unreleased RULES 2 and is omitted from logs when unset.
- `duel duplicate`: no-fluke duplicate play. Each board is one seed played twice with seats swapped; the board difference is clipped, a twice-truncated reply replays the board on a derived seed (then void), and the result needs the sign test (p <= 0.05) or the SPRT, never spend. Prints the board table, margin, p, bootstrap CI, SPRT state and spend vs `--match-max-usd`; `verify` recomputes every board. $0 with fake agents.
- Allowlist `gpt-6.1-terra` ($1/$5), `gpt-6.2-sol` ($2/$10) and `gpt-6.2-astra` ($8/$40) per 1M input/output tokens, standard short-context list prices from the OpenAI pricing page on 2026-11-19. All three are premium (need `allow_premium = true`).
- `ladder` mode preset (no-fluke): ledger scoring with flat round weights, 1 challenge a side, 3 tries, 1 round, terrain off; keystone and heat stay.
- `ScoringRule.round_weight`: `linear` (default, unchanged) or `flat`, which scores every round at weight 1 for the no-fluke ladder.
- `call_charged` logs the reasoning effort actually sent, including defaults (#191).
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
