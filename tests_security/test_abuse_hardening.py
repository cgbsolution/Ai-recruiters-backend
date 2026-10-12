"""Regression tests for the 2026-09-19 security audit: account takeover and
brute force, candidate-session abuse, cost abuse, injection into emails and
exports, and denial-of-service through hostile uploads.

Every test builds its own rows, so the shared `world` fixture is left as the
tests in test_security_hardening.py expect it.
"""
import io
import json
import os
import secrets
import struct
import time
import zipfile
import zlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.database import SessionLocal
from app.models import Candidate, InterviewSession, Organization, ResumeMetadata, User, UserRole
from app.models.interview import Evaluation, SessionStatus
from app.models.observer import ObserverToken
from app.refs import encode_ref


# ── helpers ──────────────────────────────────────────────────────────────────

def _user(email, org_id, role=UserRole.admin, password="correct-horse-battery"):
    db = SessionLocal()
    try:
        u = User(name=email.split("@")[0], email=email, password_hash=hash_password(password), role=role,
                 organization_id=org_id, is_active=True, is_email_verified=True)
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _candidate_with_session(world, status=SessionStatus.pending, **cand_fields):
    """A fresh candidate on job A with a session and its access token."""
    db = SessionLocal()
    try:
        from app.models import InterviewToken

        tok = db.query(InterviewToken).filter(InterviewToken.token == world.tok_a).first()
        c = Candidate(name=cand_fields.pop("name", "Probe"), email=f"probe-{secrets.token_hex(4)}@mailbox.co",
                      job_id=world.job_a, interview_status="Scheduled", **cand_fields)
        db.add(c)
        db.flush()
        token = secrets.token_urlsafe(32)
        s = InterviewSession(token_id=tok.id, candidate_id=c.id, job_id=world.job_a, status=status,
                             session_metadata=json.dumps({"access_token": token}),
                             completed_at=datetime.utcnow() if status == SessionStatus.completed else None)
        db.add(s)
        db.commit()
        return c.id, s.id, {"X-Session-Token": token}
    finally:
        db.close()


