"""Loop B · match — on job.created / job.updated (debounced per job) or a
recruiter's "run matching": Stage 1 SQL filter → Stage 2 vectors → Stage 3
Claude rerank in chunks of 10 → top K, stale strong matches queued for a
refresh. Ends with talent/match.completed, which starts Loop C."""

from datetime import timedelta

import inngest

from app.agent.client import inngest_client
from app.agent.common import run_db
from app.talent import match_loop, rerank

CHUNK = 10


async def _on_failure(ctx: inngest.Context) -> None:
    original = (ctx.event.data or {}).get("event") or {}
    job_id = (original.get("data") or {}).get("job_id")
    if job_id is not None:
        await run_db(match_loop.fail, int(job_id), "matching failed after retries")


@inngest_client.create_function(
    fn_id="match-job",
    name="Loop B · match a job",
    trigger=[
        inngest.TriggerEvent(event="talent/job.updated"),
        inngest.TriggerEvent(event="talent/match.requested"),
    ],
    debounce=inngest.Debounce(key="event.data.job_id", period=timedelta(minutes=2)),
    concurrency=[inngest.Concurrency(limit=1, key="event.data.job_id"), inngest.Concurrency(limit=3)],
    retries=3,
    on_failure=_on_failure,
)
async def match_job(ctx: inngest.Context) -> dict:
    job_id = int(ctx.event.data["job_id"])
    started = await ctx.step.run("stages-1-2", run_db, match_loop.start, job_id)
    if started.get("skip"):
        return started
    items = started["items"]
    for i in range(0, len(items), CHUNK):
        await ctx.step.run(f"rerank-{i}", rerank.score_chunk, job_id, items[i:i + CHUNK])
    done = await ctx.step.run("finish", run_db, match_loop.finish, job_id)
    events = [inngest.Event(name="talent/match.completed", data={"job_id": job_id})]
    if done["needs_refresh"]:
        events.append(inngest.Event(name="talent/refresh.sweep.requested",
                                    data={"organization_id": done["organization_id"]}))
    await ctx.step.send_event("matched", events)
    return {"pool": started["pool"], "reranked": len(items), **{k: v for k, v in done.items() if k != "needs_refresh"},
            "needs_refresh": len(done["needs_refresh"])}
