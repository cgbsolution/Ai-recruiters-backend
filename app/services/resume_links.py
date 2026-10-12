"""
One person, one Resume Database profile — and every job keeps up with it.

When a newer resume replaces a profile's file (a Resume Database upload, a
recruiter uploading to a job, a candidate applying through an interview link,
the Talent Agent refresh page), the profile keeps its id and, in the same
transaction:

- every job candidate linked to it (candidates.resume_metadata_id) moves to
  the new file, so a job never shows an outdated resume — or, once the old
  file is cleaned up, "not found";
- with the Talent Agent tracking the profile, the change becomes the new
  current CV version (the old file stays as history until its retention
  date) so matching uses the new resume;
- after commit, the old file is deleted once no row references it.

This is a flush hook rather than a step in each upload route, so a route
added later can't forget it.

Repair links broken before this existed:
    python -m app.services.resume_links repair            (dry run)
    python -m app.services.resume_links repair --apply
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.media import resolve_upload_file, resume_file_in_use, safe_unlink_upload, to_upload_relpath
from app.models.candidate import Candidate
from app.models.resume_metadata import ResumeMetadata

logger = logging.getLogger("app.resume_links")

_OLD_FILES = "resume_links_old_files"


def same_file(a: Optional[str], b: Optional[str]) -> bool:
    ra, rb = to_upload_relpath(a), to_upload_relpath(b)
    if ra or rb:
        return ra == rb
    return (a or "") == (b or "")


def find_profile(db: Session, organization_id: Optional[int], email: Optional[str], *,
                 include_shared_inboxes: bool = False) -> Optional[ResumeMetadata]:
    """The organization's existing profile for this person: same email, any
    letter case (the Resume Database upload already matched this way; the job
    upload paths compared exact case and made duplicates). A shared inbox
    (hr@, resumes@…) identifies nobody, so it doesn't match — unless the caller
    is about to store a row under that address anyway (emails are unique per
    organization, so it must reuse the existing row)."""
    from app.services.fast_resume_parser import is_role_mailbox

    email = (email or "").strip()
    if not email or "@" not in email or email.endswith("@resume.local"):
        return None
    if is_role_mailbox(email) and not include_shared_inboxes:
        return None
    q = db.query(ResumeMetadata).filter(sa.func.lower(ResumeMetadata.candidate_email) == email.lower())
    q = (q.filter(ResumeMetadata.organization_id == organization_id) if organization_id is not None
         else q.filter(ResumeMetadata.organization_id.is_(None)))
    return q.order_by(ResumeMetadata.id.desc()).first()


# ── The hook ─────────────────────────────────────────────────────────────────

def _record_cv_version(session: Session, profile: ResumeMetadata, new_path: str) -> None:
    """Talent Agent: the replaced resume becomes the profile's current version."""
    from app.models.talent import CvVersion
    from app.talent.cv import snapshot

    versions = session.query(CvVersion).filter(CvVersion.profile_id == profile.id).all()
    if not versions:
        return  # not tracked yet: the agent's baseline will start from the new resume
    pending = [o for o in session.new if isinstance(o, CvVersion) and o.profile_id == profile.id]
    if any(same_file(v.file_path, new_path) for v in versions + pending):
        return  # the agent's own pipeline made this change and manages its versions
    for v in versions:
        if v.is_current:
            v.is_current = False
    session.add(CvVersion(
        profile_id=profile.id, version_no=max(v.version_no for v in versions) + 1, is_current=True,
        file_path=new_path, file_hash=profile.file_hash, source="resume_upload", parse_status="parsed",
        parsed=snapshot(profile), received_at=datetime.utcnow(), retention_until=profile.retention_until,
    ))


@event.listens_for(Session, "before_flush")
def _follow_replaced_resumes(session: Session, flush_context, instances) -> None:
    changed = [o for o in session.dirty if isinstance(o, ResumeMetadata) and o.id is not None
               and sa.inspect(o).attrs.resume_file_path.history.has_changes()]
    if not changed:
        return
    conn = session.connection()
    rm, cand = ResumeMetadata.__table__, Candidate.__table__
    with session.no_autoflush:
        for profile in changed:
            new_path = profile.resume_file_path
            old_path = conn.execute(sa.select(rm.c.resume_file_path).where(rm.c.id == profile.id)).scalar()
            if not new_path or same_file(old_path, new_path):
                continue
            conn.execute(cand.update().where(cand.c.resume_metadata_id == profile.id).values(resume_path=new_path))
            for obj in list(session.identity_map.values()):
                if (isinstance(obj, Candidate) and obj.resume_metadata_id == profile.id
                        and not sa.inspect(obj).attrs.resume_path.history.has_changes()):
                    set_committed_value(obj, "resume_path", new_path)
            _record_cv_version(session, profile, new_path)
            if old_path:
                session.info.setdefault(_OLD_FILES, set()).add(old_path)


def _remove_if_unused(path: str) -> bool:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if resume_file_in_use(db, path):
            return False
        return safe_unlink_upload(path, ("resumes",))
    finally:
        db.close()


@event.listens_for(Session, "after_commit")
def _remove_replaced_files(session: Session) -> None:
    for path in session.info.pop(_OLD_FILES, ()) or ():
        try:
            _remove_if_unused(path)
        except Exception as exc:  # cleanup must never fail the request that committed
            logger.warning("Could not clean up a replaced resume file: %s", type(exc).__name__)


@event.listens_for(Session, "after_rollback")
def _forget_replaced_files(session: Session) -> None:
    session.info.pop(_OLD_FILES, None)


# ── Repair ───────────────────────────────────────────────────────────────────

def repair_links(db: Session, *, apply: bool = False) -> dict:
    """Job candidates whose resume file is missing but whose linked profile has
    one: point them at the profile's current file."""
    fixed, unrecoverable = [], 0
    rows = (db.query(Candidate, ResumeMetadata)
            .join(ResumeMetadata, ResumeMetadata.id == Candidate.resume_metadata_id)
            .filter(Candidate.resume_path.isnot(None)))
    for candidate, profile in rows.yield_per(500):
        if resolve_upload_file(candidate.resume_path, ("resumes",)):
            continue
        if not resolve_upload_file(profile.resume_file_path, ("resumes",)):
            unrecoverable += 1
            continue
        fixed.append(candidate.id)
        if apply:
            candidate.resume_path = profile.resume_file_path
    if apply:
        db.commit()
    return {"fixed" if apply else "would_fix": len(fixed), "missing_everywhere": unrecoverable,
            "candidate_ids": fixed[:50]}


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "repair":
        print(__doc__)
        sys.exit(2)
    import app.models  # noqa: F401  (register every mapper)
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        print(repair_links(session, apply="--apply" in sys.argv))
    finally:
        session.close()
