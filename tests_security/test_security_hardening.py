"""Regression tests for the September 2026 authorization hardening.

Each test pins one attacker-visible behaviour from the audit: changing an id in
a URL, reading another tenant's files, bypassing the candidate session token,
or entering someone else's interview with just their email.
"""
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from app.database import SessionLocal
from app.media import _signature
from app.models import Candidate, Organization, Question
from app.refs import encode_ref

NOT_OK = {401, 403, 404, 405, 422}


# ── Tenant isolation ─────────────────────────────────────────────────────────

def test_other_tenants_job_is_not_found(client, world):
    for job in (world.job_b, world.job_b_ref):
        assert client.get(f"/jobs/{job}", headers=world.admin_a).status_code == 404
    assert client.delete(f"/jobs/{world.job_b_ref}", headers=world.admin_a).status_code == 404
    own = client.get(f"/jobs/{world.job_a_ref}", headers=world.admin_a)
    assert own.status_code == 200
    assert own.json()["ref"] == world.job_a_ref
    assert client.get(f"/jobs/{world.job_b_ref}", headers=world.admin_b).status_code == 200  # not deleted


def test_forged_or_cross_kind_refs_are_rejected(client, world):
    forged = "Z" * 18
    assert client.get(f"/jobs/{forged}", headers=world.admin_a).status_code == 422
    assert client.get(f"/jobs/{world.cand_a_ref}", headers=world.admin_a).status_code == 422
    assert client.get("/jobs/+1", headers=world.admin_a).status_code == 422


def test_other_tenants_candidate_is_not_found(client, world):
    for path in ("", "/email-history", "/evaluation", "/job-associations"):
        r = client.get(f"/candidates/{world.cand_b_ref}{path}", headers=world.admin_a)
        assert r.status_code == 404, (path, r.status_code, r.text[:200])


def test_candidate_cannot_be_moved_or_created_into_another_tenants_job(client, world):
    moved = client.put(f"/candidates/{world.cand_a_ref}", json={"job_id": world.job_b}, headers=world.admin_a)
    assert moved.status_code == 404
    created = client.post("/candidates/", json={"name": "Mallory", "email": "mallory@mailbox.co",
                                                "job_id": world.job_b_ref}, headers=world.admin_a)
    assert created.status_code == 404
    db = SessionLocal()
    try:
        assert db.get(Candidate, world.cand_a).job_id == world.job_a
        assert db.query(Candidate).filter(Candidate.email == "mallory@mailbox.co").count() == 0
    finally:
        db.close()


def test_server_managed_file_paths_are_not_client_writable(client, world):
    db = SessionLocal()
    try:
        before = db.get(Candidate, world.cand_a)
        resume_before, recording_before = before.resume_path, before.recording_link
    finally:
        db.close()
    r = client.put(f"/candidates/{world.cand_a_ref}",
                   json={"resume_path": "../../app/main.py", "recording_link": "/uploads/recordings/999.webm"},
                   headers=world.admin_a)
    assert r.status_code in (200, 422)
    db = SessionLocal()
    try:
        after = db.get(Candidate, world.cand_a)
        assert (after.resume_path, after.recording_link) == (resume_before, recording_before)
    finally:
        db.close()


def test_candidate_list_is_scoped_and_carries_refs_and_signed_urls(client, world):
    r = client.get("/candidates/", params={"job_id": world.job_a_ref}, headers=world.admin_a)
    assert r.status_code == 200
    rows = r.json()
    assert rows and all(row["job_id"] == world.job_a for row in rows)
    assert world.cand_b not in {row["id"] for row in rows}
    alice = next(row for row in rows if row["id"] == world.cand_a)
    assert alice["ref"] == world.cand_a_ref and alice["job_ref"] == world.job_a_ref
    assert alice["recording_url"].startswith("/media/recordings/")
    assert alice["image_url"].startswith("/media/photos/")


def test_orgless_and_suspended_accounts_are_locked_out(client, world):
    for headers in (world.orphan, world.suspended):
        assert client.get("/jobs/", headers=headers).status_code == 403
        assert client.get(f"/candidates/{world.cand_a_ref}", headers=headers).status_code == 403


def test_global_super_admin_keeps_cross_tenant_access(client, world):
    assert client.get(f"/candidates/{world.cand_b_ref}", headers=world.root).status_code == 200


