"""Loop D · conduct — the questions and answers run through the candidate's
interview page (app.talent.interviews); this function prepares the questions
when the interview is booked, and once it starts enforces the time limit by
finishing an interview still open at its deadline."""

import inngest

from app.agent.client import inngest_client
from app.agent.common import as_utc, parse_iso, run_db
from app.talent import interviews


@inngest_client.create_function(
    fn_id="interview-prepare",
    name="Loop D · prepare interview questions",
    trigger=inngest.TriggerEvent(event="talent/interview.booked"),
    idempotency="event.data.key",
    concurrency=[inngest.Concurrency(limit=4)],
    retries=4,
)
async def interview_prepare(ctx: inngest.Context) -> dict:
    return await ctx.step.run("questions", interviews.prepare_questions, str(ctx.event.data["interview_id"]))


@inngest_client.create_function(
    fn_id="interview-conduct",
    name="Loop D · enforce the interview time limit",
    trigger=inngest.TriggerEvent(event="talent/interview.started"),
    idempotency="event.data.interview_id",
    retries=4,
)
async def interview_conduct(ctx: inngest.Context) -> dict:
    interview_id = str(ctx.event.data["interview_id"])
    deadline = await ctx.step.run("deadline", run_db, interviews.deadline_of, interview_id)
    if not deadline:
        return {"stopped": "not_started"}
    await ctx.step.sleep_until("until-deadline", as_utc(parse_iso(deadline)))
    finished = await ctx.step.run("finish-if-open", run_db, interviews.finish_if_overdue, interview_id)
    return {"finished_at_deadline": finished}