def _proxied_client():
    """The app wrapped the way production runs it: uvicorn --proxy-headers
    --forwarded-allow-ips="*" (X-Forwarded-For becomes the client address)."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    from app.main import app

    return TestClient(ProxyHeadersMiddleware(app, trusted_hosts="*"))


# ── Accounts ─────────────────────────────────────────────────────────────────

def test_login_locks_the_account_even_when_the_client_ip_rotates(client, world):
    from app.ratelimit import failed_logins

    email = "lockout@corpmail.co"
    _user(email, world.org_a)
    proxied = _proxied_client()
    try:
        codes = [
            proxied.post("/auth/login", json={"email": email, "password": "wrong-guess"},
                         headers={"X-Forwarded-For": f"10.7.{i}.1"}).status_code
            for i in range(11)
        ]
        assert codes[:10] == [401] * 10 and codes[10] == 429, codes
        right = proxied.post("/auth/login", json={"email": email, "password": "correct-horse-battery"},
                             headers={"X-Forwarded-For": "10.8.0.1"})
        assert right.status_code == 429
    finally:
        failed_logins.reset(email)
    assert client.post("/auth/login", json={"email": email, "password": "correct-horse-battery"}).status_code == 200


def test_password_change_revokes_earlier_tokens(client, world):
    email = "rotate@corpmail.co"
    _user(email, world.org_a)
    old = client.post("/auth/login", json={"email": email, "password": "correct-horse-battery"}).json()
    old_headers = {"Authorization": f"Bearer {old['access_token']}"}
    assert client.get("/auth/me", headers=old_headers).status_code == 200

    r = client.post("/auth/change-password", headers=old_headers,
                    json={"current_password": "correct-horse-battery", "new_password": "An0ther-long-pass"})
    assert r.status_code == 200, r.text
    assert client.get("/auth/me", headers=old_headers).status_code == 401
    fresh = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/auth/me", headers=fresh).status_code == 200


def test_short_and_overlong_passwords_are_refused_cleanly(client, world):
    email = "policy@corpmail.co"
    _user(email, world.org_a)
    headers = {"Authorization": "Bearer " + client.post(
        "/auth/login", json={"email": email, "password": "correct-horse-battery"}).json()["access_token"]}
    for bad in ("short", "x" * 100):
        r = client.post("/auth/change-password", headers=headers,
                        json={"current_password": "correct-horse-battery", "new_password": bad})
        assert r.status_code == 422, (len(bad), r.status_code)


def test_reset_mails_are_capped_per_address_with_the_same_reply(client, world):
    from app.ratelimit import password_reset_requests

    email = "bombed@corpmail.co"
    _user(email, world.org_a)
    # 4 calls: over the 3-per-address cap, under the 5/minute per-IP limit.
    replies = {client.post("/auth/forgot-password", json={"email": email}).text for _ in range(4)}
    assert len(replies) == 1  # nothing reveals the cap (or the account)
    assert password_reset_requests.blocked_for(email) > 0
    password_reset_requests.reset(email)


# ── Candidate interview surface ──────────────────────────────────────────────

def test_status_alone_does_not_open_email_only_access(client, world):
    r = client.post(f"/interview/{world.tok_a}/verify-access", json={"email": world.status_only_email})
    assert r.status_code == 200
    assert r.json()["access"] == "invite_required"
    assert not r.json().get("session_token")


def test_tts_needs_a_live_session_token(client, world):
    assert client.post("/interview/tts", json={"text": "hello"}).status_code == 403
    r = client.post(f"/interview/tts?session_id={world.sess_a}", json={"text": "hello"},
                    headers={"X-Session-Token": "wrong"})
    assert r.status_code == 403


def test_candidate_sees_only_whether_they_met_the_threshold(client, world):
    cand_id, sess_id, headers = _candidate_with_session(world, status=SessionStatus.completed)
    db = SessionLocal()
    try:
        db.add(Evaluation(session_id=sess_id, candidate_id=cand_id, overall_score=31.0,
                          recommendation="No Hire", weaknesses=json.dumps(["everything"]), meets_threshold=False))
        db.commit()
    finally:
        db.close()
    body = client.get(f"/interview/sessions/{sess_id}/evaluation", headers=headers).json()
    assert body["meets_threshold"] is False
    assert not {"overall_score", "recommendation", "weaknesses", "detailed_feedback"} & set(body)


def test_finished_interview_cannot_be_turned_into_a_cheating_termination(client, world):
    _, sess_id, headers = _candidate_with_session(world, status=SessionStatus.completed)
    db = SessionLocal()
    try:
        db.get(InterviewSession, sess_id).violation_count = 4
        db.commit()
    finally:
        db.close()
    r = client.post(f"/interview/sessions/{sess_id}/violation", headers=headers,
                    json={"violation_type": "tab_switch", "details": "late"})
    assert r.status_code == 200 and r.json()["should_terminate"] is False
    db = SessionLocal()
    try:
        assert db.get(InterviewSession, sess_id).status == SessionStatus.completed
    finally:
        db.close()


def test_coding_submissions_close_with_the_interview(client, world):
    _, sess_id, headers = _candidate_with_session(world, status=SessionStatus.completed)
    r = client.post(f"/interview/sessions/{sess_id}/save-coding-submission", headers=headers,
                    json={"code": "print(1)", "test_result": {"total": 10, "passed": 10}})
    assert r.status_code == 409


def test_pairing_code_stops_working_when_the_interview_ends(client, world):
    _, sess_id, headers = _candidate_with_session(world)
    code = client.post(f"/secondary-cam/{sess_id}/init", headers=headers).json()["pairing_code"]
    assert client.get(f"/secondary-cam/connect/{code}").status_code == 200
    db = SessionLocal()
    try:
        s = db.get(InterviewSession, sess_id)
        s.status, s.completed_at = SessionStatus.completed, datetime.utcnow()
        db.commit()
    finally:
        db.close()
    assert client.get(f"/secondary-cam/connect/{code}").status_code == 404
    assert client.post(f"/secondary-cam/connect/{code}/heartbeat").status_code == 404


def _real_pdf() -> bytes:
    """Uploads are opened and inspected (app.services.file_safety): a real PDF."""
    import io

    import pikepdf

    buf = io.BytesIO()
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(buf)
    return buf.getvalue()


def test_public_apply_neither_overwrites_records_nor_reuses_uploads(client, world):
    db = SessionLocal()
    try:
        db.add(ResumeMetadata(organization_id=world.org_a, candidate_email="target@mailbox.co",
                              candidate_name="Real Name", phone="111",
                              resume_file_path=os.path.join("uploads", "resumes", "real.pdf"),
                              parsing_status="completed"))
        db.commit()
    finally:
        db.close()

    not_pdf = client.post(f"/interview/{world.tok_a}/upload-resume",
                          files={"file": ("cv.pdf", b"<html>not a pdf</html>", "application/pdf")})
    assert not_pdf.status_code == 400

    up = client.post(f"/interview/{world.tok_a}/upload-resume",
                     files={"file": ("cv.pdf", _real_pdf(), "application/pdf")})
    assert up.status_code == 200, up.text
    path = up.json()["resume_path"]

    applied = client.post(f"/interview/{world.tok_a}/apply",
                          json={"name": "Evil", "email": "target@mailbox.co", "phone": "999", "resume_path": path})
    assert applied.status_code == 200, applied.text
    db = SessionLocal()
    try:
        meta = db.query(ResumeMetadata).filter(ResumeMetadata.candidate_email == "target@mailbox.co").one()
        assert (meta.candidate_name, meta.phone) == ("Real Name", "111")
    finally:
        db.close()

    replay = client.post(f"/interview/{world.tok_a}/apply",
                         json={"name": "Spam", "email": "spam1@mailbox.co", "resume_path": path})
    assert replay.status_code == 400


# ── Recruiter-side ───────────────────────────────────────────────────────────

def test_bad_candidate_status_is_a_422_without_sql(client, world):
    r = client.put(f"/candidates/{world.cand_a_ref}", json={"status": "probe"}, headers=world.admin_a)
    assert r.status_code == 422
    assert "SELECT" not in r.text and "UPDATE" not in r.text


def test_cancelling_revokes_observer_links_and_links_expire(client, world):
    cand_id, _, _ = _candidate_with_session(world)
    ref = encode_ref("candidate", cand_id)
    link = client.post(f"/candidates/{ref}/observer-link/resend", headers=world.admin_a)
    assert link.status_code == 200, link.text
    token = link.json()["link"].rsplit("/", 1)[-1]
    db = SessionLocal()
    try:
        assert db.query(ObserverToken).filter(ObserverToken.token == token).one().expires_at is not None
    finally:
        db.close()
    assert client.get(f"/observe/{token}").status_code == 200

    assert client.post(f"/candidates/{ref}/cancel-interview", json={}, headers=world.admin_a).status_code == 200
    assert client.get(f"/observe/{token}").status_code == 410


def test_api_responses_carry_security_headers(client):
    h = client.get("/health").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"


# ── Hostile files ────────────────────────────────────────────────────────────

def test_resume_parser_is_linear_on_hostile_text():
    from app.services import fast_resume_parser as p

    for fn, text in ((p.extract_experience_years, "1" * 20000), (p.extract_email, "a" * 50000),
                     (p.extract_social_links, "a" * 50000)):
        started = time.monotonic()
        fn(text)
        assert time.monotonic() - started < 1.0, fn.__name__
    assert p.extract_experience_years("5+ years of experience") == 5.0
    assert p.extract_experience_years("2015 years of experience") is None


def test_docx_zip_bomb_is_refused(tmp_path):
    from app.services.resume_service import extract_text

    bomb = tmp_path / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open("word/document.xml", "w") as fh:
            for _ in range(70):
                fh.write(b"\0" * (1024 * 1024))
    assert bomb.stat().st_size < 1024 * 1024
    with pytest.raises(RuntimeError, match="too large"):
        extract_text(str(bomb))


def _png(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0")) + chunk(b"IEND", b"")


def test_decompression_bomb_images_are_refused_before_decoding():
    from app.utils.face_detection import image_size_is_safe

    assert image_size_is_safe(io.BytesIO(_png(640, 480)))
    assert not image_size_is_safe(io.BytesIO(_png(30000, 30000)))
    assert not image_size_is_safe(io.BytesIO(b"not an image"))
