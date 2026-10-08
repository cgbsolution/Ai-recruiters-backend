"""refresh.reengage — daily: profiles marked fresh more than the refresh
window ago (default 6 months) become stale and return to the sweep."""

import inngest

from app.agent.client import inngest_client
from app.agent.common import enabled_orgs, run_db
from app.talent import campaigns


@inngest_client.create_function(
    fn_id="refresh-reengage",
    name="Loop A · re-engage profiles that went stale",
    trigger=inngest.TriggerCron(cron="TZ=Asia/Kolkata 30 9 * * *"),
    concurrency=[inngest.Concurrency(limit=1)],
    retries=2,
)
async def refresh_reengage(ctx: inngest.Context) -> dict:
    orgs = await ctx.step.run("orgs", run_db, enabled_orgs, "refresh")
    counts = {}
    for org_id in orgs:
        counts[str(org_id)] = await ctx.step.run(f"stale-{org_id}", run_db, campaigns.reclassify_stale, org_id)
    return counts
