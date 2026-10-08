"""
Hooks the rest of the app calls. They run as FastAPI background tasks (after
the response) and never raise: a Talent Agent problem must not fail a job save.
"""

import logging

logger = logging.getLogger("app.talent.hooks")


def job_changed(job_id: int) -> None:
    """A job was created, edited or (re)opened: let Loop B re-match it if the
    organization runs the match loop. Inngest debounces bursts of edits."""
    try:
        from app.agent.client import emit
        from app.database import SessionLocal
        from app.models.job import Job, JobStatus
        from app.talent.settings import loop_enabled

        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if job is None or job.status != JobStatus.open or not loop_enabled(db, job.organization_id, "match"):
                return
        finally:
            db.close()
        emit("talent/job.updated", {"job_id": int(job_id)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Talent Agent job hook failed: %s", type(exc).__name__)
