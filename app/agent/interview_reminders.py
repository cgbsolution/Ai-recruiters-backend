"""Loop D · booked — confirmation, reminders 24 hours and 1 hour before, and
the no-show check after the slot (first no-show: new times offered; second:
a recruiter is flagged). A reschedule cancels this run; every message also
re-checks that the interview is still at the time it was booked for."""

from datetime import timedelta

import inngest

from app.agent.client import inngest_client
from app.agent.common import as_utc, parse_iso, run_db, step_until_settled
from app.talent import scheduling


def _times(db, interview_id: str) -> dict:
    import uuid

    from app.models.talent import TalentInterview

    iv = db.get(TalentInterview, uuid.UUID(interview_id))
    if iv is None or iv.scheduled_at is None:
        return {}
    return {"scheduled_at": iv.scheduled_at.isoformat(), "ends_at": (iv.slot_ends_at or iv.scheduled_at).isoformat()}


@inngest_client.create_function(
    fn_id="interview-reminders",
    name="Loop D · confirmation, reminders, no-show",
    trigger=inngest.TriggerEvent(event="talent/interview.booked"),
    idempotency="event.data.key",
    cancel=[inngest.Cancel(event="talent/interview.rescheduled",
                           if_exp="async.data.interview_id == event.data.interview_id")],
    retries=4,
)
async def interview_reminders(ctx: inngest.Context) -> dict:
    interview_id = str(ctx.event.data["interview_id"])
    attempt = int(ctx.event.data.get("attempt") or 0)
    times = await ctx.step.run("times", run_db, _times, interview_id)
    if not times:
        return {"stopped": "not_scheduled"}
    at = parse_iso(times["scheduled_at"])

    def send(kind: str):
        return lambda: run_db(scheduling.send_interview_message, interview_id, kind, attempt, times["scheduled_at"])

    await step_until_settled(ctx, "confirmed", send("interview_confirmed"))
    # No wall-clock decisions outside steps: the body is replayed. A sleep to a
    # past time returns at once, and the send step skips a reminder that is
    # no longer meaningful (a "tomorrow" reminder for a slot two hours away).
    for kind, lead in (("interview_reminder_24h", timedelta(hours=24)), ("interview_reminder_1h", timedelta(hours=1))):
        await ctx.step.sleep_until(f"until-{kind}", as_utc(at - lead))
        result = await step_until_settled(ctx, kind, send(kind))
        if result.get("status") == "stopped" and result.get("reason") != "too_late":
            return {"stopped": result.get("reason")}

    check_at = parse_iso(times["ends_at"]) + scheduling.NO_SHOW_GRACE
    await ctx.step.sleep_until("until-no-show-check", as_utc(check_at))
    outcome = await ctx.step.run("no-show-check", run_db, scheduling.no_show_check, interview_id, attempt)
    for i in range(3):  # woken a little early: sleep to the time it names, then look again
        if outcome.get("status") != "too_early":
            break
        await ctx.step.sleep_until(f"until-no-show-check-{i}", as_utc(parse_iso(outcome["retry_at"])))
        outcome = await ctx.step.run(f"no-show-check-{i}", run_db, scheduling.no_show_check, interview_id, attempt)
    if outcome.get("status") == "reoffered":
        await ctx.step.send_event("missed", inngest.Event(
            name="talent/interview.offered",
            data={"interview_id": interview_id, "attempt": attempt + 1, "reason": "no_show",
                  "key": f"{interview_id}:{attempt + 1}:offer-missed"}))
    return outcome
