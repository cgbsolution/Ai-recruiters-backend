"""Phase 0: report, backfill, merge — and every deletion is logged."""
from datetime import datetime, timedelta

from app.models import ResumeMetadata
from app.models.candidate import Candidate
from app.models.talent import AuditLog, ContactConsent
from app.models.user import UserRole
from app.talent import identity, phase0


def test_identity_normalization():
    assert identity.canonical_email("John.Doe+jobs@GoogleMail.com") == "johndoe@gmail.com"
    assert identity.canonical_email("John.Doe@outlook.com") == "john.doe@outlook.com"
    assert identity.normalize_phone("+91 98765-43210") == "919876543210"
    assert identity.normalize_phone("098765 43210") == "919876543210"
    assert identity.normalize_phone("12345") is None
    assert identity.dedupe_key("a.b@gmail.com", None) == identity.dedupe_key("ab+x@gmail.com", "999")
    assert identity.dedupe_key("noemail_1@pending.local", "9876543210") == identity.dedupe_key(None, "+919876543210")
    assert identity.suppression_hash("email", "A.B@gmail.com") == identity.suppression_hash("email", "ab@gmail.com")
    assert identity.classify_email("noemail_x@pending.local") == "placeholder"
    assert identity.classify_email("not-an-email") == "invalid"
    assert identity.classify_email("x@mailinator.com") == "invalid"
    assert identity.classify_email("careers@acme.com") == "role"
    assert identity.classify_email("asha@acme.com") == "valid"


def test_backfill_reports_counts_and_classifies_freshness(world, db):
    org = world.org()
    now = datetime.utcnow()
    fresh = world.profile(org, email="fresh@acme.com", created_at=now - timedelta(days=30))
    stale = world.profile(org, email="stale@acme.com", created_at=now - timedelta(days=400))
    bad = world.profile(org, email="noemail_1@pending.local", created_at=now)
    bad_with_phone = world.profile(org, email="broken", phone="9876543210", created_at=now)
    pending = world.profile(org, email="noemail_2@pending.local", parsing_status="pending")

    counts = phase0.backfill(db, org.id, actor="test")
    assert counts["profiles"] == 4  # the unparsed placeholder row is not a profile yet
    assert counts["email_placeholder"] == 1 and counts["email_invalid"] == 1 and counts["email_valid"] == 2

    def status(m):
        db.expire_all()
        return db.get(ResumeMetadata, m.id)

    assert status(fresh).verification_status == "fresh"
    assert status(stale).verification_status == "stale"
    assert status(bad).verification_status == "unreachable"
    assert status(bad_with_phone).verification_status == "fresh"  # a phone still reaches them
    assert status(pending).dedupe_key is None
    assert status(fresh).dedupe_key == identity.dedupe_key("fresh@acme.com", None)
    assert status(fresh).retention_until is not None
    assert db.query(AuditLog).filter(AuditLog.action == "phase0.backfill").count() == 1


def test_report_finds_duplicate_groups(world, db):
    org = world.org()
    a = world.profile(org, email="asha.rao@gmail.com")
    b = world.profile(org, email="asharao+naukri@gmail.com")
    c = world.profile(org, email="other@acme.com", phone="+91 98765 43210")
    d = world.profile(org, email="someone@acme.com", phone="9876543210")
    e = world.profile(org, email="x1@acme.com", file_hash="f" * 64)
    f = world.profile(org, email="x2@acme.com", file_hash="f" * 64)
    other_org = world.org("Other")
    world.profile(other_org, email="asha.rao@gmail.com")  # another tenant: never counted

    rep = phase0.report(db, org.id)
    assert rep["profiles"] == 6
    assert rep["duplicates"]["same_email"]["groups"] == 1
    assert rep["duplicates"]["same_email"]["sample"] == [sorted([a.id, b.id])]
    assert rep["duplicates"]["same_phone_different_email"]["sample"] == [sorted([c.id, d.id])]
    assert rep["duplicates"]["same_file"]["sample"] == [sorted([e.id, f.id])]


