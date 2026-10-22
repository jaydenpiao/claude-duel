"""In-memory ``ResultsStore`` for app tests and the store contract suite (same semantics as the SQL adapters)."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from claude_duel.domain.results import MatchProjection, RatingRow, RunCounts, RunStatus, Stored


@dataclass
class Run:
    git_sha: str | None
    status: str = "running"
    counts: RunCounts = field(default_factory=RunCounts)
    ratings: list[RatingRow] = field(default_factory=list)
    digest: str | None = None


class MemoryResults:
    def __init__(self) -> None:
        self.matches: dict[str, MatchProjection] = {}
        self.events: dict[str, set[str]] = {}  # match id -> stored event hashes (append-only)
        self.rejected: set[str] = set()
        self.first_seen: dict[str, int] = {}
        self.rejects: list[tuple[str, str, str]] = []
        self.runs: dict[str, Run] = {}

    def stored(self, match_id: str) -> Stored:
        p = self.matches.get(match_id)
        hashes = frozenset(self.events.get(match_id, ()))
        if p is None:
            return Stored(hashes)
        return Stored(hashes, p.match.projection_version, match_id in self.rejected)

    def ingest(self, projection: MatchProjection) -> int:
        mid = projection.match.match_id
        have = self.events.setdefault(mid, set())
        new = {e.event_hash for e in projection.events} - have
        have |= new
        self.matches[mid] = projection
        self.rejected.discard(mid)
        self.first_seen.setdefault(mid, len(self.first_seen))
        return len(new)

    def start_run(self, run_id: str, git_sha: str | None) -> None:
        self.runs[run_id] = Run(git_sha)

    def reject(self, run_id: str, match_id: str, reason: str) -> None:
        self.rejects.append((run_id, match_id, reason))
        self.rejected.add(match_id)

    def last_ratings_digest(self) -> str | None:
        done = [r for r in self.runs.values() if r.status == "ok" and r.digest is not None]
        return done[-1].digest if done else None

    def finish_run(
        self, run_id: str, status: RunStatus, counts: RunCounts, ratings: list[RatingRow], digest: str | None
    ) -> None:
        run = self.runs[run_id]
        run.status, run.counts, run.ratings = status, copy.copy(counts), list(ratings)
        run.digest = digest if ratings else None

    @contextmanager
    def rebuilding(self) -> Iterator[None]:
        saved = (dict(self.matches), {k: set(v) for k, v in self.events.items()}, set(self.rejected))
        self.matches.clear()
        self.events.clear()
        try:
            yield
        except BaseException:
            self.matches, self.events, self.rejected = saved
            raise
