"""Loop C · invite — one candidate: the invite, then after 3 days without an
answer one reminder, then after 3 more days the match closes. An answer
(talent/invite.responded) ends it at once. Copilot jobs are refused by
invites.send_invite unless a recruiter approved this candidate."""

from datetime import timedelta

import inngest

from app.agent.client import inngest_client
from app.agent.common import run_db, step_until_settled
from app.talent import invites
from app.talent.config import talent_config

RESPONDED = "talent/invite.responded"


def _answered(match_id: str) -> str:
    return f"async.data.match_id == '{match_id}'"


@inngest_client.create_function(
    fn_id="invite-send",
    name="Loop C · invite a candidate",
    trigger=inngest.TriggerEvent(event="talent/invite.send"),
    idempotency="event.data.match_id",
    concurrency=[inngest.Concurrency(limit=20)],
    retries=4,
)
async def invite_send(ctx: inngest.Context) -> dict:
    match_id = str(ctx.event.data["match_id"])
    wait = timedelta(days=talent_config.TALENT_INVITE_REMINDER_DAYS)

    sent = await step_until_settled(ctx, "invite", lambda: run_db(invites.send_invite, match_id, 1))
    if sent.get("status") == "stopped":
        return {"stopped": sent.get("reason")}
    answer = await ctx.step.wait_for_event("wait-answer", event=RESPONDED, if_exp=_answered(match_id), timeout=wait)
    if answer is not None:
        return {"answered": (answer.data or {}).get("interested")}

    reminder = await step_until_settled(ctx, "reminder", lambda: run_db(invites.send_invite, match_id, 2))
    if reminder.get("status") == "stopped":
        return {"stopped": reminder.get("reason")}
    answer = await ctx.step.wait_for_event("wait-answer-2", event=RESPONDED, if_exp=_answered(match_id),
                                           timeout=timedelta(days=talent_config.TALENT_INVITE_CLOSE_DAYS))
    if answer is not None:
        return {"answered": (answer.data or {}).get("interested")}
    closed = await ctx.step.run("close", run_db, invites.close_unanswered, match_id)
    return {"closed": closed}
