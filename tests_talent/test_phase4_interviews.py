"""Phase 4 — async interviews, scoring with traceable evidence, overrides,
human-only outcomes, time limits and erasure of recordings."""
import ast
import asyncio
import json
import os
import pathlib
import re
import uuid
from datetime import datetime, timedelta

import pytest

from app.models.question import Question
from app.models.talent import AuditLog, InterviewBooking, JobMatch, TalentInterview, TalentJobSettings
from app.models.user import UserRole
from app.talent import approvals, decisions, interviews, scheduling, scoring
from tests_talent import fakes


def responder(params):
    system = params["system"][0]["text"]
    user = fakes.user_text(params)
    if "prepare a structured first-round interview" in system:
        n = int(re.search(r"Write exactly (\d+) questions", user).group(1))
        bank = json.loads(re.search(r"<untrusted_question_bank>\n(.*)\n</untrusted_question_bank>", user, re.S).group(1))
        qs = [{"text": b["text"], "must_have": "Kafka", "source": b["id"]} for b in bank[:2]]
        qs += [{"text": f"Generated question {i}?", "must_have": "Python", "source": "generated"} for i in range(n - len(qs))]
        return {"questions": qs}
    if "review one interview answer" in system:
        answer = re.search(r"<untrusted_answer>\n(.*)\n</untrusted_answer>", user, re.S).group(1)
        vague = len(answer) < 40
        return {"vague": vague, "follow_up": "Can you give one specific example of what you did?" if vague else ""}
    if "Score the interview transcript" in system:
        transcript = json.loads(re.search(r"<untrusted_transcript>\n(.*)\n</untrusted_transcript>", user, re.S).group(1))
        rubric = json.loads(re.search(r"<untrusted_rubric>\n(.*)\n</untrusted_rubric>", user, re.S).group(1))
        answers = [t["text"] for t in transcript if t["turn"] == "answer"]
        crits = []
        for i, c in enumerate(rubric["criteria"]):
            quote = " ".join(answers[i % len(answers)].split()[:6])
            crits.append({"criterion": c["criterion"], "score": 4 if i % 2 == 0 else 2, "confidence": 0.8,
                          "evidence": [quote] if i else [quote, "I single-handedly migrated the mainframe"],
                          "rationale": "Based on the answer."})
        crits[-1]["evidence"] = ["Something the candidate never said at all"]
        crits[-1]["score"] = 5
        return {"criteria": crits, "summary": "Solid streaming experience.",
                "integrity_observations": ["Answers 2 and 3 use near-identical phrasing."]}
    raise AssertionError("unexpected prompt")


def _scheduled_interview(world, db, *, mode="async_text", start_now=True, rubric=None):
    org = world.org()
    world.enable(org, "match", "invite", "interview")
    job = world.job(org, skills=("Kafka", "Python"))
    db.merge(TalentJobSettings(job_id=job.id, autonomy_mode="copilot", interview_mode=mode, question_count=8,
                               time_limit_minutes=30, slot_minutes=45, rubric=rubric))
    db.add(Question(text="Describe a Kafka consumer-lag incident you handled.", category="Kafka", difficulty="medium",
                    organization_id=org.id))
    p = world.profile(org, email="iv@acme.com", email_status="valid", verification_status="fresh",
                      candidate_name="Asha Rao")
    world.consent(p)
    m = JobMatch(job_id=job.id, profile_id=p.id, organization_id=org.id, ai_score=80, status="invited",
                 response="interested", risk_flags=[])
    db.add(m)
    db.commit()
    iv, url = scheduling.offer_interview(db, m.id)
    slot = db.query(InterviewBooking).filter_by(interview_id=iv.id).first()
    scheduling.book(db, iv.id, slot.id)
    if start_now:
        now = datetime.utcnow()
        iv = db.get(TalentInterview, iv.id)
        iv.scheduled_at, iv.slot_ends_at = now - timedelta(minutes=1), now + timedelta(minutes=44)
        db.commit()
    return org, job, p, m, db.get(TalentInterview, iv.id), url.rsplit("/", 1)[-1]


