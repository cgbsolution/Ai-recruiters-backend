"""Nira's feedback on an answer is kept apart from the next question.

Each AI turn used to be one string ("Excellent — strong audit experience. Now,
how do you define delivery excellence?"), so the candidate heard feedback and
question as one run-on line and the report's question cards showed the
feedback mixed into the question. The model now returns `feedback` and
`message` separately; `InterviewMessage.feedback` stores the reaction and
`content` holds the question alone. The splitter covers turns that still
arrive combined, plus interviews saved before the change.
"""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.candidate import Candidate
from app.models.interview import InterviewMessage, InterviewSession, InterviewToken, SessionStatus
from app.models.job import Job
from app.services import avatar_interview_service as svc
from app.services.interview_text import question_text, split_feedback_and_question


# ── Splitter ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "combined, feedback, question",
    [
        # The three turns from the reported interview.
        (
            "Excellent — that's strong foundational experience with standards and audits. "
            "Now, how do you define and measure delivery excellence within a Digital or Technology practice?",
            "Excellent — that's strong foundational experience with standards and audits.",
            "How do you define and measure delivery excellence within a Digital or Technology practice?",
        ),
        (
            "Perfect, thanks for walking us through that journey. You've got solid experience in ITIL "
            "and service management. Now, have you implemented CMMI or ISO standards in an organization?",
            "Perfect, thanks for walking us through that journey. You've got solid experience in ITIL "
            "and service management.",
            "Have you implemented CMMI or ISO standards in an organization?",
        ),
        (
            "Great, thanks for sharing that! So let me start with our first question: Please walk us "
            "through your background.",
            "Great, thanks for sharing that!",
            "Please walk us through your background.",
        ),
        # The last-question heads-up stays with the spoken feedback.
        (
            "Great answer. Alright, this is your last question. How would you scale the practice?",
            "Great answer. Alright, this is your last question.",
            "How would you scale the practice?",
        ),
        # Framing context without a transition stays with the question.
        (
            "Good point. You mentioned working at KPMG. How did you handle audits there?",
            "Good point.",
            "You mentioned working at KPMG. How did you handle audits there?",
        ),
        # Imperative questions carry no "?".
        (
            "Interesting perspective on SLAs. Tell me about a time you missed one.",
            "Interesting perspective on SLAs.",
            "Tell me about a time you missed one.",
        ),
        (
            "Thank you for sharing that. Moving on, could you describe a challenging project?",
            "Thank you for sharing that.",
            "Could you describe a challenging project?",
        ),
        # Other openers the model commonly uses.
        (
            "What a great example of stakeholder management. How did you measure the outcome?",
            "What a great example of stakeholder management.",
            "How did you measure the outcome?",
        ),
        (
            "You've clearly got strong ITIL experience. Now, how do you handle escalations?",
            "You've clearly got strong ITIL experience.",
            "How do you handle escalations?",
        ),
        (
            "That makes sense. Which ISO standard did you implement?",
            "That makes sense.",
            "Which ISO standard did you implement?",
        ),
        # Acknowledgment glued onto the question inside ONE sentence.
        (
            "Thanks for sharing that — how do you measure delivery excellence?",
            "Thanks for sharing that.",
            "How do you measure delivery excellence?",
        ),
        (
            "Great, Raja, tell me about your biggest audit finding.",
            "Great, Raja.",
            "Tell me about your biggest audit finding.",
        ),
        (
            "Perfect. Got it — so what KPIs do you track?",
            "Perfect. Got it.",
            "What KPIs do you track?",
        ),
        # Scene-setting lead-ins never reach the question card; whole-sentence
        # ones are still spoken with the feedback.
        (
            "Great, thanks for sharing that! Let's dive into a technical scenario. "
            "Here's your first question: Explain controlled vs uncontrolled components.",
            "Great, thanks for sharing that! Let's dive into a technical scenario.",
            "Explain controlled vs uncontrolled components.",
        ),
        (
            "Let's shift gears. You're working on a React dashboard. How do you speed it up?",
            "Let's shift gears.",
            "You're working on a React dashboard. How do you speed it up?",
        ),
        (
            "I see you're skipping questions. Let me ask a more direct one. "
            "You mentioned React — can you tell me about a project?",
            "I see you're skipping questions. Let me ask a more direct one.",
            "You mentioned React — can you tell me about a project?",
        ),
    ],
)
def test_splits_feedback_off_combined_turn(combined, feedback, question):
    assert split_feedback_and_question(combined) == (feedback, question)


@pytest.mark.parametrize(
    "text",
    [
        "How do you define delivery excellence?",
        # Praise-sounding opener that is really part of the question.
        "Great leaders inspire others. How do you inspire your team?",
        # Closing and coding hand-off lines are statements, not questions.
        "Thank you for your time, Raja. That concludes our interview. We'll be in touch soon.",
        "Great, here is your next coding challenge. The new problem has loaded in the editor.",
        "I didn't quite catch that. Could you elaborate a bit more on your answer?",
        "Hi Raja! I'm Nira, your AI interviewer. Could you take a moment to introduce yourself?",
        "Good, bad, or ugly — which word describes your last audit?",
        "Great, here is your next coding challenge.",
    ],
)
def test_leaves_non_feedback_text_whole(text):
    assert split_feedback_and_question(text) == ("", text)


