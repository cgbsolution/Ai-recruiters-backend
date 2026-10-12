"""Loop D · score — after an interview ends: retry any transcription that
failed, score the transcript against the rubric (quotes verified), mark it for
human review, then rebuild the job's shortlist order."""

import inngest

from app.agent.client import inngest_client
from app.agent.common import run_db
from app.talent import interview_transcripts, scoring


@inngest_client.create_function(
    fn_id="interview-score",
    name="Loop D · score an interview",
    trigger=inngest.TriggerEvent(event="talent/interview.completed"),
    # One run per interview at a time; no 24 h idempotency key, which also
    # swallowed retries and the documented re-score after a prompt change.
    # Re-running is safe — score_interview skips an interview already scored
    # with the current prompt version.
    concurrency=[inngest.Concurrency(limit=1, key="event.data.interview_id"), inngest.Concurrency(limit=4)],
    retries=4,
)
async def interview_score(ctx: inngest.Context) -> dict:
    interview_id = str(ctx.event.data["interview_id"])
    retried = await ctx.step.run("transcribe-missing", interview_transcripts.retry_failed, interview_id)
    result = await ctx.step.run("score", scoring.score_interview, interview_id)
    if result.get("job_id"):
        await ctx.step.send_event("scored", inngest.Event(name="talent/shortlist.requested",
                                                          data={"job_id": int(result["job_id"])}))
    return {"transcriptions_retried": retried, **result}


@inngest_client.create_function(
    fn_id="shortlist-build",
    name="Loop D · build the shortlist order",
    trigger=inngest.TriggerEvent(event="talent/shortlist.requested"),
    debounce=inngest.Debounce(key="event.data.job_id", period=60_000),
    retries=3,
)
async def shortlist_build(ctx: inngest.Context) -> dict:
    return await ctx.step.run("build", run_db, scoring.build_shortlist, int(ctx.event.data["job_id"]))