def test_async_interview_end_to_end(client, world, db, monkeypatch, events):
    fake = fakes.install(monkeypatch, responder)
    org, job, p, m, iv, token = _scheduled_interview(world, db)
    out = asyncio.run(interviews.prepare_questions(str(iv.id)))
    assert out == {"status": "prepared", "count": 8}
    db.expire_all()
    qs = db.get(TalentInterview, iv.id).questions
    assert qs[0]["source"].startswith("bank-") and len(qs) == 8

    state = client.get(f"/talent/public/interview/{token}").json()
    assert state["can_start"] and "AI interviewer" in state["disclosure"]
    r = client.post(f"/talent/public/interview/{token}/start",
                    json={"disclosure_acknowledged": True, "consent_to_record": False})
    assert r.status_code == 409  # no recording consent, no interview
    assert db.get(TalentInterview, iv.id).transcript is None
    r = client.post(f"/talent/public/interview/{token}/start",
                    json={"disclosure_acknowledged": True, "consent_to_record": True, "answer_mode": "text"})
    prog = r.json()
    assert prog["question"]["id"] == "q1" and prog["total"] == 8 and prog["seconds_left"] > 1700
    assert ("talent/interview.started", {"interview_id": str(iv.id)}) in events

    # A vague answer gets exactly one follow-up; the follow-up's answer is not followed up again.
    prog = client.post(f"/talent/public/interview/{token}/answer", data={"question_id": "q1", "text": "Yes, I did."}).json()
    assert prog["question"]["id"] == "q1-f" and prog["question"]["follow_up"]
    prog = client.post(f"/talent/public/interview/{token}/answer", data={"question_id": "q1-f", "text": "Ok."}).json()
    assert prog["question"]["id"] == "q2"
    assert client.post(f"/talent/public/interview/{token}/answer", data={"question_id": "q1", "text": "late"}).status_code == 409
    for i in range(2, 9):
        prog = client.post(f"/talent/public/interview/{token}/answer", data={
            "question_id": f"q{i}", "text": f"In my role at Northwind I built Kafka consumers in Python, case {i}, "
                                             "reducing lag from minutes to seconds."}).json()
    assert prog["status"] == "completed"
    db.expire_all()
    iv_db = db.get(TalentInterview, iv.id)
    assert iv_db.consent_recorded_at and iv_db.ai_disclosed_at and iv_db.completed_at
    assert db.get(JobMatch, m.id).status == "interviewed"
    assert ("talent/interview.completed", {"interview_id": str(iv.id)}) in events
    consent_log = db.query(AuditLog).filter_by(action="interview.consent_recorded").one()
    assert consent_log.detail["recording_consent"] is True

    # Scoring: every quote is checked against the transcript.
    result = asyncio.run(scoring.score_interview(str(iv.id)))
    assert result["status"] == "scored"
    db.expire_all()
    iv_db = db.get(TalentInterview, iv.id)
    assert iv_db.status == "scored" and iv_db.human_review_status == "pending"
    first, last = iv_db.scores[0], iv_db.scores[-1]
    assert first["evidence"][0]["verified"] and not first["evidence"][1]["verified"]
    assert not last["evidence"][0]["verified"] and last["confidence"] <= 0.3 and last["notes"]
    assert iv_db.overall_score == scoring.overall(iv_db.scores)
    assert iv_db.integrity_observations == ["Answers 2 and 3 use near-identical phrasing."]
    scored_log = db.query(AuditLog).filter_by(action="interview.scored").one()
    assert scored_log.detail["model"] and scored_log.detail["prompt_version"]
    # The scorer never saw the candidate's name.
    score_call = [c for c in fake.calls if "Score the interview transcript" in c["system"][0]["text"]][0]
    assert "Asha" not in fakes.user_text(score_call)
    assert db.get(JobMatch, m.id).status == "interviewed"  # a score changes no outcome


def test_recruiter_traces_evidence_and_overrides(client, world, db, monkeypatch):
    fakes.install(monkeypatch, responder)
    org, job, p, m, iv, token = _scheduled_interview(world, db)
    asyncio.run(interviews.prepare_questions(str(iv.id)))
    db.expire_all()
    iv = db.get(TalentInterview, iv.id)
    interviews.start(db, iv, disclosure_acknowledged=True, consent_to_record=True, answer_mode="text")
    for q in iv.questions:
        asyncio.run(interviews.answer(str(iv.id), q["id"], f"At Northwind I ran Kafka in production for question {q['id']}."))
    asyncio.run(scoring.score_interview(str(iv.id)))
    recruiter = world.user(org, UserRole.recruiter)
    h = world.headers(recruiter)
    detail = client.get(f"/talent/interviews/{iv.id}", headers=h).json()
    answers = [t["text"] for t in detail["transcript"] if t["turn"] == "answer"]
    for crit in detail["scores"]:
        for ev in crit["evidence"]:
            if ev["verified"]:
                assert ev["quote"].lower().replace(" ", "") in answers[ev["answer_index"]].lower().replace(" ", "")
    crit = detail["scores"][1]["criterion"]
    assert client.post(f"/talent/interviews/{iv.id}/override", json={"criterion": crit, "score": 5, "reason": ""},
                       headers=h).status_code == 400
    r = client.post(f"/talent/interviews/{iv.id}/override",
                    json={"criterion": crit, "score": 5, "reason": "Answer 2 shows production ownership"}, headers=h)
    assert r.status_code == 200
    log = db.query(AuditLog).filter_by(action="interview.score_overridden").one()
    assert log.actor == f"user:{recruiter.id}" and log.detail["from"] == 2 and log.detail["to"] == 5
    db.expire_all()
    s = db.get(TalentInterview, iv.id).scores[1]
    assert s["score"] == 2 and s["override"]["score"] == 5 and s["override"]["by"] == recruiter.id
    assert client.post(f"/talent/interviews/{iv.id}/review", headers=h).json()["human_review_status"] == "reviewed"


