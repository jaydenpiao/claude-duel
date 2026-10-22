"""Observer isolation: presentation can never affect the match (architecture round 2, N4)."""

from __future__ import annotations

import logging

from claude_duel.domain.models import Event
from claude_duel.observability import log
from claude_duel.ports import EventObserver, Metrics

_LOG = logging.getLogger(__name__)


class IsolatedObserver:
    """Calls ``inner`` after an event is applied and saved. The first failure (e.g. a closed pipe
    from ``duel run | head``) is logged and counted, then the observer is switched off. Never raises.
    """

    def __init__(self, inner: EventObserver, metrics: Metrics) -> None:
        self._inner = inner
        self._metrics = metrics
        self.failed = False

    def __call__(self, event: Event) -> None:
        if self.failed:
            return
        try:
            self._inner(event)
        except Exception as exc:
            self.failed = True
            self._metrics.incr("observer.error", kind=type(exc).__name__)
            log(_LOG, logging.WARNING, "observer failed; output disabled, match continues", error=type(exc).__name__)
