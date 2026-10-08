"""Phase 5 · live voice — every 10 minutes, import the transcripts of live
interviews that have finished, so they go through the same scoring and review."""

import uuid

import inngest

from app.agent.client import inngest_client
from app.agent.common import enabled_orgs, run_db
from app.talent import live_voice


def _open_for_enabled_orgs(db) -> list:
    from app.models.talent import JobMatch, TalentInterview

    orgs = set(enabled_orgs(db, "live_voice"))
    if not orgs:
        return []
    ids = live_voice.open_live_interviews(db)
    keep = []
    for iid in ids:
        iv = db.get(TalentInterview, uuid.UUID(iid))
        if db.get(JobMatch, iv.match_id).organization_id in orgs:
            keep.append(iid)
    return keep


@inngest_client.create_function(
    fn_id="live-voice-sync",
    name="Phase 5 · import finished live interviews",
    trigger=inngest.TriggerCron(cron="*/10 * * * *"),
    concurrency=[inngest.Concurrency(limit=1)],
    retries=2,
)
async def live_voice_sync(ctx: inngest.Context) -> dict:
    ids = await ctx.step.run("open-live-interviews", run_db, _open_for_enabled_orgs)
    imported = 0
    for iid in ids:
        result = await ctx.step.run(f"sync-{iid}", run_db, live_voice.sync, iid)
        imported += result.get("status") == "imported"
    return {"checked": len(ids), "imported": imported}
