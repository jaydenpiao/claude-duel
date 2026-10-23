"""Fight replay (docs/design/fight-animation.md §6): pure, byte-identical HTML, beats snapshot, redaction,
the shared spend formatter, the CLI, and (with Chrome) stable frame pixels."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import asdict, replace
from itertools import pairwise
from pathlib import Path

import pytest

from claude_duel import cli
from claude_duel.app.container import build_event_log, build_store
from claude_duel.app.session import play
from claude_duel.config import Settings
from claude_duel.domain import events as ev
from claude_duel.domain.models import Event, MatchState, Rules, Usage
from claude_duel.observability import InMemoryMetrics
from claude_duel.presentation.card import log_digest
from claude_duel.presentation.common import usage_text
from claude_duel.presentation.fight import (
    ATTACK,
    DEFEND,
    FRAMES,
    HITSTOP,
    MISS_VARIANTS,
    STRIKE,
    Beat,
    FightData,
    Side,
    fight_beats,
    fight_data,
    fight_html,
    miss_pool,
    mulberry32,
    pick_variants,
)
from tests.helpers import FLAG, forfeit_events

ROOT = Path(__file__).parent.parent.parent
SNAPSHOT = ROOT / "tests" / "snapshots" / "fight_beats_fake_match.json"
TEMPLATE = ROOT / "src" / "claude_duel" / "presentation" / "assets" / "fight.html"
BEATS = re.compile(r'<script type="application/json" id="beats">(.*?)</script>', re.S)


def _fake(tmp_path: Path) -> tuple[MatchState, list[Event]]:
    settings = Settings(store_dir=tmp_path, sandbox="subprocess", rules=Rules(rounds=2))
    play(settings, "snap", InMemoryMetrics(), lambda e: None)
    return build_store(settings).load("snap"), build_event_log(settings).read("snap")


def _data(tmp_path: Path) -> FightData:
    state, events = _fake(tmp_path)
    return fight_data(state, events, log_digest(events))


def _payload(html: str) -> str:
    m = BEATS.search(html)
    assert m is not None
    return m.group(1)


def test_same_log_same_bytes(tmp_path: Path) -> None:
    state, events = _fake(tmp_path)
    one = fight_html(fight_data(state, events, log_digest(events)))
    two = fight_html(fight_data(state, events, log_digest(events)))
    assert one.encode() == two.encode()


def test_beats_snapshot(tmp_path: Path) -> None:
    beats = json.dumps([asdict(b) for b in _data(tmp_path).beats], indent=1, sort_keys=True, ensure_ascii=False)
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(beats + "\n", encoding="utf-8")
    assert beats + "\n" == SNAPSHOT.read_text(encoding="utf-8")


def test_final_sides_match_state_usage(tmp_path: Path) -> None:
    state, events = _fake(tmp_path)
    last = fight_data(state, events, log_digest(events)).beats[-1]
    assert last.kind == "ko"
    for name, side in last.sides.items():
        u = state.usage_by_agent.get(name) or Usage()
        assert (side.tokens_in, side.tokens_out, side.hud) == (u.input_tokens, u.output_tokens, usage_text(u))
        assert len(side.hud) <= 50


def test_no_secrets_or_script_breakout(tmp_path: Path) -> None:
    state, events = _fake(tmp_path)
    leaky = state.model_copy(update={"match_id": f"</script><b>{FLAG} sk-abcdefgh12345678"})
    html = fight_html(fight_data(leaky, events, log_digest(events)))
    payload = _payload(html)
    assert FLAG not in html
    assert "sk-abcdefgh" not in html
    assert "</script>" not in payload
    assert state.secret not in payload
    assert not {"solution", "source", "secret", "flag"} & set(re.findall(r'"(\w+)":', payload))


def test_forfeit_fizzles_and_failure_ends_the_fight() -> None:
    events = forfeit_events("forfeit", f"boom {FLAG}")
    state = ev.initial_state("m", events[0])
    for e in events:
        ev.apply(state, e)
    beats = fight_beats(state, events)
    assert [b.kind for b in beats] == ["intro", "build", "fizzle", "ko"]
    assert [b.start for b in beats] == [0, FRAMES["intro"], FRAMES["intro"] + FRAMES["build"], 95]
    assert all(FLAG not in b.outcome for b in beats)


def test_usage_text() -> None:
    assert usage_text(Usage()) == "in 0 · out 0 · $0.0000"
    u = Usage(input_tokens=1234, output_tokens=567, reasoning_tokens=89, usd=0.01234)
    assert usage_text(u) == "in 1,234 · out 567 · $0.0123"
    assert usage_text(u, think=True) == "in 1,234 · out 567 (think 89) · $0.0123"


def test_template_js_is_clock_and_random_free() -> None:
    js = TEMPLATE.read_text(encoding="utf-8")
    for banned in ("Math.random", "Date", "performance.now", "http://", "https://"):
        assert banned not in js


def test_cli_fight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DUEL_SANDBOX", "subprocess")
    monkeypatch.setenv("DUEL_ROUNDS", "1")
    assert cli.main(["run", "--match-id", "m", "--format", "json"]) == 0
    assert cli.main(["fight", "m", "--out", "tmp.html"]) == 0
    beats = json.loads(_payload((tmp_path / "tmp.html").read_text(encoding="utf-8")))["beats"]
    assert [beats[0]["kind"], beats[-1]["kind"]] == ["intro", "ko"]
    assert "fight written: tmp.html" in capsys.readouterr().out
    assert cli.main(["fight", "m"]) == 0
    assert (tmp_path / "m-fight.html").exists()


CHROME = shutil.which(os.environ.get("CHROME", "google-chrome"))
needs_chrome = pytest.mark.skipif(
    not (CHROME and shutil.which("node") and shutil.which("ffmpeg")), reason="needs headless Chrome, node and ffmpeg"
)
MAX_PARTICLES = 400


def test_hitstop_is_passed_in_the_payload(tmp_path: Path) -> None:
    d = _data(tmp_path)
    assert json.loads(_payload(fight_html(d)))["hitstop"] == d.hitstop == HITSTOP
    assert f"MAX_PARTICLES = {MAX_PARTICLES};" in TEMPLATE.read_text(encoding="utf-8")


def _render(page: Path, dest: Path, first: int = 0, last: int | None = None) -> Path:
    span = [str(first), str(last)] if last is not None else []
    node = ["node", "--experimental-websocket", "--no-warnings", str(ROOT / "scripts" / "render_fight.mjs")]
    subprocess.run([*node, str(page), str(dest), *span], check=True, timeout=600)
    return dest


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> tuple[FightData, Path]:
    tmp = tmp_path_factory.mktemp("fight")
    d = _data(tmp)
    page = tmp / "f.html"
    page.write_text(fight_html(d), encoding="utf-8")
    return d, _render(page, tmp / "all")


def _luminance(png: Path, box: tuple[int, int, int, int]) -> list[float]:
    x, y, w, h = box
    crop = ["-vf", f"crop={w}:{h}:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(png), *crop], check=True, capture_output=True).stdout

    def lin(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    return [0.2126 * lin(raw[i]) + 0.7152 * lin(raw[i + 1]) + 0.0722 * lin(raw[i + 2]) for i in range(0, len(raw), 3)]


@pytest.mark.slow
@needs_chrome
def test_hud_contrast_on_flash_frames(rendered: tuple[FightData, Path]) -> None:
    d, out = rendered
    impact = next(b for b in d.beats if b.kind == "impact")
    flashes = [30, impact.start + STRIKE, d.beats[-1].start]  # VS, impact, KO peaks
    for f in flashes:
        lum = sorted(_luminance(out / f"f{f:05d}.png", (24, 672, 260, 28)))  # left spend HUD
        text, ground = lum[-1], lum[len(lum) // 2]
        assert (text + 0.05) / (ground + 0.05) >= 4.5, f


@pytest.mark.slow
@needs_chrome
def test_particles_stay_under_the_cap(rendered: tuple[FightData, Path]) -> None:
    d, out = rendered
    counts = json.loads((out / "stats.json").read_text())["particles"]
    assert len(counts) == d.frames
    assert 0 < max(counts) <= MAX_PARTICLES


@pytest.mark.slow
@needs_chrome
def test_frame_pixels_are_stable(tmp_path: Path, rendered: tuple[FightData, Path]) -> None:
    d, out = rendered
    page = tmp_path / "f.html"
    page.write_text(fight_html(d), encoding="utf-8")
    impact = next(b for b in d.beats if b.kind in {"impact", "defend"})
    for f in (0, impact.start + STRIKE + 1, d.beats[-1].start + 20):
        again = _render(page, tmp_path / str(f), f, f) / "f00000.png"
        assert (
            hashlib.sha256(again.read_bytes()).digest() == hashlib.sha256((out / f"f{f:05d}.png").read_bytes()).digest()
        )


# ---- Miss variants (design "Miss variants") ----


def _beat(kind: str, i: int = 0, pressure: float = 0.5, reason_tag: str = "") -> Beat:
    empty = Side(0, 0, 0.0, "in 0 · out 0 · $0.0000")
    return Beat(kind, i * 42, 1, "a", "b", "x", 0, {"a": 0, "b": 0}, {"a": empty, "b": empty}, pressure, reason_tag)


def test_mulberry32_matches_js() -> None:
    r = mulberry32(1)
    assert [r() for _ in range(3)] == [0.6270739405881613, 0.002735721180215478, 0.5274470399599522]


def test_variants_snapshot_includes_picks(tmp_path: Path) -> None:
    d = _data(tmp_path)
    assert {b.variant for b in d.beats if b.kind in {ATTACK, DEFEND}} <= {v[0] for v in MISS_VARIANTS}
    assert any(b.variant for b in d.beats)
    assert all(0 <= b.pressure <= 1 for b in d.beats)
    assert all(b.reason_tag in {"", "attempts", "tokens"} for b in d.beats)


def test_no_adjacent_repeat_over_10k() -> None:
    beats = [_beat(ATTACK if i % 2 == 0 else DEFEND, i, 0.5, "tokens" if i % 2 else "") for i in range(10_000)]
    out = pick_variants(beats, 0xC0FFEE)
    misses = [b.variant for b in out if b.variant]
    assert len(misses) == 10_000
    assert all(a != b for a, b in pairwise(misses))


def _steady(kind: str, tag: str) -> dict[str, float]:
    """Markov steady state for miss picks with the no-repeat exclusion (pressure 0.5)."""
    ids = [v for v, kinds, _ in MISS_VARIANTS if kind in kinds and (v != "timeout" or tag == "tokens")]
    n = len(ids)
    # P[i][j] = Prob(next=j | prev=i); for i==j it's 0
    p = [[0.0] * n for _ in range(n)]
    for i, prev in enumerate(ids):
        pool = miss_pool(_beat(kind, 0, 0.5, tag), prev)
        total = sum(w for _, w in pool)
        for vid, w in pool:
            p[i][ids.index(vid)] = w / total
    # solve pi P = pi, sum pi = 1
    a = [[p[j][i] - (1.0 if i == j else 0.0) for j in range(n)] + [0.0] for i in range(n)]
    a[0] = [1.0] * n + [1.0]  # replace one equation with sum=1
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[piv] = a[piv], a[col]
        for r in range(n):
            if r == col:
                continue
            f = a[r][col] / a[col][col]
            for c in range(col, n + 1):
                a[r][c] -= f * a[col][c]
    return {ids[i]: a[i][n] / a[i][i] for i in range(n)}


def test_variant_distribution_within_three_points() -> None:
    n, seed = 10_000, 0xFACE
    for kind, tag in ((ATTACK, ""), (DEFEND, "tokens"), (DEFEND, "attempts")):
        got = Counter(b.variant for b in pick_variants([_beat(kind, i, 0.5, tag) for i in range(n)], seed))
        expect = _steady(kind, tag)
        for vid, share in expect.items():
            assert abs(100 * got[vid] / n - 100 * share) <= 3, (kind, tag, vid, got[vid] / n, share)


def test_payload_has_pressure_and_variant_and_is_scrubbed(tmp_path: Path) -> None:
    state, events = _fake(tmp_path)
    html = fight_html(fight_data(state, events, log_digest(events)))
    assert fight_html(fight_data(state, events, log_digest(events))).encode() == html.encode()
    payload = json.loads(_payload(html))
    assert any(b.get("variant") for b in payload["beats"])
    assert any(b.get("pressure") for b in payload["beats"])
    blob = json.dumps(payload)
    assert '"payload"' not in blob
    assert '"output"' not in blob
    assert '"reason":' not in blob
    assert {b["reason_tag"] for b in payload["beats"]} <= {"", "attempts", "tokens"}


@needs_chrome
def test_particle_budget_all_variants_at_k1(tmp_path: Path) -> None:
    d = _data(tmp_path)
    empty = Side(0, 0, 0.0, "in 0 · out 0 · $0.0000")
    names = [n for n, _ in d.players[:2]]
    sides = {n: empty for n in names}
    score = dict.fromkeys(names, 0)
    start = FRAMES["intro"]
    beats = [Beat("intro", 0, 0, "", "", "VS", 0, score, sides)]
    for vid, kinds, _ in MISS_VARIANTS:
        kind = kinds[0]
        tag = "tokens" if vid == "timeout" else ""
        beats.append(Beat(kind, start, 1, names[0], names[1], vid, 0, score, sides, 1.0, tag, vid))
        start += FRAMES[kind]
    beats.append(Beat("ko", start, 1, "", "", "K.O.", 0, score, sides))
    page = tmp_path / "allv.html"
    page.write_text(fight_html(replace(d, beats=beats, frames=start + FRAMES["ko"])), encoding="utf-8")
    stats = json.loads((_render(page, tmp_path / "allv") / "stats.json").read_text(encoding="utf-8"))
    assert max(stats["particles"]) <= MAX_PARTICLES


@pytest.mark.slow
@needs_chrome
def test_hud_contrast_per_variant(tmp_path: Path) -> None:
    d = _data(tmp_path)
    empty = Side(0, 0, 0.0, "in 0 · out 0 · $0.0000")
    names = [n for n, _ in d.players[:2]]
    sides = {n: empty for n in names}
    score = dict.fromkeys(names, 0)
    start = FRAMES["intro"]
    beats = [Beat("intro", 0, 0, "", "", "VS", 0, score, sides)]
    peaks = {}
    for vid, kinds, _ in MISS_VARIANTS:
        kind = kinds[0]
        tag = "tokens" if vid == "timeout" else ""
        peaks[vid] = start + STRIKE + 1
        beats.append(Beat(kind, start, 1, names[0], names[1], vid, 30, score, sides, 1.0, tag, vid))
        start += FRAMES[kind]
    page = tmp_path / "hud.html"
    page.write_text(fight_html(replace(d, beats=beats, frames=start)), encoding="utf-8")
    out = _render(page, tmp_path / "hud")
    for vid, f in peaks.items():
        lum = sorted(_luminance(out / f"f{f:05d}.png", (24, 672, 260, 28)))  # left spend HUD plate
        text, ground = lum[-1], lum[len(lum) // 2]
        assert (text + 0.05) / (ground + 0.05) >= 4.5, (vid, f)


@needs_chrome
def test_reduced_motion_keeps_frames_clears_shake(tmp_path: Path) -> None:
    d = _data(tmp_path)
    page = tmp_path / "f.html"
    page.write_text(fight_html(d), encoding="utf-8")
    dest = tmp_path / "rm"
    node = ["node", "--experimental-websocket", "--no-warnings", str(ROOT / "scripts" / "render_fight.mjs")]
    env = {**os.environ, "FIGHT_STATS_ONLY": "1"}
    subprocess.run([*node, f"{page}?motion=reduced", str(dest)], check=True, timeout=600, env=env)
    stats = json.loads((dest / "stats.json").read_text(encoding="utf-8"))
    assert stats["frames"] == d.frames
    for b in d.beats:
        if b.kind not in {ATTACK, DEFEND}:
            continue
        for f in range(b.start, b.start + FRAMES[b.kind]):
            fx = stats["fx"][f]
            assert fx["shake"] == 0, (b.variant, f, fx)
            assert fx["tilt"] == 0, (b.variant, f, fx)