def test_question_bank_is_tenant_scoped(client, world):
    assert client.put(f"/questions/{world.q_b}", json={"text": "pwned"}, headers=world.admin_a).status_code == 404
    assert client.delete(f"/questions/{world.q_b}", headers=world.admin_a).status_code == 404
    db = SessionLocal()
    try:
        assert db.get(Question, world.q_b).text == "Org B question"
    finally:
        db.close()


def test_job_description_download_requires_auth_and_ownership(client, world):
    path = f"/jobs/{world.job_a_ref}/download-jd"
    assert client.get(path).status_code in (401, 403)
    assert client.get(path, headers=world.admin_b).status_code == 404
    assert client.get(path, headers=world.admin_a).status_code == 200


def test_organization_with_users_cannot_be_deleted(client, world):
    r = client.delete(f"/organizations/{encode_ref('org', world.org_off)}", headers=world.root)
    assert r.status_code == 409
    db = SessionLocal()
    try:
        assert db.get(Organization, world.org_off) is not None
    finally:
        db.close()


# ── Private media ────────────────────────────────────────────────────────────

def test_public_upload_and_recording_routes_are_gone(client, world):
    assert client.get(f"/uploads/recordings/{world.sess_b}.webm").status_code in (404, 405)
    assert client.get(f"/uploads/photos/{world.cand_b}.jpg").status_code in (404, 405)
    assert client.get(f"/recordings/{world.sess_b}.webm/download").status_code in (404, 405)
    assert client.post("/recordings/fix-all").status_code in (404, 405)


def _signed_recording_url(client, world):
    body = client.get(f"/candidates/{world.cand_a_ref}", headers=world.admin_a).json()
    return body["recording_url"]


def test_signed_media_url_serves_the_file_with_range_support(client, world):
    url = _signed_recording_url(client, world)
    full = client.get(url)
    assert full.status_code == 200
    assert full.content.startswith(b"webm-bytes-a-")
    assert full.headers["x-content-type-options"] == "nosniff"
    part = client.get(url, headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == b"webm-bytes"
    assert client.head(url).status_code == 200


def test_delivery_options_are_part_of_the_signature(client, world):
    """An inline viewing link must not also authorize the download or the
    expensive ?format=mp4 transcode — those need their own signature."""
    from app.media import sign_media_url

    inline = _signed_recording_url(client, world)
    assert client.get(inline + "&dl=1").status_code == 403
    assert client.get(inline + "&format=mp4").status_code == 403

    signed_download = sign_media_url(f"/uploads/recordings/{world.sess_a}.webm", download=True)
    got = client.get(signed_download)
    assert got.status_code == 200
    assert "attachment" in got.headers.get("content-disposition", "")


def test_tampered_swapped_or_expired_media_urls_are_refused(client, world):
    url = _signed_recording_url(client, world)
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}

    tampered = dict(query, sig=query["sig"][:-1] + ("A" if query["sig"][-1] != "A" else "B"))
    assert client.get(f"{parts.path}?{urlencode(tampered)}").status_code == 403

    other_file = f"/media/recordings/{world.sess_b}.webm"
    assert client.get(f"{other_file}?{parts.query}").status_code == 403

    expired_exp = int(time.time()) - 60
    rel = parts.path[len("/media/"):]
    expired = {"exp": expired_exp, "sig": _signature(rel, expired_exp)}
    assert client.get(f"{parts.path}?{urlencode(expired)}").status_code == 403

    assert client.get(parts.path).status_code == 403  # no signature at all


# ── Candidate session tokens ─────────────────────────────────────────────────

def test_session_routes_require_the_session_token(client, world):
    sid, token = world.sess_a, world.sess_a_token
    assert client.get(f"/interview/sessions/{sid}/resume").status_code == 403
    assert client.get(f"/interview/sessions/{sid}/resume", headers={"X-Session-Token": "wrong"}).status_code == 403
    assert client.get(f"/interview/sessions/{sid}/resume",
                      headers={"X-Session-Token": world.sess_b_token}).status_code == 403
    assert client.get(f"/interview/sessions/{sid}/resume", headers={"X-Session-Token": token}).status_code == 200


def test_session_id_spelling_variants_do_not_bypass_the_token(client, world):
    sid = world.sess_a
    for variant in (f"+{sid}", f"{sid}.0", f"%20{sid}", f"{sid}%20", f"0{sid}", f"%2B{sid}"):
        r = client.get(f"/interview/sessions/{variant}/resume")
        assert r.status_code in NOT_OK, (variant, r.status_code)
    upload = client.post(f"/interview/sessions/{sid}/upload-recording",
                         files={"file": ("x.webm", b"0" * 4096, "video/webm")})
    assert upload.status_code == 403


