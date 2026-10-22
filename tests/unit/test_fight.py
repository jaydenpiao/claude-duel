"""Fight replay (docs/design/fight-animation.md §6): pure, byte-identical HTML, beats snapshot, redaction,
the shared spend formatter, the CLI, and (with Chrome) stable frame pixels."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict
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
from claude_duel.presentation.fight import FRAMES, HITSTOP, STRIKE, FightData, fight_beats, fight_data, fight_html
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
