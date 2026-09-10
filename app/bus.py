"""NexusOps — Feature 4: in-process pub-sub bus for the WebSocket stream.

Pattern A (approved, Phase 1): events are published once, kept in a bounded
per-incident ring buffer for reconnect catch-up, then fanned out live to every
subscriber (one asyncio.Queue per socket). The replay log dies with the
process — the failure class D-3 already accepts for everything in-flight.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, history_size: int = 200, max_incidents: int = 1000):
        self.history_size = history_size
        self.max_incidents = max_incidents
        self._history: dict[str, deque[dict[str, Any]]] = {}
        self._seq: dict[str, int] = {}
        self._order: deque[str] = deque()
        self._readers: set[asyncio.Queue] = set()

    def publish(self, incident_id: str, event_type: str, **payload) -> dict[str, Any]:
        """Record one event and deliver it to every connected reader."""
        seq = self._seq.get(incident_id, 0) + 1
        self._seq[incident_id] = seq
        event = {"type": event_type, "incident_id": incident_id, "seq": seq, **payload}
        history = self._history.setdefault(incident_id, deque(maxlen=self.history_size))
        history.append(event)
        self._prune(incident_id)
        for reader in list(self._readers):
            reader.put_nowait(event)
        return event

    def _prune(self, incident_id: str) -> None:
        """Cap tracked incidents FIFO. Catch-up is only promised for in-flight
        incidents (FR-6), so finished incidents age out oldest-first."""
        if incident_id not in self._order:
            self._order.append(incident_id)
        while len(self._order) > self.max_incidents:
            oldest = self._order.popleft()
            self._history.pop(oldest, None)
            self._seq.pop(oldest, None)

    def subscribe(self) -> asyncio.Queue:
        """Register a reader, seeded with the current history for catch-up.

        Seeding + registering happen with no await between them, so on a single
        event loop no publish can slip into the gap (snapshot is atomic with
        subscription).
        """
        reader: asyncio.Queue = asyncio.Queue()
        # ponytail: per-reader queues are unbounded — a stalled socket accumulates
        # every live event in RAM. OK for a single-operator minimal dashboard; add a
        # bounded queue with drop-oldest when multi-consumer dashboards matter.
        for scope in self._history.values():
            for event in scope:
                reader.put_nowait(event)
        self._readers.add(reader)
        return reader

    def unsubscribe(self, reader: asyncio.Queue) -> None:
        self._readers.discard(reader)