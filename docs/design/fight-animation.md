# Design: Fight Animation (match replay) — v1

Status: implemented (`duel fight`). API spend: **$0**. It only replays existing logs.

TL;DR: build **Direction C**. That's a deterministic Canvas 2D replay plus a vendored WebGL post-processing pass (bloom and chromatic aberration) and seeded synth sound, rendered from the event log to HTML, MP4 and GIF. If headless WebGL fails, fall back to Canvas 2D bloom.

---

## 1. Research findings

| Tool / technique | What it adds | Cost / license | Headless on box? |
|---|---|---|---|
| Game feel: hitstop, screen shake, knockback, flashes, trails ([Juice it or lose it](https://www.youtube.com/watch?v=Fy0aCDmgnxg), [Art of Screenshake](https://www.youtube.com/watch?v=AJdEqssNZ-U)) | The biggest "wow" for the effort. Makes hits feel heavy. | Free (technique) | Yes (pure math) |
| Canvas 2D ([MDN](https://developer.mozilla.org/en-US/docs/Web/API/Canvas_API)) | No dependencies, full control, deterministic | Free | Yes (headless Chrome) |
| [three.js](https://threejs.org) + [pmndrs/postprocessing](https://github.com/pmndrs/postprocessing) | Bloom, chromatic aberration, vignette, film grain | MIT | Yes with SwiftShader/ANGLE; riskier |
| [PixiJS](https://pixijs.com) + [pixi-filters](https://github.com/pixijs/filters) | 2D WebGL renderer with glow, RGB-split and shockwave filters | MIT | Same WebGL caveat |
| [Remotion](https://www.remotion.dev) | React to MP4 with a frame-exact renderer | Free for 3 or fewer people; companies of 4+ need a [paid license](https://www.remotion.dev/license) | Yes (Chrome + ffmpeg) |
| [Motion Canvas](https://motioncanvas.io) | Code-driven animation editor and timeline | MIT | Yes (rendering is browser-based) |
| [Rive](https://rive.app), [Lottie](https://airbnb.io/lottie/), [Spline](https://spline.design) | Designer-made vector/3D assets and state machines | Free tiers; editors are SaaS | Runtimes yes; authoring no. Adds an asset pipeline, so not in v1. |
| [jsfxr](https://github.com/chr15m/jsfxr) + [Web Audio](https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API) | Retro SFX generated from parameters, so they can be seeded | Unlicense / free | Yes (offline WAV render for MP4) |
| Terminal: [rich](https://github.com/Textualize/rich) + [VHS](https://github.com/charmbracelet/vhs) | ASCII arena and recorded GIFs | MIT | Yes |
| AI-arena presentation ([Kaggle Game Arena](https://www.kaggle.com/game-arena)) | Board/replay plus per-move reasoning, leaderboards, a clear score HUD | n/a | n/a (reference) |
| Grok Bot plugins: Remotion (12 skills), GSAP (8 skills), Motion (best practices + perf audit) | Guidance on timing, easing and perf | Free, guidance only | n/a |
| Grok Bot plugins: fal, Runway, Krea | Generated art and video | **Paid, non-deterministic.** Only for pre-baked static assets committed to the repo, and only with jayden's OK on spend. **Never in the render path.** | n/a |

Takeaway: the "wow" comes mostly from juice (hitstop, shake, particles, glow, sound), not from heavy frameworks. Canvas 2D plus one small post-processing shader gets roughly 90% of the effect and stays deterministic.

## 2. Directions

- **A. Rich terminal arena.** ASCII fighters and box-drawn HUD, recorded with VHS. Cheap and on-brand for a CLI, but a low ceiling on visuals.
- **B. Canvas 2D HTML replay.** A single self-contained HTML file with no dependencies, with particles, shake and hitstop. Solid and portable.
- **C. (Recommended) B + a vendored WebGL post-processing pass + seeded synth sound.** Bloom and chromatic aberration spike on impacts, with audio muxed into the MP4. The most impressive option that's still deterministic. **Fallback:** if headless WebGL fails (no GPU, or SwiftShader output that varies), switch to a Canvas 2D bloom (downsample, blur and `lighter` composite), chosen with `?fx=2d` and auto-detected.

## 3. Architecture

```
duel fight <ID> --out f.html
  event log → build_beats(events, state) → scrub → JSON embed → template → f.html
scripts/render_fight.sh f.html out.mp4 [out.gif]
  headless Chrome: f.html#frame=N → PNG per frame → ffmpeg → MP4 (+ seeded synth WAV)
  GIF: 640px wide, 15fps (palettegen/paletteuse)
```

- `src/claude_duel/presentation/fight.py` sits next to `card.py`. It reuses `log_digest` from card.py.
- **Shared helper** in `common.py`: `usage_text(u: Usage) -> str` returns, for example, `in 1,234 · out 567 · $0.0123`. `agent_cost()` is refactored to use it (DRY).
- **Beat** (frozen dataclass/pydantic): `kind` (intro | build | attack | impact | defend | fizzle | round_end | ko), `round`, `actor`, `target`, `outcome`, `score`, and `sides: {name: {tokens_in, tokens_out, usd, hud}}`, where `hud = usage_text(cumulative usage at that beat)`.
- **Scrubbing:** a whitelist of fields only. Challenge titles are truncated to 40 characters. No flag, solution or source.
- **Embedding:** `json.dumps(sort_keys=True, separators=(",",":"))`, then replace `<` with `\u003c` (so no raw `</script>`), in `<script type="application/json" id="beats">`.
- **Determinism:** `render(frame)` is a pure function of `(beats, seed, frame)`.
  - seed = `state.seed`, falling back to an int from `log_digest`.
  - PRNG: mulberry32, re-seeded per beat with `seed ^ beatIndex`, so effects don't drift.
  - Banned in source (lint test): `Math.random`, `Date`, `performance.now`. `requestAnimationFrame` is only used for live preview and drives a frame counter.
- Fixed 1280×720 at 30fps. `#frame=N` renders one frame and sets `window.__ready=true`.

## 4. Game feel (frames at 30fps)

### Beat timeline
| Beat | Frames | Notes |
|---|---|---|
| Intro | 45 | Fighters slide in with easeOutBack; names and vendor badges; VS flash (white at 0.8 alpha, 3f) |
| Challenge build | 30 | Glyph rain converging on the attacker's hand; the title types out at 1 char/frame |
| Attack windup | 12 | Attacker scales up 1.0→1.08 with easeInQuad; charge particles pulling inward |
| Projectile travel | 18 | easeInOutCubic; trail of 12 ghosts at 0.6 alpha, decaying ×0.8 |
| Impact (capture) | 1 + hitstop **5** | Full-screen white flash at 0.6, decaying over 4f; damage number pops |
| Shake | 10 | Amplitude 14px, ×0.8 decay per frame, direction from the seeded PRNG; rotation ±0.6° |
| Knockback | 12 | Defender pushed 60px, easeOutExpo, then recovers over 15f |
| Defense (shield shatter) | hitstop 4 + 20 | Hex shield appears, cracks, then 40 shards (life 24f, gravity 0.4) |
| Invalid challenge (fizzle) | 20 | The projectile sputters: 8 gray puffs, a "FIZZLE" text, a short downward pitch sound; no shake |
| Round transition | 30 | Wipe from 0 to 1280px, easeInOutQuart; "ROUND n" slams in (scale 2→1, easeOutBack) |
| Final KO | 60 | Slow-mo at 0.25× for 40f; camera punch-in 1.0→1.35 toward the loser (easeOutCubic); hitstop 6; 120 particles; "K.O." with chromatic aberration at max; hold 30f, then results card |

### Palette
| Role | Hex |
|---|---|
| Side A (left) | `#22D3EE` cyan, glow `#67E8F9` |
| Side B (right) | `#F472B6` magenta, glow `#F9A8D4` |
| Background | `#0B0F1A`, grid `#1E293B` at 0.4 |
| Bloom tint / flash | `#FFFFFF` / `#FDE68A` |
| HUD text / dim | `#E2E8F0` / `#94A3B8` |
| Damage text | `#FACC15` (capture), `#F87171` (KO), `#A3E635` (defense "BLOCK") |
| Fizzle | `#64748B` |

### Particles (hard cap of 400 live particles)
Impact: 60 sparks with life 18f and speed 6–14px/f. Shards: 40, life 24f. KO: 120, life 36f. Ambient embers: 20, life 90f. Additive blend.

### Post-processing (Direction C)
- Bloom: base strength 0.6, threshold 0.7, radius 0.4. Spikes to 1.6 on impact or KO, decaying over 10f.
- Chromatic aberration: base offset 0.0008; impact 0.004; KO 0.008, decaying over 15f.
- Vignette 0.35 throughout.

### Sound (seeded synth presets, seeded with seed+beat for small pitch jitter of ±3%)
| Event | Sound |
|---|---|
| Intro / VS | powerUp |
| Build | blipSelect ticks |
| Windup | laserShoot (rising) |
| Impact | explosion (short) |
| Block | hitHurt + glass |
| Fizzle | a descending blip |
| Round | pickupCoin |
| KO | a long explosion plus a low sine drop |

The audio is rendered offline as a WAV with a fixed sample offset per beat frame. It is muted in the browser until the user clicks (autoplay policy).

### Easing
easeOutBack for entrances, easeInQuad for windup, easeInOutCubic for travel, easeOutExpo for knockback, and easeOutCubic for the camera. Every easing is a pure function in one `ease.js` block.

### Reduced motion
With `prefers-reduced-motion` or `?motion=reduced`: no shake, no chromatic aberration, flashes capped at 0.2 alpha, no slow-mo. Beat timing stays the same so frame counts still match.

## 5. HUD and readability

- Top bar: `ROUND 2/3 · A 1 – 0 B · log 3f9a1c2e`
- Per side, bottom corners, 18px monospace: `in 1,234 · out 567 · $0.0123`. Each side string is **50 characters or fewer** (asserted). The two together fit about 100 columns. Values update at each beat boundary, never between beats.
- Contrast is at least 4.5:1 against the background. The HUD is drawn *after* post-processing so bloom never blurs the text.
- Forbidden in the output: flags, secrets, solver code, `sk-` strings, and raw `</script>` inside the JSON.

## 6. Acceptance criteria and tests

1. The same log produces **byte-identical HTML** (run twice and compare bytes).
2. A forbidden-string scan finds no `sk-`, flag patterns, solution/source fields, and no `</script>` inside the JSON block.
3. A beat snapshot of the fake match (`tests/fixtures`) matches.
4. `usage_text` formatting: `$0.0123`, `$0.0000`, thousands separators; `agent_cost` output is unchanged (regression).
5. Per-side in/out values in the final beat equal `state.usage_by_agent`.
6. CLI end-to-end test: `duel fight <id> --out tmp.html` exits 0 and the file contains the beats.
7. Frame pixel hashes from two headless renders of frames {0, impact, KO} are stable (marked `slow`; skipped if Chrome isn't present).
8. A lint test finds no `Math.random`, `Date` or `performance.now` in the template JS.
9. Coverage is at least 90% on `fight.py` and `common.usage_text`.
10. The arena view snapshot is unchanged, or the diff has been reviewed.
11. Docs: README section, `docs/design/fight-animation.md`, and a demo-videos note (Duel Demo Videos switches its clips to `render_fight.sh` and drops the token overlays added in editing).

## 7. Cost

**$0 in API spend.** It only replays existing logs. Paid generative assets (fal, Runway, Krea) are out of scope unless jayden approves them.

## Implementation notes
- Built as Direction C in `presentation/fight.py` + `presentation/assets/fight.html`; the CLI is `duel fight`.
- Sound is a seeded synth: sfxr-style presets rendered to a WAV in the page (no vendored library).
- The Chrome frame-hash determinism test is marked `slow` and deselected by default (`pytest -m slow`).
- Demo videos: the Duel Demo Videos clips switch to `scripts/render_fight.sh` output; the token overlays previously added in editing are dropped, since the HUD now carries `in X · out Y · $Z` per side.
- Review tuning: the flash is composited after post-processing and under the HUD (peaks 0.35 VS/impact, 0.45 KO; bloom spike +0.5); HUD text sits on dark plates so it keeps at least 4.5:1 contrast on flash frames (pixel-tested). Particles are capped at `MAX_PARTICLES` = 400 per frame. Hitstop lengths (`HITSTOP` in `fight.py`) are passed to the page in the payload.