def test_merge_is_dry_run_by_default_and_logs_every_deletion(world, db):
    org = world.org()
    now = datetime.utcnow()
    old = world.profile(org, email="asha.rao@gmail.com", created_at=now - timedelta(days=90))
    new = world.profile(org, email="asharao@gmail.com", created_at=now - timedelta(days=1))
    db.add(ContactConsent(profile_id=old.id, channel="email", purpose="jobs", status="granted", source="test"))
    app_row = Candidate(name="A", email="asha.rao@gmail.com", resume_metadata_id=old.id)
    db.add(app_row)
    db.commit()

    dry = phase0.merge_duplicates(db, org.id, actor="user:7")
    assert dry["rows_to_delete"] == 1 and dry["plan"] == [{"keep": new.id, "delete": [old.id]}]
    assert db.query(ResumeMetadata).count() == 2

    done = phase0.merge_duplicates(db, org.id, actor="user:7", dry_run=False)
    assert done["deleted"] == 1
    db.expire_all()
    assert db.get(ResumeMetadata, old.id) is None
    assert db.get(Candidate, app_row.id).resume_metadata_id == new.id
    assert db.query(ContactConsent).filter_by(profile_id=new.id, purpose="jobs").count() == 1

    deleted_log = db.query(AuditLog).filter_by(action="profile.deleted", subject_id=str(old.id)).one()
    assert deleted_log.actor == "user:7"
    assert deleted_log.detail["reason"] == "phase0_duplicate_merge"
    merged = db.query(AuditLog).filter_by(action="profile.merged").one()
    assert merged.detail["deleted_profile_ids"] == [old.id]


def test_existing_delete_endpoints_are_logged_by_the_trigger(client, world, db):
    org = world.org()
    admin = world.user(org)
    one = world.profile(org, email="one@acme.com")
    two = world.profile(org, email="two@acme.com")
    three = world.profile(org, email="three@acme.com")

    r = client.delete(f"/resume-database/{one.id}", headers=world.headers(admin))
    assert r.status_code == 204, r.text
    r = client.post("/resume-database/bulk/delete", json={"ids": [two.id, three.id]}, headers=world.headers(admin))
    assert r.status_code == 200, r.text
    logged = {row.subject_id for row in db.query(AuditLog).filter_by(action="profile.deleted")}
    assert logged == {str(one.id), str(two.id), str(three.id)}


def test_phase0_api_is_admin_only_and_org_scoped(client, world):
    org, other = world.org(), world.org("Other")
    admin, recruiter, foreign = world.user(org), world.user(org, UserRole.recruiter), world.user(other)
    world.profile(org, email="a.b@gmail.com")
    world.profile(org, email="ab@gmail.com")

    r = client.get("/talent/phase0/report", headers=world.headers(admin))
    assert r.status_code == 200 and r.json()["duplicates"]["same_email"]["groups"] == 1
    assert client.get("/talent/phase0/report", headers=world.headers(foreign)).json()["profiles"] == 0
    r = client.get(f"/talent/phase0/report?organization_id={org.id}", headers=world.headers(foreign))
    assert r.status_code == 404
    assert client.post("/talent/phase0/backfill", json={}, headers=world.headers(recruiter)).status_code == 403
    assert client.post("/talent/phase0/backfill", json={}, headers=world.headers(admin)).status_code == 200

    no_db = world.org("NoDB", resume_db=False)
    assert client.get("/talent/phase0/report", headers=world.headers(world.user(no_db))).status_code == 403


def test_candidate_confirmed_values_survive_cv_rereads(world, db):
    from app.models.resume_metadata import mark_candidate_confirmation

    org = world.org()
    meta = world.profile(org, email="c@acme.com", raw_text="Current CTC: 6 LPA. Notice period: 90 days.")
    db.refresh(meta)
    assert meta.current_ctc == 6 and meta.notice_period_days == 90

    mark_candidate_confirmation(db)
    meta.current_ctc = 9.5
    meta.notice_period_days = 30
    meta.current_company = "Newco"
    db.commit()
    db.refresh(meta)
    assert set(identity_fields(meta.candidate_fields)) == {"current_ctc", "notice_period_days", "current_company"}

    meta.raw_text = "Current CTC: 6 LPA. Notice period: 90 days. Worked at Oldco."  # a CV re-read
    meta.pipeline_status = "shortlisted"
    db.commit()
    db.refresh(meta)
    assert (meta.current_ctc, meta.notice_period_days, meta.current_company) == (9.5, 30, "Newco")


def identity_fields(raw):
    import json

    return json.loads(raw or "[]")
