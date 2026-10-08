"""
The Inngest client and the one helper everything uses to emit events.

Event payloads carry ids only — never names, emails, phones or CV text
(Inngest stores event payloads on its side). `emit()` enforces that: a payload
value must be an int, bool, None, a uuid / short slug string, or a list of those.

emit() never raises into the caller: the agent's correctness never depends on
an event arriving (every send re-checks state in the database, and the daily
sweeps catch up on anything left behind), so an Inngest outage degrades
timeliness, not safety.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

import inngest

from app.talent.config import talent_config

logger = logging.getLogger("app.agent")

inngest_client = inngest.Inngest(
    app_id=talent_config.INNGEST_APP_ID,
    event_key=talent_config.INNGEST_EVENT_KEY or None,
    signing_key=talent_config.INNGEST_SIGNING_KEY or None,
    is_production=not talent_config.inngest_is_dev,
    logger=logging.getLogger("app.agent.inngest"),
)

_SAFE_STR = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")

# Tests replace this to capture events instead of sending them.
_sink: Optional[Callable[[str, dict], None]] = None


def _check_ids_only(value: Any, path: str = "data") -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        if not _SAFE_STR.match(value):
            raise ValueError(f"{path}: event payloads carry ids only, got a free-text string")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check_ids_only(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _check_ids_only(v, f"{path}.{k}")
        return
    raise ValueError(f"{path}: unsupported event value type {type(value).__name__}")


def set_sink(sink: Optional[Callable[[str, dict], None]]) -> None:
    global _sink
    _sink = sink


def emit(name: str, data: Dict[str, Any]) -> bool:
    """Send one event. True if Inngest accepted it."""
    _check_ids_only(data)
    if _sink is not None:
        _sink(name, data)
        return True
    if not talent_config.TALENT_AGENT_ENABLED:
        return False
    try:
        inngest_client.send_sync(inngest.Event(name=name, data=data))
        return True
    except Exception as exc:
        logger.warning("Inngest event %s not sent (%s); the next sweep will catch up", name, type(exc).__name__)
        return False


def emit_many(events: List[tuple]) -> int:
    """[(name, data), ...] in batches of 100. Returns how many were accepted."""
    for _, data in events:
        _check_ids_only(data)
    if _sink is not None:
        for name, data in events:
            _sink(name, data)
        return len(events)
    if not talent_config.TALENT_AGENT_ENABLED or not events:
        return 0
    sent = 0
    for i in range(0, len(events), 100):
        chunk = events[i:i + 100]
        try:
            inngest_client.send_sync([inngest.Event(name=n, data=d) for n, d in chunk])
            sent += len(chunk)
        except Exception as exc:
            logger.warning("Inngest batch of %d events not sent (%s)", len(chunk), type(exc).__name__)
    return sent
