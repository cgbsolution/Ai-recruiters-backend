"""
Phase 0 — inspect and clean the talent pool before anything is sent.

    report()            duplicate groups and email validity, read-only
    backfill()          dedupe_key, email_status, freshness status, retention date
    merge_duplicates()  fold same-person rows (same canonical email) into the
                        newest one; dry run by default; every deletion is logged
                        (the resume_metadata delete trigger + a profile.merged entry)

Freshness is anchored on when we last heard from the candidate:
last_verified_at, or for never-verified rows the date their CV arrived
(resume_metadata.created_at). Rows older than the org's refresh window are
`stale`; rows with no sendable email and no phone are `unreachable`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.resume_metadata import ResumeMetadata as RM
from app.models.talent import ContactConsent
from app.talent import identity
from app.talent.audit import audit
from app.talent.schema import set_audit_actor
from app.talent.settings import get_settings

logger = logging.getLogger("app.talent.phase0")

# Rows still waiting for their first parse are bulk-upload placeholders.
_PARSED = sa.or_(RM.parsing_status.notin_(("pending", "processing")), RM.parsing_status.is_(None))
# Statuses the agent owns once a profile is in motion; a backfill rerun keeps them.
_AGENT_OWNED = ("refreshing",)


def _scoped(q, organization_id: Optional[int]):
    return q.filter(RM.organization_id == organization_id) if organization_id is not None else q


def classify_freshness(
    email_status: str, phone: Optional[str], last_verified_at: Optional[datetime],
    received_at: Optional[datetime], now: datetime, window: timedelta,
) -> str:
    if email_status not in identity.EMAIL_SENDABLE and not identity.normalize_phone(phone):
        return "unreachable"
    anchor = last_verified_at or received_at
    if anchor is None:
        return "stale"
    return "fresh" if anchor >= now - window else "stale"


def backfill(
    db: Session, organization_id: Optional[int], *, check_mx: bool = False, actor: str = "system",
    batch_size: int = 500, now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Compute identity and freshness columns for every parsed profile in the
    org (all orgs for None). Plain UPDATEs: none of these columns feed the
    derived search fields, so the before_flush profile hook is not needed."""
    now = now or datetime.utcnow()
    table = RM.__table__
    counts: Dict[str, int] = defaultdict(int)
    settings_cache: Dict[Optional[int], object] = {}
    stmt = (
        table.update()
        .where(table.c.id == sa.bindparam("_id"))
        .values(
            dedupe_key=sa.bindparam("_dk"),
            email_status=sa.bindparam("_es"),
            email_checked_at=sa.bindparam("_ec"),
            verification_status=sa.bindparam("_vs"),
            retention_until=sa.bindparam("_ru"),
        )
    )
    last_id = 0
    while True:
        rows = (
            _scoped(db.query(
                RM.id, RM.organization_id, RM.candidate_email, RM.phone, RM.last_verified_at,
                RM.created_at, RM.verification_status, RM.retention_until,
            ), organization_id)
            .filter(RM.id > last_id, _PARSED)
            .order_by(RM.id)
            .limit(batch_size)
            .all()
        )
        if not rows:
            break
        params = []
        for r in rows:
            org_settings = settings_cache.get(r.organization_id)
            if org_settings is None:
                org_settings = settings_cache[r.organization_id] = get_settings(db, r.organization_id)
            window = timedelta(days=int(org_settings.refresh_after_days or 90))
            status = identity.classify_email(r.candidate_email, check_mx=check_mx)
            fresh = classify_freshness(status, r.phone, r.last_verified_at, r.created_at, now, window)
            if r.verification_status in _AGENT_OWNED:
                fresh = r.verification_status
            retention = r.retention_until or (
                (r.last_verified_at or r.created_at or now) + timedelta(days=30 * int(org_settings.retention_months or 24))
            )
            params.append({
                "_id": r.id, "_dk": identity.dedupe_key(r.candidate_email, r.phone), "_es": status,
                "_ec": now, "_vs": fresh, "_ru": retention,
            })
            counts[f"email_{status}"] += 1
            counts[f"status_{fresh}"] += 1
            counts["profiles"] += 1
        db.execute(stmt, params)
        db.commit()
        last_id = rows[-1].id
    audit(
        db, actor=actor, action="phase0.backfill", subject_type="organization",
        subject_id=organization_id, organization_id=organization_id,
        detail={"counts": dict(counts), "check_mx": check_mx}, commit=True,
    )
    return dict(counts)


def _rows_for_report(db: Session, organization_id: Optional[int]):
    return (
        _scoped(db.query(
            RM.id, RM.organization_id, RM.candidate_email, RM.phone, RM.file_hash, RM.created_at,
            RM.email_status,
        ), organization_id)
        .filter(_PARSED)
        .all()
    )


