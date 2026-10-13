"""Backfill — version-1 baselines, then embeddings, so every profile can be
matched semantically.

    talent/backfill.requested {organization_id, kind: "embed", limit}   one organization (Embed profiles)
    every 15 minutes                                                      every organization with Match on

The schedule is what keeps new uploads matchable: a resume added to the
Resume Database gets its baseline version and vector within ~15 minutes.
One run at a time (a scheduled and a manual run never overlap); each step is
idempotent, so a retry never re-applies. There is no bulk Claude re-parse:
profiles keep their fast-parse metadata."""

import inngest

from app.agent.client import inngest_client
from app.agent.common import enabled_orgs, run_db
from app.talent import backfill

MAX_ROUNDS = 400
SWEEP_MINUTES = 15


@inngest_client.create_function(
    fn_id="cv-backfill",
    name="Backfill · CV versions and embeddings",
    trigger=[
        inngest.TriggerEvent(event="talent/backfill.requested"),
        inngest.TriggerCron(cron=f"*/{SWEEP_MINUTES} * * * *"),
    ],
    concurrency=[inngest.Concurrency(limit=1)],
    retries=3,
)
async def cv_backfill(ctx: inngest.Context) -> dict:
    if ctx.event.name == "inngest/scheduled.timer":
        orgs = await ctx.step.run("orgs", run_db, enabled_orgs, "match")
    else:
        orgs = [ctx.event.data.get("organization_id")]
    summary = {"baselines": 0, "embedded": 0}

    for org_id in orgs:
        for i in range(MAX_ROUNDS):
            made = await ctx.step.run(f"baselines-{org_id}-{i}", run_db, backfill.ensure_baselines, org_id)
            summary["baselines"] += made
            if made == 0:
                break
        for i in range(MAX_ROUNDS):
            done = await ctx.step.run(f"embed-{org_id}-{i}", run_db, backfill.embed_chunk, org_id)
            summary["embedded"] += done
            if done == 0:
                break
    return summary
