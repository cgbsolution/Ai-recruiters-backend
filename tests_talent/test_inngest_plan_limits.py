"""Inngest Cloud refuses to sync an app whose functions ask for more concurrency
than the plan allows ("higher concurrency limits (25) than your plan limit of 5")."""
from app.agent import common
from app.agent.registry import FUNCTIONS
from app.talent.config import talent_config


def _function_wide_limits():
    for f in FUNCTIONS:
        cfg = f.get_config("https://api.example.com/api/inngest")
        for fn in (cfg.main, cfg.on_failure):
            for c in (fn.concurrency or []) if fn is not None else []:
                if c.key is None:
                    yield fn.id, c.limit


def test_no_function_asks_for_more_concurrency_than_the_plan_allows():
    limits = dict(_function_wide_limits())
    assert limits and max(limits.values()) <= talent_config.INNGEST_MAX_CONCURRENCY == 5
    assert limits["anytimehire-talent-agent-refresh-touch"] == 5  # wants 25
    assert limits["anytimehire-talent-agent-invite-send"] == 5  # wants 20


def test_a_paid_plan_raises_the_cap(monkeypatch):
    monkeypatch.setattr(talent_config, "INNGEST_MAX_CONCURRENCY", 100)
    assert common.concurrency(25).limit == 25
    monkeypatch.setattr(talent_config, "INNGEST_MAX_CONCURRENCY", 0)
    assert common.concurrency(25).limit == 1
