"""refresh.respond — after a candidate submits the refresh page (or a lost
version is re-queued): parse the new CV version (Claude, with a diff against
the previous one), then embed it for matching."""

import inngest

from app.agent.client import inngest_client
from app.agent.common import run_db
from app.talent import cv_pipeline, embeddings


@inngest_client.create_function(
    fn_id="refresh-respond",
    name="Loop A · process a refreshed profile",
    trigger=[
        inngest.TriggerEvent(event="talent/refresh.responded"),
        inngest.TriggerEvent(event="talent/cv.version.pending"),
    ],
    # One run per version at a time; no 24 h idempotency key: Inngest counts a
    # FAILED run too, so the sweep's retry of a stuck version was ignored for a
    # day. Re-running is safe — process() skips a version that isn't pending.
    concurrency=[inngest.Concurrency(limit=1, key="event.data.cv_version_id"), inngest.Concurrency(limit=4)],
    retries=4,
)
async def refresh_respond(ctx: inngest.Context) -> dict:
    version_id = str(ctx.event.data["cv_version_id"])
    parsed = await ctx.step.run("parse", cv_pipeline.process, version_id)
    embedded = await ctx.step.run("embed", run_db, embeddings.embed_version_sync, version_id)
    return {"parse": parsed, "embed": embedded}