def test_audio_answers_are_transcribed_and_served_only_signed(client, world, db, monkeypatch):
    fakes.install(monkeypatch, responder)
    monkeypatch.setattr(interviews, "transcribe", lambda path: "I tuned Kafka consumer groups in Python services for two years.")
    org, job, p, m, iv, token = _scheduled_interview(world, db, mode="async_voice")
    asyncio.run(interviews.prepare_questions(str(iv.id)))
    client.post(f"/talent/public/interview/{token}/start",
                json={"disclosure_acknowledged": True, "consent_to_record": True, "answer_mode": "audio"})
    webm = b"\x1aE\xdf\xa3" + b"\x00" * 200
    r = client.post(f"/talent/public/interview/{token}/answer", data={"question_id": "q1"},
                    files={"audio": ("a.webm", webm, "audio/webm")})
    assert r.status_code == 200, r.text
    bad = client.post(f"/talent/public/interview/{token}/answer", data={"question_id": r.json()["question"]["id"]},
                      files={"audio": ("a.webm", b"MZ\x90 not audio", "audio/webm")})
    assert bad.status_code == 409
    db.expire_all()
    iv_db = db.get(TalentInterview, iv.id)
    ans = [t for t in iv_db.transcript if t["turn"] == "answer"][0]
    assert ans["kind"] == "audio" and ans["transcription"] == "ok" and "Kafka consumer groups" in ans["text"]
    assert ans["audio_path"].startswith("uploads/talent_audio/") and len(iv_db.recording_paths) == 1
    admin = world.user(org)
    detail = client.get(f"/talent/interviews/{iv.id}", headers=world.headers(admin)).json()
    url = [t for t in detail["transcript"] if t["turn"] == "answer"][0]["audio_url"]
    assert client.get(url).status_code == 200
    assert client.get(url.split("?")[0]).status_code == 403  # unsigned: refused


def test_time_limit_finishes_the_interview(world, db, monkeypatch):
    fakes.install(monkeypatch, responder)
    org, job, p, m, iv, token = _scheduled_interview(world, db)
    asyncio.run(interviews.prepare_questions(str(iv.id)))
    db.expire_all()
    iv = db.get(TalentInterview, iv.id)
    interviews.start(db, iv, disclosure_acknowledged=True, consent_to_record=True, answer_mode="text")
    assert interviews.finish_if_overdue(db, str(iv.id)) is False
    iv.started_at = datetime.utcnow() - timedelta(minutes=40)
    db.commit()
    with pytest.raises(interviews.InterviewError):
        asyncio.run(interviews.answer(str(iv.id), "q1", "A late but detailed answer about Kafka in production."))
    db.expire_all()
    assert db.get(TalentInterview, iv.id).status == "completed"
    assert db.query(AuditLog).filter_by(action="interview.completed").one().detail["reason"] == "time_limit"


def test_no_rejection_without_a_recruiter(client, world, db, monkeypatch, outbox):
    fakes.install(monkeypatch, responder)
    org, job, p, m, iv, token = _scheduled_interview(world, db)
    m = db.get(JobMatch, m.id)
    with pytest.raises(approvals.NotApproved):
        decisions.send_rejection(db, m)
    # Nothing the agent does — not a terrible score — sends one.
    iv = db.get(TalentInterview, iv.id)
    iv.status, iv.transcript = "completed", [{"turn": "question", "question_id": "q1", "text": "Q"},
                                             {"turn": "answer", "question_id": "q1", "text": "no idea"}]
    db.commit()
    asyncio.run(scoring.score_interview(str(iv.id)))
    assert not any("Update on the" in o["subject"] for o in outbox)
    assert db.get(JobMatch, m.id).status != "closed"

    recruiter = world.user(org, UserRole.recruiter)
    r = client.post(f"/talent/matches/{m.id}/reject", json={"note": "Needs more Kafka depth", "notify": True},
                    headers=world.headers(recruiter))
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert outbox[-1]["subject"] == f"Update on the {job.title} role"
    db.expire_all()
    approval = db.query(approvals.TalentApproval).filter_by(match_id=m.id, kind="reject_message").one()
    assert approval.decided_by == recruiter.id and approval.status == "approved"
    sent_log = db.query(AuditLog).filter_by(action="match.rejection_sent").one()
    assert sent_log.actor == f"user:{recruiter.id}"


