"""Loop D · offer times — email the candidate their three slots (or, after a
no-show, new ones) and wait up to 4 days for a booking; an offer nobody picks
expires and is flagged for a recruiter."""

from datetime import timedelta

import inngest

from app.agent.client import inngest_client
from app.agent.common import run_db, step_until_settled
from app.talent import scheduling

OFFER_DAYS = 4


@inngest_client.create_function(
    fn_id="interview-schedule",
    name="Loop D · offer interview times",
    trigger=inngest.TriggerEvent(event="talent/interview.offered"),
    idempotency="event.data.key",
    retries=4,
)
async def interview_schedule(ctx: inngest.Context) -> dict:
    interview_id = str(ctx.event.data["interview_id"])
    attempt = int(ctx.event.data.get("attempt") or 0)
    kind = "interview_missed" if ctx.event.data.get("reason") == "no_show" else "interview_offer"

    sent = await step_until_settled(
        ctx, "offer", lambda: run_db(scheduling.send_interview_message, interview_id, kind, attempt))
    if sent.get("status") == "stopped":
        return {"stopped": sent.get("reason")}
    booked = await ctx.step.wait_for_event(
        "wait-booking", event="talent/interview.booked",
        if_exp=f"async.data.interview_id == '{interview_id}'", timeout=timedelta(days=OFFER_DAYS))
    if booked is not None:
        return {"booked": True}
    expired = await ctx.step.run("expire", run_db, scheduling.expire_offer, interview_id, attempt)
    return {"expired": expired}
