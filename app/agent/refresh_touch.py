"""refresh.touch — one member's sequence: touch 1 → wait 3 days → touch 2 →
wait 4 days → touch 3 → wait 4 days → no_response. Any `talent/member.stopped`
for the profile (responded, opted out, bounced, complaint) ends it at once;
each send step re-checks the database anyway, so a lost stop event can't cause
an unwanted message."""

from datetime import timedelta

import inngest

from app.agent.client import inngest_client
from app.agent.common import concurrency, run_db, step_until_settled, stop_filter
from app.talent import refresh
from app.talent.config import talent_config

STOPPED = "talent/member.stopped"
FINAL_WAIT = timedelta(days=4)


@inngest_client.create_function(
    fn_id="refresh-touch",
    name="Loop A · refresh touch sequence",
    trigger=inngest.TriggerEvent(event="talent/refresh.member.dispatched"),
    idempotency="event.data.key",
    concurrency=[concurrency(25)],
    retries=4,
)
async def refresh_touch(ctx: inngest.Context) -> dict:
    campaign_id = str(ctx.event.data["campaign_id"])
    profile_id = int(ctx.event.data["profile_id"])
    gaps = list(talent_config.refresh_touch_gaps) + [FINAL_WAIT]

    for touch_no in (1, 2, 3):
        result = await step_until_settled(
            ctx, f"touch-{touch_no}",
            lambda n=touch_no: run_db(refresh.send_step, campaign_id, profile_id, n),
        )
        if result.get("status") == "stopped":
            return {"stopped_at": touch_no, "reason": result.get("reason")}
        stop = await ctx.step.wait_for_event(
            f"wait-after-{touch_no}", event=STOPPED, if_exp=stop_filter(profile_id),
            timeout=gaps[min(touch_no - 1, len(gaps) - 1)],
        )
        if stop is not None:
            return {"stopped_after": touch_no, "reason": (stop.data or {}).get("reason")}

    await ctx.step.run("no-response", run_db, refresh.end_sequence, campaign_id, profile_id)
    return {"result": "no_response"}
