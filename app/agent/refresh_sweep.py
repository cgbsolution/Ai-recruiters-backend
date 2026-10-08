"""refresh.sweep — daily: enroll due profiles (rolling campaigns), dispatch
what today's send budget allows, re-dispatch lost starts, and re-queue CV
versions whose processing event was lost."""

import inngest

from app.agent.client import _check_ids_only, inngest_client
from app.agent.common import enabled_orgs, flag_on, run_db
from app.talent import campaigns, cv_pipeline

DISPATCHED = "talent/refresh.member.dispatched"


def _sweep(db, organization_id: int) -> dict:
    return campaigns.sweep_org(db, organization_id, rolling=flag_on(db, organization_id, "refresh_rolling"))


@inngest_client.create_function(
    fn_id="refresh-sweep",
    name="Loop A · refresh sweep",
    trigger=[
        inngest.TriggerCron(cron="TZ=Asia/Kolkata 0 10 * * *"),
        inngest.TriggerEvent(event="talent/refresh.sweep.requested"),
    ],
    concurrency=[inngest.Concurrency(limit=1)],
    retries=2,
)
async def refresh_sweep(ctx: inngest.Context) -> dict:
    only = (ctx.event.data or {}).get("organization_id") if ctx.event.name != "inngest/scheduled.timer" else None
    orgs = await ctx.step.run("orgs", run_db, enabled_orgs, "refresh")
    if only is not None:
        orgs = [o for o in orgs if o == int(only)]
    summary = {}
    for org_id in orgs:
        result = await ctx.step.run(f"sweep-org-{org_id}", run_db, _sweep, org_id)
        pairs = result["dispatch"]  # step outputs are replayed: never mutate them
        for i in range(0, len(pairs), 200):
            events = []
            for campaign_id, profile_id in pairs[i:i + 200]:
                data = {"campaign_id": campaign_id, "profile_id": profile_id, "key": f"{campaign_id}:{profile_id}"}
                _check_ids_only(data)
                events.append(inngest.Event(name=DISPATCHED, data=data))
            await ctx.step.send_event(f"dispatch-{org_id}-{i}", events)
        summary[str(org_id)] = {k: v for k, v in result.items() if k != "dispatch"} | {"dispatched": len(pairs)}

    pending = await ctx.step.run("lost-cv-versions", run_db, cv_pipeline.pending_versions)
    if pending:
        await ctx.step.send_event("requeue-cv-versions", [
            inngest.Event(name="talent/cv.version.pending", data={"cv_version_id": v}) for v in pending])
    return {"orgs": summary, "requeued_cv_versions": len(pending)}
