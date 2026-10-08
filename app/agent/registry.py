"""Every Inngest function the Talent Agent serves."""

from app.agent.refresh_dead_letter import refresh_dead_letter
from app.agent.refresh_reengage import refresh_reengage
from app.agent.refresh_respond import refresh_respond
from app.agent.refresh_sweep import refresh_sweep
from app.agent.refresh_touch import refresh_touch

FUNCTIONS = [
    # Loop A — refresh
    refresh_sweep,
    refresh_touch,
    refresh_respond,
    refresh_reengage,
    refresh_dead_letter,
]