def test_question_text_trusts_stored_feedback():
    # Split at write time — content is the question verbatim, never re-split.
    msg = SimpleNamespace(content="Great leaders inspire others. How do you?", feedback="Thanks.")
    assert question_text(msg) == "Great leaders inspire others. How do you?"
    legacy = SimpleNamespace(content="Great, thanks. Now, how do you measure SLAs?", feedback=None)
    assert question_text(legacy) == "How do you measure SLAs?"


# ── Live turn ─────────────────────────────────────────────────────────────────

def _session(db, current_question_index):
    stamp = datetime.utcnow().timestamp()
    job = Job(
        title="Practice Manager",
        department="Delivery",
        location="Bangalore",
        job_type="Full-time",
        experience_level="Senior",
        emp_type="Full-time",
    )
    db.add(job)
    db.flush()
    token = InterviewToken(job_id=job.id, token=f"fb-tok-{stamp}")
    db.add(token)
    db.flush()
    cand = Candidate(name="Raja", email=f"fb{stamp}@example.com", interview_status="Scheduled")
    db.add(cand)
    db.flush()
    session = InterviewSession(
        token_id=token.id,
        candidate_id=cand.id,
        job_id=job.id,
        status=SessionStatus.in_progress,
        started_at=datetime.utcnow(),
        current_question_index=current_question_index,
    )
    db.add(session)
    db.flush()
    db.add(InterviewMessage(
        session_id=session.id, role="ai",
        content="Have you implemented CMMI or ISO standards?",
        feedback="Thanks for walking us through your journey.",
        question_index=current_question_index,
    ))
    db.commit()
    return session


def _fake_llm(monkeypatch, reply: dict, seen: list):
    async def fake(system, messages, **kwargs):
        seen.append(messages)
        return SimpleNamespace(text=json.dumps(reply))

    monkeypatch.setattr(svc, "_ask_with_system_usage", fake)
    monkeypatch.setattr(svc, "log_usage", lambda *a, **k: None)


def _last_ai(db, session):
    return (
        db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == session.id, InterviewMessage.role == "ai")
        .order_by(InterviewMessage.id.desc())
        .first()
    )


async def test_turn_returns_and_stores_feedback_apart(db, monkeypatch):
    session = _session(db, current_question_index=2)
    seen: list = []
    _fake_llm(monkeypatch, {
        "feedback": "Excellent — that's strong experience with audits.",
        "message": "How do you define delivery excellence?",
        "is_complete": False,
        "question_index": 3,
    }, seen)

    result = await svc.conduct_turn(session, "Yes, I was part of multiple CMMI audits.", db)

    assert result["feedback"] == "Excellent — that's strong experience with audits."
    assert result["message"] == "How do you define delivery excellence?"
    row = _last_ai(db, session)
    assert row.content == "How do you define delivery excellence?"
    assert row.feedback == "Excellent — that's strong experience with audits."
    # The model still sees everything Nira said earlier, feedback included.
    history = seen[0]
    assert history[0] == {
        "role": "assistant",
        "content": "Thanks for walking us through your journey. Have you implemented CMMI or ISO standards?",
    }


async def test_turn_splits_a_combined_message_the_model_returned(db, monkeypatch):
    session = _session(db, current_question_index=2)
    _fake_llm(monkeypatch, {
        "message": "Excellent — strong audit experience. Now, how do you define delivery excellence?",
        "is_complete": False,
        "question_index": 3,
    }, [])

    result = await svc.conduct_turn(session, "Yes, I was part of multiple CMMI audits.", db)

    assert result["feedback"] == "Excellent — strong audit experience."
    assert result["message"] == "How do you define delivery excellence?"
    assert _last_ai(db, session).content == "How do you define delivery excellence?"


@pytest.mark.parametrize(
    "reply",
    [
        # Question put in feedback, message left empty.
        {"feedback": "Excellent — strong audit experience. Now, how do you define delivery excellence?",
         "message": ""},
        # Question put in feedback, message holds only a filler line.
        {"feedback": "Excellent — strong audit experience. How do you define delivery excellence?",
         "message": "Take your time."},
    ],
)
async def test_question_never_left_in_feedback(db, monkeypatch, reply):
    # The report reads the question from `content` only — a question the model
    # misplaced into `feedback` must still end up there.
    session = _session(db, current_question_index=2)
    _fake_llm(monkeypatch, {**reply, "is_complete": False, "question_index": 3}, [])

    result = await svc.conduct_turn(session, "Yes, I was part of multiple CMMI audits.", db)

    row = _last_ai(db, session)
    assert "define delivery excellence" in row.content
    assert "define delivery excellence" not in (row.feedback or "")
    assert result["message"] == row.content
    assert row.content != "Thank you. Let's continue."


async def test_closing_turn_is_spoken_as_one_line(db, monkeypatch):
    session = _session(db, current_question_index=7)
    _fake_llm(monkeypatch, {
        "feedback": "Great answer.",
        "message": "Thank you for your time, Raja. That concludes our interview.",
        "is_complete": True,
        "question_index": 8,
    }, [])

    result = await svc.conduct_turn(session, "I would scale it with playbooks.", db)

    assert result["is_complete"] is True
    assert result["feedback"] is None
    assert result["message"] == "Great answer. Thank you for your time, Raja. That concludes our interview."
