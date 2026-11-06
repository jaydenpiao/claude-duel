"""Live arena view: an animated scoreboard for ``duel run --view arena`` built on rich Live.

Same isolation as the commentary: it folds events (``MatchFold``) into its own copy of the
match and only draws. ``frame`` is a pure function of an ``ArenaModel`` and a width, so it is
snapshot-tested; ``ArenaView`` is the thin observer that keeps the model and the Live in step.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from claude_duel.domain.flags import scrub
from claude_duel.domain.heat import heat
from claude_duel.domain.models import Challenge, Event, MatchState, Stage
from claude_duel.domain.moments import moment_of
from claude_duel.domain.stakes import own_challenges
from claude_duel.presentation.common import (
    MatchFold,
    agent_cost,
    attempt_view,
    board,
    defense,
    leader,
    result,
    spend,
    verdict,
    verdict_text,
)

SPARKS = "▁▂▃▄▅▆▇█"
TICKER_LINES = 5
REFRESH_PER_SECOND = 4
MIN_BAR = 4
COLORS = ("cyan", "magenta")
STAGE_NOW = {
    Stage.PENDING: "setting up",
    Stage.BUILD: "building traps",
    Stage.VALIDATE: "arena checking traps are solvable",
    Stage.ATTACK: "attacking",
    Stage.SCORE: "scoring",
}


def sparkline(values: list[int]) -> str:
    """One block per round, scaled between the lowest and highest round."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    return "".join(SPARKS[(v - lo) * (len(SPARKS) - 1) // span] for v in values)


def meter(value: int, top: int, width: int) -> str:
    """HP-style bar: ``value`` as a share of ``top`` (the best score so far); negatives are empty."""
    width = max(width, MIN_BAR)
    filled = 0 if top <= 0 else round(width * max(value, 0) / top)
    return "█" * filled + "░" * (width - filled)


def tug(a: int, b: int, width: int) -> str:
    """Tug-of-war / eval bar: the marker sits toward whoever leads, centred on a tie."""
    width = max(width, MIN_BAR + 1)
    total = abs(a) + abs(b) or 1
    pos = round((width - 1) * (0.5 - (a - b) / (2 * total)))
    return "━" * pos + "◆" + "━" * (width - 1 - pos)


def _k(tokens: int) -> str:
    return f"{tokens / 1000:g}k" if tokens >= 1000 else str(tokens)


@dataclass
class Player:
    name: str
    model: str
    score: int = 0
    rounds: list[int] = field(default_factory=list)
    cost: str = ""  # live tokens in/out and $ (common.agent_cost)


@dataclass
class ArenaModel:
    match_id: str = ""
    round: int = 0
    rounds: int = 0
    players: list[Player] = field(default_factory=list)
    now: str = "setting up"
    busy: bool = True
    flash: str = ""
    ticker: list[str] = field(default_factory=list)
    spend: str = ""
    banner: str = ""
    ghosts: str = ""  # deep layer: "GHOSTS" row, "" when ghosts are off
    ledger: dict[str, str] = field(default_factory=dict)  # player -> "stakes 4k·4k·4k · keystone #2"

    def log(self, line: str) -> None:
        self.ticker = [*self.ticker, line][-TICKER_LINES:]


def _player_rows(m: ArenaModel, bar: int) -> Table:
    grid = Table.grid(padding=(0, 1))
    for _ in range(4):
        grid.add_column(no_wrap=True, overflow="ellipsis")
    top = max([p.score for p in m.players] + [1])
    for i, p in enumerate(m.players):
        style = COLORS[i % len(COLORS)]
        grid.add_row(
            Text(p.name, style=f"bold {style}"),
            Text(meter(p.score, top, bar), style=style),
            Text(f"{p.score:>5}"),
            Text(sparkline(p.rounds), style="dim"),
        )
    return grid


def _tug_line(m: ArenaModel, width: int) -> Text:
    if len(m.players) < 2:
        return Text("")
    a, b = m.players[0], m.players[1]
    bar = tug(a.score, b.score, max(width - len(a.name) - len(b.name) - 2, MIN_BAR + 1))
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(a.name, style=COLORS[0]).append(" ").append(bar).append(" ").append(b.name, style=COLORS[1])
    return line


