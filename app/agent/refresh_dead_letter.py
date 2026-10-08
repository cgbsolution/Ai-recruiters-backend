"""refresh.dead_letter — after repeated bounces a profile is marked
unreachable and never contacted again.

Bounces are acted on synchronously when they are recorded
(app.talent.deliverability.record_event); this function is the daily catch-up
for anything missed, and ends the waiting sequences of the profiles it marks."""

import inngest

from app.agent.client import inngest_client
from app.agent.common import enabled_orgs, run_db
from app.talent import deliverability


@inngest_client.create_function(
    fn_id="refresh-dead-letter",
    name="Loop A · dead-letter unreachable profiles",
    trigger=inngest.TriggerCron(cron="TZ=Asia/Kolkata 0 9 * * *"),
    concurrency=[inngest.Concurrency(limit=1)],
    retries=2,
)
async def refresh_dead_letter(ctx: inngest.Context) -> dict:
    orgs = await ctx.step.run("orgs", run_db, enabled_orgs, "refresh")
    total = 0
    for org_id in orgs:
        marked = await ctx.step.run(f"dead-letter-{org_id}", run_db, deliverability.dead_letter_sweep, org_id)
        if marked:
            await ctx.step.send_event(f"stop-{org_id}", [
                inngest.Event(name="talent/member.stopped",
                              data={"profile_id": pid, "campaign_id": None, "reason": "unreachable"})
                for pid in marked])
        total += len(marked)
    return {"marked_unreachable": total}
