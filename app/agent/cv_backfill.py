"""Backfill — version-1 baselines, embeddings, and (when asked) the Claude
re-parse of every CV through the Message Batches API.

    talent/backfill.requested {organization_id, kind: "embed" | "parse", limit}

`parse` submits batches of ≤5,000, polls every 10 minutes until each ends
(Batches finish within 24 h), applies the results, then embeds. One run per
organization at a time; each step is idempotent, so a retry never re-applies."""

from datetime import timedelta

import inngest

from app.agent.client import inngest_client
from app.agent.common import run_db
from app.talent import backfill

MAX_ROUNDS = 400


@inngest_client.create_function(
    fn_id="cv-backfill",
    name="Backfill · CV versions, embeddings, Claude re-parse",
    trigger=inngest.TriggerEvent(event="talent/backfill.requested"),
    concurrency=[inngest.Concurrency(limit=1, key="event.data.organization_id")],
    retries=3,
)
async def cv_backfill(ctx: inngest.Context) -> dict:
    org_id = ctx.event.data.get("organization_id")
    kind = ctx.event.data.get("kind", "embed")
    actor = str(ctx.event.data.get("actor") or "agent")
    summary = {"baselines": 0, "embedded": 0, "batches": []}

    for i in range(MAX_ROUNDS):
        made = await ctx.step.run(f"baselines-{i}", run_db, backfill.ensure_baselines, org_id)
        summary["baselines"] += made
        if made == 0:
            break

    if kind == "parse":
        # Submit every batch first, then poll them together: one poll step and
        # one sleep per round keeps a 24-hour wait far below Inngest's step cap.
        remaining = int(ctx.event.data.get("limit") or 50_000)
        run_ids = []
        for b in range(20):
            if remaining <= 0:
                break
            run_id = await ctx.step.run(f"create-batch-{b}", run_db, backfill.create_parse_batch, org_id, actor,
                                        min(remaining, backfill.PARSE_BATCH_SIZE))
            if not run_id:
                break
            run_ids.append(run_id)
            remaining -= backfill.PARSE_BATCH_SIZE
        for poll in range(160):  # 160 x 10 min > 24 h
            statuses = await ctx.step.run(f"poll-{poll}", run_db, backfill.batch_statuses, run_ids)
            if all(s in ("ended", "collected", "missing") for s in statuses):
                break
            await ctx.step.sleep(f"wait-{poll}", timedelta(minutes=10))
        for b, run_id in enumerate(run_ids):
            result = await ctx.step.run(f"collect-{b}", run_db, backfill.collect_parse_batch, run_id)
            summary["batches"].append({"run_id": run_id, **result})

    for i in range(MAX_ROUNDS):
        done = await ctx.step.run(f"embed-{i}", run_db, backfill.embed_chunk, org_id)
        summary["embedded"] += done
        if done == 0:
            break
    return summary