def frame(m: ArenaModel, width: int, spinner: RenderableType | None = None) -> Group:
    """Pure: one full screen for ``m`` at ``width`` columns."""
    width = max(width, 20)
    names = " vs ".join(f"{p.name} ({p.model})" for p in m.players)
    header = Text(f"claude duel · {names}", style="bold", no_wrap=True, overflow="ellipsis")
    sub = Text(f"round {m.round}/{m.rounds} · match {m.match_id}", style="dim", no_wrap=True, overflow="ellipsis")
    bar = max(width - 30, MIN_BAR)
    now: RenderableType = Text(f"now: {m.now}", no_wrap=True, overflow="ellipsis")
    if spinner is not None and m.busy:
        now = Table.grid()
        now.add_row(spinner, Text(f" {m.now}", no_wrap=True, overflow="ellipsis"))
    parts: list[RenderableType] = [header, sub, Text(""), _player_rows(m, bar), _tug_line(m, width), Text("")]
    parts.extend(
        Text(f"{p.name}: {p.cost}", style=f"dim {COLORS[i % len(COLORS)]}", no_wrap=True, overflow="ellipsis")
        for i, p in enumerate(m.players)
        if p.cost
    )
    parts.extend(
        Text(f"{p.name}: {m.ledger[p.name]}", style=COLORS[i % len(COLORS)], no_wrap=True, overflow="ellipsis")
        for i, p in enumerate(m.players)
        if m.ledger.get(p.name)
    )
    if m.ghosts:
        parts.append(Text(f"GHOSTS {m.ghosts}", style="dim", no_wrap=True, overflow="ellipsis"))
    parts.append(now)
    if m.flash:
        parts.append(Text(m.flash, style="bold", no_wrap=True, overflow="ellipsis"))
    parts.extend(Text(f"· {line}", style="dim", no_wrap=True, overflow="ellipsis") for line in m.ticker)
    parts.append(Text(m.spend, style="yellow", no_wrap=True, overflow="ellipsis"))
    if m.banner:
        parts.append(Text(f"🏆 {m.banner}", style="bold reverse", no_wrap=True, overflow="ellipsis"))
    return Group(*parts)


def ghost_row(s: MatchState) -> str:
    """This round's ghost runs and strikes, or "" when the deep layer's ghosts are off."""
    if s.rules.deep is None or not s.rules.deep.ghosts:
        return ""
    now = [hit for key, hit in s.ghost_runs.items() if key.split(":", 1)[0] == str(s.round)]
    total = sum(s.ghost_runs.values())
    return f"r{s.round}: {len(now)}/{s.rules.deep.max_ghost_runs} runs · {sum(now)} strikes · {total} in match"