def test_coding_submissions_are_admin_only_and_tenant_scoped(client, world):
    path = f"/interview/sessions/{world.sess_a}/coding-submissions"
    assert client.get(path).status_code in (401, 403)
    assert client.get(path, headers={"X-Session-Token": world.sess_a_token}).status_code in (401, 403)
    assert client.get(path, headers=world.admin_b).status_code == 404
    assert client.get(path, headers=world.admin_a).status_code == 200


def test_secondary_camera_init_requires_the_session_token(client, world):
    path = f"/secondary-cam/{world.sess_a}/init"
    assert client.post(path).status_code == 403
    assert client.post(path, headers={"X-Session-Token": world.sess_b_token}).status_code == 403
    assert client.post(path, headers={"X-Session-Token": world.sess_a_token}).status_code in (200, 201)


def test_email_only_candidate_routes_are_removed(client, world):
    for route in ("register", "login", "auto-register"):
        r = client.post(f"/interview/{world.tok_a}/{route}", json={"email": world.legacy_email})
        assert r.status_code in (404, 405), (route, r.status_code)
    r = client.post(f"/interview/sessions/{world.sess_a}/submit-code",
                    json={"code": "print(1)", "language": "python"},
                    headers={"X-Session-Token": world.sess_a_token})
    assert r.status_code in (404, 405)


# ── Personal invite links ────────────────────────────────────────────────────

def _verify(client, token, **body):
    r = client.post(f"/interview/{token}/verify-access", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_personal_invite_link_admits_only_its_candidate(client, world):
    ok = _verify(client, world.tok_a, invite=world.invite)
    assert ok["access"] == "granted" and ok["session_token"]

    assert _verify(client, world.tok_a, invite="not-a-real-invite")["access"] == "invalid_invite"
    # Another tenant's (valid) invite used on this job's link.
    assert _verify(client, world.tok_a, invite=world.invite_b)["access"] == "invalid_invite"


def test_email_alone_cannot_open_an_invited_or_unscheduled_candidates_session(client, world):
    for email in (world.invited_email, world.fresh_email):
        res = _verify(client, world.tok_a, email=email)
        assert res["access"] == "invite_required", (email, res)
        assert not res.get("session_token") and not res.get("session_id") and not res.get("candidate_name")


def test_legacy_scheduled_candidate_can_still_use_their_emailed_link(client, world):
    res = _verify(client, world.tok_a, email=world.legacy_email)
    assert res["access"] == "granted" and res["session_token"]


def test_unknown_email_gets_apply_without_leaking_other_tenants_names(client, world):
    res = _verify(client, world.tok_a, email="stranger@mailbox.co")
    assert res["access"] == "apply"
    assert not res.get("candidate_name")


# ── Late recordings: rescue, but never overwrite ─────────────────────────────
# Kept last: it ends session A and rewrites its recording file.

def test_late_upload_rescues_a_missing_recording_but_never_replaces_one(client, world):
    """Orphan recovery re-uploads a recording the browser kept after a crash,
    often days later — the only way to rescue a video the server never got, so
    it must still be accepted. Once a recording exists, though, a candidate
    (including one auto-terminated for cheating, whose session token stays
    valid) must not be able to overwrite the evidence."""
    import os
    from datetime import datetime, timedelta

    from app.models.interview import InterviewSession, SessionStatus

    db = SessionLocal()
    try:
        session = db.get(InterviewSession, world.sess_a)
        session.status = SessionStatus.completed
        session.completed_at = datetime.utcnow() - timedelta(days=7)
        db.commit()
    finally:
        db.close()

    path = os.path.join("uploads", "recordings", f"{world.sess_a}.webm")
    os.remove(path)  # the server never received it
    headers = {"X-Session-Token": world.sess_a_token}

    rescued = client.post(
        f"/interview/sessions/{world.sess_a}/upload-recording",
        files={"file": ("interview.webm", b"rescued-" * 200, "video/webm")},
        data={"recovery": "true"}, headers=headers,
    )
    assert rescued.status_code == 200, rescued.text
    assert os.path.isfile(path)

    replaced = client.post(
        f"/interview/sessions/{world.sess_a}/upload-recording",
        files={"file": ("interview.webm", b"tampered-" * 500, "video/webm")},
        data={"recovery": "true"}, headers=headers,
    )
    assert replaced.status_code == 409, replaced.text
    with open(path, "rb") as fh:
        assert fh.read().startswith(b"rescued-")
