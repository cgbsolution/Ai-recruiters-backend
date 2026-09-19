"""
Rate-limit keys and per-account brute-force throttles.

Why per-account and per-user, not per-IP: production runs uvicorn with
`--proxy-headers --forwarded-allow-ips="*"`, which makes request.client.host
the LEFTMOST X-Forwarded-For entry, a value the client chooses. Every slowapi
limit keyed on get_remote_address could therefore be reset by sending a fresh
X-Forwarded-For per request (verified: 60 wrong passwords in a row, no 429).
The limits below key on things the client can't rotate: the account being
signed into, or the authenticated user.

The counters are in-process. With several workers each keeps its own, so the
effective ceiling is limit x workers — still bounded, which is the point.
"""
import threading
import time
from collections import deque
from typing import Deque, Dict, Tuple

from fastapi import Request
from slowapi.util import get_remote_address


def user_or_ip_key(request: Request) -> str:
    """slowapi key for authenticated routes: the signed-in user's id.

    Dependencies (get_current_user) run before a slowapi-wrapped endpoint, so
    by the time this is called the bearer token has already been verified —
    decoding it again here only reads the subject. Falls back to the client
    address for a request without a usable token."""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        try:
            from app.auth import decode_access_token

            return "user:" + str(decode_access_token(auth[7:].strip())["sub"])
        except Exception:
            pass
    return "ip:" + get_remote_address(request)


class SlidingWindowCounter:
    """Counts events per key inside a rolling window."""

    def __init__(self, limit: int, window_seconds: int, max_keys: int = 50_000):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._events: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> Deque[float]:
        q = self._events.get(key)
        if q is None:
            return deque()
        while q and now - q[0] >= self.window:
            q.popleft()
        if not q:
            self._events.pop(key, None)
        return q

    def blocked_for(self, key: str) -> int:
        """Seconds until `key` may try again (0 when not blocked)."""
        now = time.monotonic()
        with self._lock:
            q = self._prune(key, now)
            if len(q) < self.limit:
                return 0
            return max(1, int(self.window - (now - q[0])) + 1)

    def hit(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            if key not in self._events and len(self._events) >= self.max_keys:
                # Bound memory under a flood of distinct keys: drop the oldest.
                self._events.pop(next(iter(self._events)))
            q = self._events.setdefault(key, deque())
            q.append(now)
            while len(q) > self.limit:
                q.popleft()

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def snapshot(self) -> Tuple[int, int]:
        with self._lock:
            return len(self._events), sum(len(q) for q in self._events.values())


def per_user_limit(name: str, limit: int, window_seconds: int = 60):
    """Route dependency: at most `limit` calls per signed-in user per window.

    For the expensive admin actions (LLM re-scoring, re-evaluation, outgoing
    email / WhatsApp) that had no limit at all. Use as
    `dependencies=[Depends(per_user_limit("fit-score", 20))]`."""
    from fastapi import Depends, HTTPException

    from app.auth import get_current_user

    counter = SlidingWindowCounter(limit=limit, window_seconds=window_seconds)

    def _dependency(user=Depends(get_current_user)):
        key = f"{name}:{user.id}"
        wait = counter.blocked_for(key)
        if wait:
            raise HTTPException(
                status_code=429,
                detail="Too many requests for this action. Please wait a moment and try again.",
                headers={"Retry-After": str(wait)},
            )
        counter.hit(key)
        return user

    return _dependency


# 10 wrong passwords for one account inside 15 minutes locks that account's
# sign-in for the rest of the window. A correct password clears the counter.
failed_logins = SlidingWindowCounter(limit=10, window_seconds=15 * 60)

# At most 3 reset emails per address per hour. Requests over the limit get the
# usual generic reply, so this neither reveals accounts nor lets anyone use
# the endpoint to flood a mailbox.
password_reset_requests = SlidingWindowCounter(limit=3, window_seconds=60 * 60)
