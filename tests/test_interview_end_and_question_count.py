"""The interview ends only when it should.

1. "End the interview" counts only when the candidate's whole message is the
   command. It used to be a substring match anywhere in the answer, with
   "end up" in the list — Saloni Jalan's Q1 answer ("...we end up with an
   EBITDA...") closed a 10-question "Only these questions" interview after one
   question, marked "Ended by Candidate".
2. Even a real command doesn't end the interview: Nira asks for confirmation,
   a clear "yes" ends it, anything else re-asks the current question.
3. The server's completion guards use the same verbal count as the prompt.
   "Only these questions" asks exactly len(predefined), which can differ from
   the job's max_questions setting.
"""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.candidate import Candidate
from app.models.interview import InterviewMessage, InterviewSession, InterviewToken, SessionStatus
from app.models.job import InterviewSettings, Job, JobQuestion, MixOfQuestions
from app.services import avatar_interview_service as svc

SALONI_Q1_ANSWER = (
    "Well, quality of earnings, is an important part of both buy side and sell side "
    "transaction deals. So quality of earnings tells us the true operational earnings. "
    "So we try to reduce that or to include that so that, you know, we end up with an "
    "EBITDA that is in operations of the company. And the motive in quality of earnings "
    "is to come to an operation EBITDA that we know That is sustainable in the future."
)
CONFIRM_PROMPT = svc.END_CONFIRM_PROMPTS["en"]


# ── What counts as an end command ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        SALONI_Q1_ANSWER,
        "We always end up reconciling the trial balance first.",
        "I'd end the interview with the client by summarising next steps.",
        # The command at the end of an answer is still part of the answer.
        "Net working capital is current assets minus current liabilities. End the interview.",
        "I usually prepare for the weekend call.",
        "Yes",
        "",
        None,
    ],
)
def test_answers_are_not_end_commands(text):
    assert svc._is_end_interview_request(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "End interview",
        "Please end the interview.",
        "Can you please close this interview now?",
        "Okay, I need to leave.",
        "I want to end",
        "end this call!",
        "Stop.",
    ],
)
def test_whole_message_commands_are_end_requests(text):
    assert svc._is_end_interview_request(text) is True


@pytest.mark.parametrize(
    "text, is_yes",
    [
        ("yes", True),
        ("Yes please.", True),
        ("Okay, yes", True),
        ("Haan ji", True),
        ("हाँ", True),
        ("End the interview", True),
        ("no", False),
        ("No, I want to continue the interview.", False),
        ("Yes, so quality of earnings is about EBITDA adjustments", False),
        ("yes but not now", False),
        ("okay", False),
        ("", False),
    ],
)
def test_confirmation_needs_a_clear_yes(text, is_yes):
    assert svc._is_end_confirmation_yes(text) is is_yes


# ── Live turns ────────────────────────────────────────────────────────────────

def _session(db, *, predefined: int, max_questions: int, current_question_index: int):
    stamp = datetime.utcnow().timestamp()
    job = Job(
        title="Senior Associate-FDD",
        department="Finance",
        location="Mumbai",
        job_type="Full-time",
        experience_level="4-7 Years",
        emp_type="Full-time",
        mix_of_questions=MixOfQuestions.only,
    )
    db.add(job)
    db.flush()
    db.add(InterviewSettings(job_id=job.id, max_questions=max_questions))
    for i in range(predefined):
        db.add(JobQuestion(job_id=job.id, text=f"Predefined question {i + 1}?"))
    token = InterviewToken(job_id=job.id, token=f"end-tok-{stamp}")
    db.add(token)
    db.flush()
    cand = Candidate(name="Saloni", email=f"end{stamp}@example.com", interview_status="Scheduled")
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
        content=f"Predefined question {current_question_index}?",
        question_index=current_question_index,
    ))
    db.commit()
    return session


