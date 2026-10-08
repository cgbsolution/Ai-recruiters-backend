"""Loop C · plan invites — after matching (or on request): autopilot jobs
invite their top K; copilot jobs get one approval request per match, and
nothing is sent until a recruiter approves."""

import inngest

from app.agent.client import inngest_client
from app.agent.common import run_db
from app.talent import invites


@inngest_client.create_function(
    fn_id="invite-plan",
    name="Loop C · plan invites",
    trigger=[
        inngest.TriggerEvent(event="talent/match.completed"),
        inngest.TriggerEvent(event="talent/invite.plan.requested"),
    ],
    concurrency=[inngest.Concurrency(limit=1, key="event.data.job_id")],
    retries=3,
)
async def invite_plan(ctx: inngest.Context) -> dict:
    job_id = int(ctx.event.data["job_id"])
    plan = await ctx.step.run("plan", run_db, invites.plan, job_id)
    if plan.get("invite"):
        await ctx.step.send_event("send-invites", [
            inngest.Event(name="talent/invite.send", data={"match_id": m}) for m in plan["invite"]])
    return {k: (len(v) if isinstance(v, list) else v) for k, v in plan.items()}