def test_only_decisions_can_send_a_rejection():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    users = []
    for path in list(root.rglob("*.py")):
        rel = path.relative_to(root.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "not_moving_forward":
                users.append(rel)
            if isinstance(node, ast.Attribute) and node.attr == "send_rejection":
                users.append(rel + ":call")
    assert set(users) <= {"app/talent/decisions.py", "app/talent/templates.py", "app/talent/outbox.py",
                          "app/talent/decisions.py:call"}, users


def test_submit_is_a_recorded_human_decision(client, world, db, outbox):
    org, job, p, m, iv, token = _scheduled_interview(world, db, start_now=False)
    sent_before = len(outbox)
    recruiter = world.user(org, UserRole.recruiter)
    r = client.post(f"/talent/matches/{m.id}/submit", json={"note": "Strong"}, headers=world.headers(recruiter))
    assert r.status_code == 200 and r.json()["status"] == "submitted" and r.json()["packet"]["job"] == job.title
    assert len(outbox) == sent_before  # nothing is sent to anyone automatically
    approval = db.query(approvals.TalentApproval).filter_by(match_id=m.id, kind="submit").one()
    assert approval.decided_by == recruiter.id
    assert db.query(AuditLog).filter_by(action="match.submitted_to_client").one().actor == f"user:{recruiter.id}"


def test_shortlist_is_a_sort_order_only(world, db):
    org = world.org()
    job = world.job(org)
    rows = []
    for i, (ai, overall, flags) in enumerate([(70, 4.5, 0), (95, 2.0, 0), (80, 4.5, 3)]):
        p = world.profile(org, email=f"s{i}@x.com")
        m = JobMatch(job_id=job.id, profile_id=p.id, organization_id=org.id, ai_score=ai, status="interviewed",
                     risk_flags=[{"flag": "x", "evidence": ""}] * flags)
        db.add(m)
        db.flush()
        db.add(TalentInterview(match_id=m.id, mode="async_text", status="scored", overall_score=overall))
        rows.append(m)
    db.commit()
    order = scoring.shortlist(db, job.id)
    # 80.5 (strong interview) > 75.5 (strong interview, three risk flags) > 53 (best CV match, weak interview)
    assert [r["match_id"] for r in order] == [str(rows[0].id), str(rows[2].id), str(rows[1].id)]
    assert [r["composite"] for r in order] == [80.5, 75.5, 53.0]
    assert {m.status for m in db.query(JobMatch)} == {"interviewed"}


def test_erasing_a_candidate_removes_recordings_and_transcripts(client, world, db, monkeypatch):
    from app.media import UPLOAD_ROOT
    from app.talent import retention

    fakes.install(monkeypatch, responder)
    monkeypatch.setattr(interviews, "transcribe", lambda path: "spoken answer")
    org, job, p, m, iv, token = _scheduled_interview(world, db, mode="async_voice")
    asyncio.run(interviews.prepare_questions(str(iv.id)))
    client.post(f"/talent/public/interview/{token}/start",
                json={"disclosure_acknowledged": True, "consent_to_record": True, "answer_mode": "audio"})
    client.post(f"/talent/public/interview/{token}/answer", data={"question_id": "q1"},
                files={"audio": ("a.webm", b"\x1aE\xdf\xa3" + b"\x00" * 50, "audio/webm")})
    iv_id, match_id = iv.id, m.id
    db.expire_all()
    path = db.get(TalentInterview, iv_id).recording_paths[0]
    full = os.path.join(UPLOAD_ROOT, *path.split("/")[1:])
    assert os.path.exists(full)
    profile_id = p.id
    result = retention.erase_profile(db, profile_id, actor="user:1", reason="candidate_request")
    assert result["files_removed"] >= 1
    assert not os.path.exists(full)
    db.expunge_all()
    assert db.query(TalentInterview).filter_by(id=iv_id).first() is None
    assert db.query(JobMatch).filter_by(id=match_id).first() is None
    erased = db.query(AuditLog).filter_by(action="profile.erased").one()
    assert erased.profile_id == profile_id