def _fake_llm(monkeypatch, reply: dict, calls: list):
    async def fake(system, messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(text=json.dumps(reply))

    monkeypatch.setattr(svc, "_ask_with_system_usage", fake)
    monkeypatch.setattr(svc, "log_usage", lambda *a, **k: None)


_NEXT_QUESTION = {
    "feedback": "Thanks.",
    "message": "Predefined question 4?",
    "is_complete": False,
    "question_index": 4,
}


async def test_answer_containing_end_up_continues_the_interview(db, monkeypatch):
    session = _session(db, predefined=10, max_questions=10, current_question_index=1)
    calls: list = []
    _fake_llm(monkeypatch, {
        "feedback": "Good overview of QoE.",
        "message": "Predefined question 2?",
        "is_complete": False,
        "question_index": 2,
    }, calls)

    result = await svc.conduct_turn(session, SALONI_Q1_ANSWER, db)

    assert result["is_complete"] is False
    assert result["message"] == "Predefined question 2?"
    assert len(calls) == 1
    db.refresh(session)
    assert session.status == SessionStatus.in_progress


async def test_end_command_asks_for_confirmation_first(db, monkeypatch):
    session = _session(db, predefined=10, max_questions=10, current_question_index=3)
    calls: list = []
    _fake_llm(monkeypatch, _NEXT_QUESTION, calls)

    result = await svc.conduct_turn(session, "Please end the interview.", db)

    assert result["is_complete"] is False
    assert result["awaiting_end_confirmation"] is True
    assert result["message"] == CONFIRM_PROMPT
    assert result["question_index"] == 3
    assert calls == []
    db.refresh(session)
    assert session.status == SessionStatus.in_progress
    assert session.current_question_index == 3
    # Neither the request nor the prompt is part of Q3's Q&A.
    unindexed = (
        db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == session.id, InterviewMessage.question_index.is_(None))
        .count()
    )
    assert unindexed == 2


async def test_yes_after_confirmation_ends_the_interview(db, monkeypatch):
    session = _session(db, predefined=10, max_questions=10, current_question_index=3)
    calls: list = []
    _fake_llm(monkeypatch, _NEXT_QUESTION, calls)

    await svc.conduct_turn(session, "End interview", db)
    result = await svc.conduct_turn(session, "Yes.", db)

    assert result["is_complete"] is True
    assert calls == []
    db.refresh(session)
    assert session.status == SessionStatus.completed
    cand = db.query(Candidate).filter(Candidate.id == session.candidate_id).one()
    assert cand.interview_status == "Ended by Candidate"


async def test_no_after_confirmation_re_asks_the_current_question(db, monkeypatch):
    session = _session(db, predefined=10, max_questions=10, current_question_index=3)
    calls: list = []
    _fake_llm(monkeypatch, _NEXT_QUESTION, calls)

    await svc.conduct_turn(session, "End interview", db)
    result = await svc.conduct_turn(session, "No, I want to continue the interview.", db)

    assert result["is_complete"] is False
    assert result["message"] == "Predefined question 3?"
    assert result["feedback"] == svc.END_DECLINED_FEEDBACK["en"]
    assert result["question_index"] == 3
    assert calls == []
    db.refresh(session)
    assert session.status == SessionStatus.in_progress
    assert session.current_question_index == 3

    # And the interview carries on normally from there.
    answer = await svc.conduct_turn(session, "My answer to the third question.", db)
    assert answer["message"] == "Predefined question 4?"
    assert len(calls) == 1


async def test_long_reply_after_confirmation_is_treated_as_the_answer(db, monkeypatch):
    session = _session(db, predefined=10, max_questions=10, current_question_index=3)
    calls: list = []
    _fake_llm(monkeypatch, _NEXT_QUESTION, calls)

    await svc.conduct_turn(session, "End interview", db)
    result = await svc.conduct_turn(session, SALONI_Q1_ANSWER, db)

    assert result["is_complete"] is False
    assert result["message"] == "Predefined question 4?"
    assert len(calls) == 1


# ── Question count in "Only these questions" ─────────────────────────────────

async def test_only_mode_ends_after_last_predefined_below_max_questions(db, monkeypatch):
    # 3 predefined, max_questions=10: Q3 is the last question. The old guards
    # compared against 10, so the model was called and, when it closed, was
    # re-prompted ("asked 3 of 10") into inventing Q4.
    session = _session(db, predefined=3, max_questions=10, current_question_index=3)
    calls: list = []
    _fake_llm(monkeypatch, _NEXT_QUESTION, calls)

    result = await svc.conduct_turn(session, "My answer to the third question.", db)

    assert result["is_complete"] is True
    assert "concludes our interview" in result["message"]
    assert calls == []  # closed by the server after the last predefined answer


async def test_only_mode_asks_predefined_beyond_max_questions(db, monkeypatch):
    # 12 predefined, max_questions=10: after Q10 the old max_q guard closed the
    # interview without calling the model, so Q11 and Q12 were never asked.
    session = _session(db, predefined=12, max_questions=10, current_question_index=10)
    calls: list = []
    _fake_llm(monkeypatch, {
        "feedback": "Thanks.",
        "message": "Predefined question 11?",
        "is_complete": False,
        "question_index": 11,
    }, calls)

    result = await svc.conduct_turn(session, "My answer to the tenth question.", db)

    assert result["is_complete"] is False
    assert result["message"] == "Predefined question 11?"
    assert len(calls) == 1