def report(db: Session, organization_id: Optional[int], sample: int = 25) -> dict:
    """Duplicate groups and email validity. Read-only; ids only."""
    rows = _rows_for_report(db, organization_id)
    by_email: Dict[tuple, List[int]] = defaultdict(list)
    by_phone: Dict[tuple, set] = defaultdict(set)
    by_file: Dict[tuple, List[int]] = defaultdict(list)
    email_counts: Dict[str, int] = defaultdict(int)
    for r in rows:
        status = r.email_status or identity.classify_email(r.candidate_email)
        email_counts[status] += 1
        canonical = identity.canonical_email(r.candidate_email)
        if canonical and not identity.is_placeholder_email(canonical):
            by_email[(r.organization_id, canonical)].append(r.id)
        phone = identity.normalize_phone(r.phone)
        if phone:
            by_phone[(r.organization_id, phone)].add(r.id)
        if r.file_hash:
            by_file[(r.organization_id, r.file_hash)].append(r.id)

    email_groups = [sorted(ids) for ids in by_email.values() if len(ids) > 1]
    # Same phone, but not already the same person by email.
    email_of = {i: key for key, ids in by_email.items() for i in ids}
    phone_groups = [
        sorted(ids) for ids in by_phone.values()
        if len(ids) > 1 and len({email_of.get(i, ("solo", i)) for i in ids}) > 1
    ]
    file_groups = [sorted(ids) for ids in by_file.values() if len(ids) > 1]
    invalid = sum(email_counts[s] for s in ("invalid", "placeholder", "no_mx"))
    return {
        "profiles": len(rows),
        "email_status": dict(email_counts),
        "invalid_or_unusable_emails": invalid,
        "duplicates": {
            "same_email": {"groups": len(email_groups), "rows": sum(len(g) for g in email_groups),
                           "removable": sum(len(g) - 1 for g in email_groups), "sample": email_groups[:sample]},
            "same_phone_different_email": {"groups": len(phone_groups), "rows": sum(len(g) for g in phone_groups),
                                           "sample": phone_groups[:sample],
                                           "note": "Review by hand: shared family / agency numbers are common."},
            "same_file": {"groups": len(file_groups), "rows": sum(len(g) for g in file_groups),
                          "sample": file_groups[:sample]},
        },
        "freshness": dict(
            _scoped(db.query(RM.verification_status, sa.func.count()), organization_id)
            .filter(_PARSED).group_by(RM.verification_status).all()
        ),
    }


def merge_duplicates(
    db: Session, organization_id: Optional[int], *, actor: str, dry_run: bool = True,
) -> dict:
    """Fold rows that are the same person by canonical email into the newest
    CV. The kept row gets the others' job applications and any consent it
    lacks. Phone-only matches are never merged automatically."""
    from app.media import resume_file_in_use, safe_unlink_upload

    rows = _rows_for_report(db, organization_id)
    groups: Dict[tuple, list] = defaultdict(list)
    for r in rows:
        canonical = identity.canonical_email(r.candidate_email)
        if canonical and not identity.is_placeholder_email(canonical):
            groups[(r.organization_id, canonical)].append(r)

    plan = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (m.created_at or datetime.min, m.id), reverse=True)
        plan.append((members[0].id, [m.id for m in members[1:]]))

    result = {"groups": len(plan), "rows_to_delete": sum(len(l) for _, l in plan), "dry_run": dry_run,
              "plan": [{"keep": k, "delete": l} for k, l in plan[:200]]}
    if dry_run or not plan:
        return result

    files: List[str] = []
    deleted = 0
    for keep_id, loser_ids in plan:
        set_audit_actor(db, actor, "phase0_duplicate_merge")
        db.query(Candidate).filter(Candidate.resume_metadata_id.in_(loser_ids)).update(
            {"resume_metadata_id": keep_id}, synchronize_session=False
        )
        kept_consent = {
            (c.channel, c.purpose) for c in db.query(ContactConsent).filter(ContactConsent.profile_id == keep_id)
        }
        for c in db.query(ContactConsent).filter(ContactConsent.profile_id.in_(loser_ids)).all():
            if (c.channel, c.purpose) not in kept_consent:
                db.add(ContactConsent(
                    profile_id=keep_id, channel=c.channel, purpose=c.purpose, status=c.status, source=c.source,
                    basis=c.basis, recorded_by=c.recorded_by, granted_at=c.granted_at, withdrawn_at=c.withdrawn_at,
                ))
                kept_consent.add((c.channel, c.purpose))
        losers = db.query(RM).filter(RM.id.in_(loser_ids)).all()
        org_id = losers[0].organization_id if losers else organization_id
        for meta in losers:
            if meta.resume_file_path:
                files.append(meta.resume_file_path)
            db.delete(meta)
        audit(
            db, actor=actor, action="profile.merged", subject_type="profile", subject_id=keep_id,
            organization_id=org_id, profile_id=keep_id,
            detail={"deleted_profile_ids": loser_ids, "reason": "same canonical email"},
        )
        db.commit()
        deleted += len(loser_ids)

    for path in set(files):
        try:
            if not resume_file_in_use(db, path):
                safe_unlink_upload(path, ("resumes",))
        except Exception as exc:  # file cleanup never undoes a merge
            logger.warning("Could not remove merged resume file: %s", exc)
    result["deleted"] = deleted
    return result