class ArenaView(MatchFold):
    """EventObserver that keeps an ``ArenaModel`` and redraws it in a rich ``Live``.

    ``pace`` (seconds) pauses after each event so fast fake matches can be watched or recorded.
    Call ``close()`` when the match ends (the CLI does, in ``finally``).
    """

    def __init__(
        self,
        console: Console,
        match_id: str,
        cap_usd: float,
        lifetime_usd: Callable[[], float],
        pace: float = 0.0,
    ) -> None:
        super().__init__(match_id)
        self.model = ArenaModel(match_id=match_id)
        self._console = console
        self._cap = cap_usd
        self._lifetime = lifetime_usd
        self._pace = pace
        self._spinner = Spinner("dots")
        self._live = Live(console=console, refresh_per_second=REFRESH_PER_SECOND, get_renderable=self._draw)

    def _draw(self) -> RenderableType:
        return frame(self.model, self._console.width, self._spinner)

    def __call__(self, event: Event) -> None:
        if not self._live.is_started:
            self._live.start()
        super().__call__(event)
        if self.state is not None:
            self._sync(self.state)
        self._live.refresh()
        if self._pace:
            time.sleep(self._pace)

    def close(self) -> None:
        if self._live.is_started:
            self.model.busy = False
            self._live.refresh()
            self._live.stop()

    def _sync(self, s: MatchState) -> None:
        m = self.model
        m.round, m.rounds = s.round, s.rules.rounds
        for p in m.players:
            p.score = s.scores.get(p.name, 0)
            p.cost = agent_cost(s, p.name)
        m.spend = spend(s, self._lifetime(), self._cap)
        m.ghosts = ghost_row(s)

    def _created(self, s: MatchState, e: Event) -> None:
        self.model.players = [Player(a.name, a.model or "default") for a in s.agents]

    def _stage(self, s: MatchState, e: Event) -> None:
        if s.stage is Stage.COMPLETE:
            self.model.busy = False
            self.model.now = "match over"
            self.model.banner = result(s)
        elif s.stage in STAGE_NOW:
            self.model.now = STAGE_NOW[s.stage]
            if s.stage is Stage.BUILD:
                self.model.log(f"round {s.round}/{s.rules.rounds} starts")

    def _built(self, s: MatchState, e: Event) -> None:
        ch = Challenge.model_validate(e.data["challenge"])
        what = f"built {scrub(ch.spec.title)!r}" if ch.spec else "forfeited a trap"
        self.model.log(f"{ch.author} {what}")

    def _validated(self, s: MatchState, e: Event) -> None:
        ch, title, _ = verdict(s, e)
        self.model.log(f"check {ch.author}'s {title!r}: {verdict_text(s, e)}")

    def _started(self, s: MatchState, e: Event) -> None:
        cid, _, attacker = str(e.data["key"]).rpartition(":")
        ch = s.challenges[cid]
        title = scrub(ch.spec.title) if ch.spec else "forfeit"
        self.model.busy = True
        self.model.now = (
            f"{attacker} attacking {ch.author}'s {title!r}... try {e.data['try_no']}/{s.max_attempts(attacker)}"
        )

    def _attempted(self, s: MatchState, e: Event) -> None:
        a = attempt_view(s, e)
        self.model.now = (
            f"{a.attacker} attacking {a.author}'s {a.title!r} (try {a.try_no}/{s.max_attempts(a.attacker)})"
        )
        if a.captured:
            self.model.flash = f"⚔ {a.attacker} captured {a.author}'s flag ✓ +{a.award}"
            self.model.log(self.model.flash)

    def _attacked(self, s: MatchState, e: Event) -> None:
        held = defense(s, e)
        if held is not None:
            _, ch, award = held
            self.model.flash = f"🛡 {ch.author} defended {scrub(ch.spec.title if ch.spec else '')!r} +{award}"
            self.model.log(self.model.flash)

    def _scored(self, s: MatchState, e: Event) -> None:
        delta = cast("dict[str, int]", e.data["delta"])
        for p in self.model.players:
            p.rounds.append(int(delta.get(p.name, 0)))
        for cid, pts in cast("dict[str, int]", e.data.get("points", {})).items():
            ch = s.challenges.get(cid)
            atk = next((a for a in s.attacks.values() if a.challenge_id == cid), None)
            if ch is None or atk is None:  # only a forged log names a capture that never happened
                continue
            title = scrub(ch.spec.title) if ch.spec else "forfeit"
            self.model.log(f"{atk.attacker} +{pts} for {ch.author}'s {title!r}")
        lines = {cid: line for cid, line in cast("dict[str, int]", e.data.get("heat", {})).items() if line}
        rnd = int(str(e.data["round"]))
        hot = heat(s.challenges.values(), s.validations, rnd, s.rules.ledger) if lines and s.rules.ledger else {}
        for cid, line in sorted(lines.items()):  # a 0 line (at the threshold) is not news
            ch = s.challenges.get(cid)
            if ch is not None:
                pct = f"heat {hot[cid]}% " if cid in hot else "heat "
                self.model.log(f"🔥 {ch.author}'s {scrub(ch.spec.title) if ch.spec else 'forfeit'!r} {pct}-{line}")
        lead = leader(s)
        self.model.log(f"after round {e.data['round']}: {board(s)}" + (f" · {lead} leads" if lead else " · level"))

    def _staked(self, s: MatchState, e: Event) -> None:
        stakes = "·".join(_k(int(x)) for x in cast("list[int]", e.data["stakes"]))
        note = " (fallback)" if e.data.get("fallback") else ""
        self.model.ledger[str(e.data["agent"])] = f"r{e.data['round']} stakes {stakes}{note}"

    def _revealed(self, s: MatchState, e: Event) -> None:
        idx = e.data.get("index")
        key = f"keystone #{cast('int', idx) + 1}" if idx is not None else "no keystone"
        agent = str(e.data["agent"])
        self.model.ledger[agent] = " · ".join(x for x in (self.model.ledger.get(agent, ""), key) if x)

    def _ghost_ran(self, s: MatchState, e: Event) -> None:
        m = moment_of(e)
        if m is None:
            return
        ch = s.challenges[m.subject]
        attacker = m.by.rpartition(":")[2]
        title = scrub(ch.spec.title) if ch.spec else ""
        self.model.flash = f"👻 {attacker}'s ghost struck {ch.author}'s {title!r} (no call)"
        self.model.log(self.model.flash)

    def _terrain(self, s: MatchState, e: Event) -> None:
        drawn = cast("dict[str, str]", e.data["terrains"])
        rnd = int(str(e.data["round"]))
        per: dict[str, list[str]] = {}
        for cid, t in drawn.items():  # every challenge's terrain, by its keystone number: "author #3 swapcase"
            ch = s.challenges.get(cid)
            if ch is not None:
                n = next(i for i, c in enumerate(own_challenges(s.challenges, rnd, ch.author), 1) if c.id == cid)
                per.setdefault(ch.author, []).append(f"#{n} {t.partition(':')[0]}")
        parts = [f"{a} {', '.join(sorted(ts, key=lambda x: int(x[1:].split()[0])))}" for a, ts in sorted(per.items())]
        self.model.log(f"terrain r{rnd}: {' · '.join(parts) or 'none'}")

    def _recovered(self, s: MatchState, e: Event) -> None:
        self.model.log("resuming where the match stopped")

    def _failed(self, s: MatchState, e: Event) -> None:
        self.model.busy = False
        self.model.now = f"match failed during {s.failed_stage}"
        self.model.banner = f"failed: {scrub(s.failure or '')} · duel resume {s.match_id}"
