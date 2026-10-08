"""Retention — daily: profiles past their retention date. With the org's
`retention` switch on they are erased (profile, CV versions and files,
interviews, recordings, transcripts), at most 200 a day per organization;
with it off they are only counted in the audit log for an admin to review."""

import inngest

from app.agent.client import inngest_client
from app.agent.common import run_db
from app.talent import retention


def _orgs_with_settings(db) -> list:
    from app.models.talent import TalentSettings
    from app.talent.config import talent_config

    if not talent_config.TALENT_AGENT_ENABLED:
        return []
    return [(row.organization_id, bool((row.flags or {}).get("retention"))) for row in db.query(TalentSettings)]


def _sweep(db, organization_id: int, delete: bool) -> dict:
    from app.talent.audit import audit

    due = retention.due_for_deletion(db, organization_id, limit=200)
    if not delete:
        audit(db, actor="agent", action="retention.due_report", subject_type="organization",
              subject_id=organization_id, organization_id=organization_id, detail={"due": len(due)}, commit=True)
        return {"due": len(due), "erased": 0}
    erased = 0
    for profile_id in due:
        if retention.erase_profile(db, profile_id, actor="agent", reason="retention_expired") is not None:
            erased += 1
    return {"due": len(due), "erased": erased}


@inngest_client.create_function(
    fn_id="retention-sweep",
    name="Retention · erase expired profiles",
    trigger=inngest.TriggerCron(cron="TZ=Asia/Kolkata 0 3 * * *"),
    concurrency=[inngest.Concurrency(limit=1)],
    retries=2,
)
async def retention_sweep(ctx: inngest.Context) -> dict:
    orgs = await ctx.step.run("orgs", run_db, _orgs_with_settings)
    out = {}
    for org_id, delete in orgs:
        out[str(org_id)] = await ctx.step.run(f"retention-{org_id}", run_db, _sweep, org_id, delete)
    return out
