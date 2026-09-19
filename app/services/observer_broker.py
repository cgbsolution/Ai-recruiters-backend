"""In-process pub/sub broker for live observer feeds.

Two channels per interview session:

1. Event channel — interview transcript + status updates pushed to observers
   over Server-Sent Events. Producers: interview turn endpoint and other
   places that mutate session state. Consumers: observer SSE handlers.

2. Signal channel — WebRTC SDP/ICE messages routed between the candidate
   (single publisher) and observers (subscribers). Each observer gets a
   private inbox; the publisher gets a single inbox that fans in messages
   from all observers.

Caveats:
  - In-memory only. Multiple worker processes (Gunicorn -w 2) would each
    have an isolated broker — observers and publisher must hit the same
    process. Run uvicorn with --workers 1 or stick to a single worker.
  - Not durable. If the server restarts mid-interview the candidate
    auto-reconnects (interview itself is REST-based) but observers will
    need to reload the page.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _safe_put_nowait(q: asyncio.Queue, item: dict) -> None:
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        logger.warning("observer queue full — dropping (cross-thread)")


class _SessionChannels:
    __slots__ = (
        "event_subscribers",
        "signal_publisher_inbox",
        "signal_observer_inboxes",
        "frame_subscribers",
        "frame_publishers",
        "last_frame",
    )

    def __init__(self) -> None:
        # SSE: each observer gets a queue of dict events.
        self.event_subscribers: List[asyncio.Queue] = []
        # Publisher inbox: messages from observers TO the candidate browser.
        self.signal_publisher_inbox: Optional[asyncio.Queue] = None
        # Per-observer inbox keyed by observer_id (random uuid string).
        self.signal_observer_inboxes: Dict[str, asyncio.Queue] = {}
        # Frame relay: subscribers receive binary JPEG blobs as they arrive
        # from the candidate. Each subscriber gets its own bounded queue so
        # a slow observer doesn't back up the candidate.
        self.frame_subscribers: List[asyncio.Queue] = []
        # Open frame-publisher sockets. A count, not a flag: when the
        # candidate's socket reconnects, the stale socket's cleanup must not
        # mark the live one absent or wipe its cached frame.
        self.frame_publishers: int = 0
        # Cache the most recent frame so a freshly-connected observer sees
        # something immediately instead of a black tile until the next
        # candidate frame arrives.
        self.last_frame: Optional[bytes] = None

    def is_idle(self) -> bool:
        """Nothing is attached to this session any more."""
        return not (
            self.event_subscribers
            or self.signal_publisher_inbox is not None
            or self.signal_observer_inboxes
            or self.frame_subscribers
            or self.frame_publishers
        )


class ObserverBroker:
    def __init__(self) -> None:
        self._sessions: Dict[int, _SessionChannels] = defaultdict(_SessionChannels)
        self._lock = asyncio.Lock()
        # Captured at startup so we can dispatch sync producers (SQLAlchemy
        # event listeners running in FastAPI's worker thread pool for sync
        # endpoints) safely back into the loop. asyncio.Queue.put_nowait is
        # NOT thread-safe — calling it from a non-loop thread can drop the
        # wake-up to the consumer.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _prune_locked(self, session_id: int) -> None:
        """Forget a session once nothing is attached, so the map doesn't keep an
        entry for every interview ever observed. Caller must hold _lock."""
        ch = self._sessions.get(session_id)
        if ch is not None and ch.is_idle():
            del self._sessions[session_id]

    def _put_nowait_threadsafe(self, q: asyncio.Queue, item: dict) -> None:
        """Schedule put_nowait on the broker's event loop if we're called from
        a different thread; otherwise put directly. Drops on full queue."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = self._loop or running
        if running is loop or running is not None and loop is None:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                logger.warning("observer queue full — dropping")
            return
        if loop is None:
            # No loop captured and we're not on one — best effort, drop.
            return
        try:
            loop.call_soon_threadsafe(_safe_put_nowait, q, item)
        except RuntimeError:
            # Loop closed (server shutdown) — nothing we can do.
            pass

    # ── Event (SSE transcript) channel ──────────────────────────────────────

    def viewer_count(self, session_id: int) -> int:
        """Largest number of open observer channels of any one kind for a
        session (SSE streams, signalling sockets, frame sockets) — used to cap
        how many viewers a single (possibly leaked) link can attach."""
        ch = self._sessions.get(session_id)
        if ch is None:
            return 0
        return max(
            len(ch.event_subscribers),
            len(ch.signal_observer_inboxes),
            len(ch.frame_subscribers),
        )

    async def subscribe_events(self, session_id: int) -> asyncio.Queue:
        async with self._lock:
            q: asyncio.Queue = asyncio.Queue(maxsize=256)
            self._sessions[session_id].event_subscribers.append(q)
            return q

    async def unsubscribe_events(self, session_id: int, q: asyncio.Queue) -> None:
        async with self._lock:
            ch = self._sessions.get(session_id)
            if ch and q in ch.event_subscribers:
                ch.event_subscribers.remove(q)
            self._prune_locked(session_id)

    def publish_event(self, session_id: int, event: dict) -> None:
        """Synchronous fan-out. Safe to call from anywhere — including a
        different thread (drops on full queue rather than blocking)."""
        ch = self._sessions.get(session_id)
        if not ch or not ch.event_subscribers:
            return
        for q in list(ch.event_subscribers):
            self._put_nowait_threadsafe(q, event)

    # ── Signal (WebRTC) channel ─────────────────────────────────────────────

    async def register_publisher(self, session_id: int) -> asyncio.Queue:
        async with self._lock:
            ch = self._sessions[session_id]
            if ch.signal_publisher_inbox is not None:
                # Stale publisher inbox — close it so the prior connection's
                # reader exits cleanly. The new one takes over.
                try:
                    ch.signal_publisher_inbox.put_nowait({"type": "_replaced"})
                except Exception:
                    pass
            new_inbox: asyncio.Queue = asyncio.Queue(maxsize=256)
            ch.signal_publisher_inbox = new_inbox
            # Replay observer_join for every observer that's already connected,
            # so a publisher arriving (or reconnecting) after observers can
            # send them offers without requiring them to re-open their WS.
            for observer_id in list(ch.signal_observer_inboxes.keys()):
                try:
                    new_inbox.put_nowait({
                        "type": "observer_join",
                        "observer_id": observer_id,
                    })
                except asyncio.QueueFull:
                    break
            return new_inbox

    async def unregister_publisher(self, session_id: int, q: asyncio.Queue) -> None:
        async with self._lock:
            ch = self._sessions.get(session_id)
            if ch and ch.signal_publisher_inbox is q:
                ch.signal_publisher_inbox = None
            # Tell every observer the publisher went away.
            if ch:
                for inbox in list(ch.signal_observer_inboxes.values()):
                    try:
                        inbox.put_nowait({"type": "publisher_gone"})
                    except Exception:
                        pass
            self._prune_locked(session_id)

    async def register_observer(self, session_id: int, observer_id: str) -> asyncio.Queue:
        async with self._lock:
            ch = self._sessions[session_id]
            q: asyncio.Queue = asyncio.Queue(maxsize=256)
            ch.signal_observer_inboxes[observer_id] = q
            # Ping the publisher so it knows to send a fresh offer.
            if ch.signal_publisher_inbox is not None:
                try:
                    ch.signal_publisher_inbox.put_nowait({
                        "type": "observer_join",
                        "observer_id": observer_id,
                    })
                except Exception:
                    pass
            return q

    async def unregister_observer(self, session_id: int, observer_id: str) -> None:
        async with self._lock:
            ch = self._sessions.get(session_id)
            if not ch:
                return
            ch.signal_observer_inboxes.pop(observer_id, None)
            if ch.signal_publisher_inbox is not None:
                try:
                    ch.signal_publisher_inbox.put_nowait({
                        "type": "observer_leave",
                        "observer_id": observer_id,
                    })
                except Exception:
                    pass
            self._prune_locked(session_id)

    def publisher_present(self, session_id: int) -> bool:
        ch = self._sessions.get(session_id)
        return bool(ch and ch.signal_publisher_inbox is not None)

    def deliver_to_observer(self, session_id: int, observer_id: str, msg: dict) -> bool:
        ch = self._sessions.get(session_id)
        if not ch:
            return False
        inbox = ch.signal_observer_inboxes.get(observer_id)
        if inbox is None:
            return False
        try:
            inbox.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            logger.warning("observer signal queue full for session %s observer %s",
                           session_id, observer_id)
            return False

    def deliver_to_publisher(self, session_id: int, msg: dict) -> bool:
        ch = self._sessions.get(session_id)
        if not ch or ch.signal_publisher_inbox is None:
            return False
        try:
            ch.signal_publisher_inbox.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            logger.warning("publisher signal queue full for session %s", session_id)
            return False

    # ── Frame relay (low-latency JPEG fanout) ───────────────────────────────

    async def register_frame_subscriber(self, session_id: int) -> asyncio.Queue:
        async with self._lock:
            ch = self._sessions[session_id]
            # Small queue: drop frames if observer falls behind. Skipping a
            # frame is fine for live video; backing up the candidate is not.
            q: asyncio.Queue = asyncio.Queue(maxsize=4)
            ch.frame_subscribers.append(q)
            # Send the cached last frame immediately so the observer doesn't
            # see a blank tile until the next candidate frame arrives.
            if ch.last_frame is not None:
                try:
                    q.put_nowait(ch.last_frame)
                except asyncio.QueueFull:
                    pass
            return q

    async def unregister_frame_subscriber(self, session_id: int, q: asyncio.Queue) -> None:
        async with self._lock:
            ch = self._sessions.get(session_id)
            if ch and q in ch.frame_subscribers:
                ch.frame_subscribers.remove(q)
            self._prune_locked(session_id)

    async def set_frame_publisher_present(self, session_id: int, present: bool) -> None:
        """Call with True when a frame-publisher socket opens and False when it
        closes (always paired)."""
        async with self._lock:
            ch = self._sessions[session_id]
            if present:
                ch.frame_publishers += 1
                return
            ch.frame_publishers = max(0, ch.frame_publishers - 1)
            if ch.frame_publishers == 0:
                ch.last_frame = None
            self._prune_locked(session_id)

    def frame_publisher_present(self, session_id: int) -> bool:
        ch = self._sessions.get(session_id)
        return bool(ch and ch.frame_publishers > 0)

    def broadcast_frame(self, session_id: int, frame: bytes) -> None:
        """Fan a JPEG (or other binary) frame out to all observers on this
        session. Drops on full queue — slow observers fall behind rather than
        backing up the candidate."""
        ch = self._sessions.get(session_id)
        if not ch:
            return
        ch.last_frame = frame
        if not ch.frame_subscribers:
            return
        for q in list(ch.frame_subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                # Observer is behind — discard frame for them this tick.
                pass


broker = ObserverBroker()
