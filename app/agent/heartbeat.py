"""Worker check-in every 10 minutes (app.talent.heartbeat): proves Inngest can
reach the worker, so the app can warn when background work isn't running."""
import inngest

from app.agent.client import inngest_client
from app.agent.common import run_db
from app.talent import heartbeat


@inngest_client.create_function(
    fn_id="agent-heartbeat",
    name="Agent · worker check-in",
    trigger=inngest.TriggerCron(cron=f"*/{heartbeat.INTERVAL_MINUTES} * * * *"),
    retries=0,
)
async def agent_heartbeat(ctx: inngest.Context) -> dict:
    return await ctx.step.run("beat", run_db, heartbeat.beat)
