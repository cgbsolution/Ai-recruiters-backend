"""Loop B · match — Stage 1 SQL filter → Stage 2 vectors → Stage 3 Claude
rerank in chunks of 10 → top K, stale strong matches queued for a refresh.
Ends with talent/match.completed, which starts Loop C.

Two entry points: a recruiter's "Run matching" (talent/match.requested) runs
at once; job edits (talent/job.updated) settle for 2 minutes first, so a burst
of edits matches once — that debounce used to delay the button too."""

from datetime import timedelta

import inngest

from app.agent.client import inngest_client
from app.agent.common import run_db
from app.talent import llm, match_loop, rerank


async def _on_failure(ctx: inngest.Context) -> None:
    original = (ctx.event.data or {}).get("event") or {}
    job_id = (original.get("data") or {}).get("job_id")
    if job_id is not None:
        await run_db(match_loop.fail, int(job_id), "matching failed after retries")


@inngest_client.create_function(
    fn_id="match-job",
    name="Loop B · match a job",
    trigger=inngest.TriggerEvent(event="talent/match.requested"),
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
    chunk = llm.rerank_chunk()  # small on a CPU Ollama, so each step stays short
    for i in range(0, len(items), chunk):
        await ctx.step.run(f"rerank-{i}", rerank.score_chunk, job_id, items[i:i + chunk])
    done = await ctx.step.run("finish", run_db, match_loop.finish, job_id)
    events = [inngest.Event(name="talent/match.completed", data={"job_id": job_id})]
    if done["needs_refresh"]:
        events.append(inngest.Event(name="talent/refresh.sweep.requested",
                                    data={"organization_id": done["organization_id"]}))
    await ctx.step.send_event("matched", events)
    return {"pool": started["pool"], "reranked": len(items), **{k: v for k, v in done.items() if k != "needs_refresh"},
            "needs_refresh": len(done["needs_refresh"])}


@inngest_client.create_function(
    fn_id="match-job-on-edit",
    name="Loop B · match after a job edit",
    trigger=inngest.TriggerEvent(event="talent/job.updated"),
    debounce=inngest.Debounce(key="event.data.job_id", period=timedelta(minutes=2)),
    retries=2,
)
async def match_job_on_edit(ctx: inngest.Context) -> dict:
    job_id = int(ctx.event.data["job_id"])
    reason = await ctx.step.run("request", run_db, match_loop.request, job_id)
    if reason:
        return {"skip": reason}
    await ctx.step.send_event("requested", [inngest.Event(name="talent/match.requested", data={"job_id": job_id})])
    return {"requested": True}
