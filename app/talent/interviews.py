"""
Loop D — the asynchronous interview (async_text / async_voice).

prepare_questions()  8-12 questions built from the job's must-haves, drawing
                     on the Question Bank (the org's questions and the job's own),
                     via Claude when available — otherwise from the bank and
                     must-have templates. Prepared when the interview is booked.
start()              requires the AI disclosure to be acknowledged and explicit
                     consent to record. Nothing is stored before that.
answer()             one answer (typed, or recorded audio transcribed with
                     Deepgram). A vague answer gets at most ONE follow-up.
finish()             on the last answer, on request, or when the time limit
                     passes (app.agent.interview_conduct) → scoring.

The transcript is a list of turns:
  {"turn": "question"|"answer", "question_id", "text", "kind": "text"|"audio",
   "audio_path", "transcription": "ok"|"failed"|None, "at"}
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.media import UPLOAD_ROOT
from app.models.job import Job, JobQuestion
from app.models.question import Question
from app.models.talent import JobMatch, TalentInterview
from app.talent import claude, llm
from app.talent.audit import audit
from app.talent.config import talent_config
from app.talent.matching import requirements
from app.talent.prompts import FOLLOWUP_SCHEMA, FOLLOWUP_SYSTEM, FOLLOWUP_VERSION, QUESTIONS_SCHEMA, QUESTIONS_SYSTEM, \
    QUESTIONS_VERSION
from app.talent.settings import get_job_settings

logger = logging.getLogger("app.talent.interviews")

AUDIO_DIR = "talent_audio"
TIME_GRACE = timedelta(minutes=3)
AUDIO_TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
               "audio/wav": ".wav", "audio/x-wav": ".wav", "video/webm": ".webm"}
_MIN_ANSWER_CHARS = 2


class InterviewError(Exception):
    pass


# ── Questions ───────────────────────────────────────────────────────────────

def _bank(db: Session, job: Job, must_haves: List[str], limit: int = 30) -> List[dict]:
    words = {w for m in must_haves for w in re.findall(r"[a-z][a-z0-9+#.]{2,}", m.lower())} - {
        "experience", "years", "hands", "with", "least", "relevant", "the", "and"}
    rows = [{"id": f"job-{q.id}", "text": q.text, "category": q.category}
            for q in db.query(JobQuestion).filter(JobQuestion.job_id == job.id).all()
            if (q.category or "").lower() != "coding"]
    bank = db.query(Question).filter(Question.organization_id == job.organization_id).limit(2000).all()
    scored = []
    for q in bank:
        text = f"{q.text} {q.category}".lower()
        hits = sum(1 for w in words if w in text)
        if hits:
            scored.append((hits, {"id": f"bank-{q.id}", "text": q.text, "category": q.category}))
    scored.sort(key=lambda t: t[0], reverse=True)
    return (rows + [q for _, q in scored])[:limit]


def _fallback_questions(must_haves: List[str], bank: List[dict], count: int) -> List[dict]:
    out = [{"text": q["text"], "must_have": "", "source": q["id"]} for q in bank[: count // 2]]
    for m in must_haves:
        if len(out) >= count:
            break
        out.append({"text": f"Tell us about a specific piece of work that shows: {m}. What did you do yourself, "
                            "and what was the result?", "must_have": m, "source": "template"})
    generic = [
        "Describe the most complex problem you solved in your current or last role, step by step.",
        "Tell us about a time a project went wrong. What did you change afterwards?",
        "How do you check that your work is correct before others rely on it?",
        "What would you want to learn or improve in your first six months in this role?",
    ]
    for g in generic:
        if len(out) >= count:
            break
        out.append({"text": g, "must_have": "", "source": "template"})
    return out[:count]


async def prepare_questions(interview_id: str) -> dict:
    from app.agent.common import run_db

    def _context(db: Session):
        iv = db.get(TalentInterview, uuid.UUID(interview_id))
        if iv is None or iv.questions:
            return None
        match = db.get(JobMatch, iv.match_id)
        job = db.get(Job, match.job_id)
        js = get_job_settings(db, job.id, job.organization_id)
        req = requirements(db, job, js)
        return {"must_haves": req.must_haves, "bank": _bank(db, job, req.must_haves), "count": js.question_count,
                "title": job.title, "organization_id": job.organization_id, "job_id": job.id}

    ctx = await run_db(_context)
    if ctx is None:
        return {"status": "ready"}
    questions, model, version = None, None, None
    if llm.available():
        user = claude.joined([
            claude.untrusted("untrusted_job", {"title": ctx["title"], "must_haves": ctx["must_haves"]}),
            claude.untrusted("untrusted_question_bank", ctx["bank"]),
            f"Write exactly {ctx['count']} questions.",
        ])
        try:
            result = await llm.structured(
                feature="talent_interview_questions", system=QUESTIONS_SYSTEM, user=user, schema=QUESTIONS_SCHEMA,
                model=talent_config.TALENT_INTERVIEW_MODEL, effort=talent_config.TALENT_INTERVIEW_EFFORT,
                prompt_version=QUESTIONS_VERSION, max_tokens=4000, organization_id=ctx["organization_id"],
                job_id=ctx["job_id"],
            )
            if result.data and not result.refused:
                questions = [q for q in result.data.get("questions", []) if q.get("text", "").strip()][: ctx["count"]]
                model, version = result.model, result.prompt_version
        except Exception as exc:  # noqa: BLE001 — the interview still runs on the fallback set
            logger.warning("Question generation failed (%s); using the bank and templates", type(exc).__name__)
    if not questions or len(questions) < 8:
        questions = _fallback_questions(ctx["must_haves"], ctx["bank"], max(8, ctx["count"]))
        model, version = None, "fallback"

    def _store(db: Session):
        iv = db.get(TalentInterview, uuid.UUID(interview_id))
        if iv is None or iv.questions:
            return
        iv.questions = [{"id": f"q{i + 1}", "text": q["text"].strip()[:1000], "must_have": q.get("must_have") or "",
                         "source": q.get("source") or "generated"} for i, q in enumerate(questions)]
        audit(db, actor="agent", action="interview.questions_prepared", subject_type="interview", subject_id=iv.id,
              detail={"count": len(iv.questions), "model": model, "prompt_version": version,
                      "from_bank": sum(1 for q in iv.questions if q["source"].startswith(("bank-", "job-")))})
        db.commit()

    await run_db(_store)
    return {"status": "prepared", "count": len(questions)}


# ── Running the interview ───────────────────────────────────────────────────

def _deadline(db: Session, iv: TalentInterview) -> Optional[datetime]:
    if not iv.started_at:
        return None
    match = db.get(JobMatch, iv.match_id)
    js = get_job_settings(db, match.job_id)
    return iv.started_at + timedelta(minutes=int(js.time_limit_minutes or 30))


def _turns(iv: TalentInterview) -> list:
    return list(iv.transcript or [])


def current_question(iv: TalentInterview) -> Optional[dict]:
    """The next question to answer: the last asked one without an answer."""
    turns = _turns(iv)
    if turns and turns[-1]["turn"] == "question":
        return {"id": turns[-1]["question_id"], "text": turns[-1]["text"],
                "follow_up": turns[-1]["question_id"].endswith("-f")}
    return None


def _ask(iv: TalentInterview, qid: str, text: str) -> None:
    turns = _turns(iv)
    turns.append({"turn": "question", "question_id": qid, "text": text, "at": datetime.utcnow().isoformat()})
    iv.transcript = turns
    flag_modified(iv, "transcript")


def _next_main(iv: TalentInterview) -> Optional[dict]:
    asked = {t["question_id"] for t in _turns(iv) if t["turn"] == "question"}
    return next((q for q in iv.questions or [] if q["id"] not in asked), None)


def progress(db: Session, iv: TalentInterview, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    deadline = _deadline(db, iv)
    answered = len({t["question_id"] for t in _turns(iv) if t["turn"] == "answer" and not t["question_id"].endswith("-f")})
    return {"status": iv.status, "question": current_question(iv), "answered": answered,
            "total": len(iv.questions or []), "deadline": deadline.isoformat() if deadline else None,
            "seconds_left": max(0, int((deadline - now).total_seconds())) if deadline else None}


def start(db: Session, iv: TalentInterview, *, disclosure_acknowledged: bool, consent_to_record: bool,
          answer_mode: str, now: Optional[datetime] = None) -> dict:
    from app.talent import scheduling

    now = now or datetime.utcnow()
    if iv.status == "in_progress":
        return progress(db, iv, now)
    if iv.status != "scheduled":
        raise InterviewError("This interview can't be started now.")
    opens, closes = scheduling.join_window(iv)
    if opens is None or not (opens <= now <= closes):
        raise InterviewError("This interview isn't open right now.")
    if not disclosure_acknowledged or not consent_to_record:
        raise InterviewError("Please confirm you understand this is an AI interview and agree to it being recorded.")
    if answer_mode not in ("text", "audio") or (answer_mode == "audio" and iv.mode != "async_voice"):
        answer_mode = "text"
    if not iv.questions:
        raise InterviewError("The questions are still being prepared. Please try again in a minute.")
    match = db.get(JobMatch, iv.match_id)
    iv.ai_disclosed_at = iv.ai_disclosed_at or now
    iv.consent_recorded_at = now
    iv.started_at, iv.status = now, "in_progress"
    first = iv.questions[0]
    _ask(iv, first["id"], first["text"])
    audit(db, actor="candidate", action="interview.consent_recorded", subject_type="interview", subject_id=iv.id,
          organization_id=match.organization_id, profile_id=match.profile_id,
          detail={"ai_disclosure_acknowledged": True, "recording_consent": True, "answer_mode": answer_mode})
    db.commit()
    from app.agent.client import emit

    emit("talent/interview.started", {"interview_id": str(iv.id)})
    return progress(db, iv, now)


def _audio_ext(upload) -> Optional[str]:
    head = upload.file.read(16)
    upload.file.seek(0)
    if head.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return ".wav"
    if head[4:8] == b"ftyp":
        return ".m4a"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    return None


def store_audio(iv: TalentInterview, upload) -> str:
    max_bytes = int(talent_config.TALENT_AUDIO_MAX_MB) * 1024 * 1024
    if upload.size is not None and upload.size > max_bytes:
        raise InterviewError(f"Recordings can be at most {talent_config.TALENT_AUDIO_MAX_MB} MB.")
    ext = _audio_ext(upload)
    if ext is None:
        raise InterviewError("That doesn't look like an audio recording.")
    folder = os.path.join(UPLOAD_ROOT, AUDIO_DIR)
    os.makedirs(folder, exist_ok=True)
    n = len(iv.recording_paths or []) + 1
    name = f"{iv.id.hex}-{n}{ext}"
    full = os.path.join(folder, name)
    written = 0
    with open(full, "xb") as out:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                out.close()
                os.remove(full)
                raise InterviewError(f"Recordings can be at most {talent_config.TALENT_AUDIO_MAX_MB} MB.")
            out.write(chunk)
    rel = f"uploads/{AUDIO_DIR}/{name}"
    iv.recording_paths = list(iv.recording_paths or []) + [rel]
    flag_modified(iv, "recording_paths")
    return rel


def transcribe(path: str) -> Optional[str]:
    """Deepgram pre-recorded transcription. None when unavailable."""
    if not settings.DEEPGRAM_API_KEY:
        return None
    from app.media import resolve_upload_file

    full = resolve_upload_file(path, [AUDIO_DIR])
    if not full:
        return None
    import httpx

    ext = os.path.splitext(full)[1].lower()
    content_type = {".webm": "audio/webm", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".mp3": "audio/mpeg",
                    ".wav": "audio/wav"}.get(ext, "application/octet-stream")
    try:
        with open(full, "rb") as fh:
            resp = httpx.post("https://api.deepgram.com/v1/listen", params={"model": "nova-3", "smart_format": "true"},
                              headers={"Authorization": f"Token {settings.DEEPGRAM_API_KEY}", "Content-Type": content_type},
                              content=fh.read(), timeout=120)
        resp.raise_for_status()
        alts = resp.json()["results"]["channels"][0]["alternatives"]
        return (alts[0].get("transcript") or "").strip() if alts else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Transcription failed (%s)", type(exc).__name__)
        return None


async def _follow_up(question: str, answer: str, organization_id: Optional[int]) -> Optional[str]:
    if not llm.followups_enabled() or len(answer) > 4000:
        return None
    try:
        result = await asyncio.wait_for(llm.structured(
            feature="talent_interview_followup", system=FOLLOWUP_SYSTEM,
            user=claude.joined([claude.untrusted("untrusted_question", question),
                                claude.untrusted("untrusted_answer", answer)]),
            schema=FOLLOWUP_SCHEMA, model=talent_config.TALENT_INTERVIEW_MODEL, effort="low",
            prompt_version=FOLLOWUP_VERSION, max_tokens=600, organization_id=organization_id,
        ), timeout=25)
    except Exception as exc:  # noqa: BLE001 — no follow-up is better than a stalled interview
        logger.info("No follow-up (%s)", type(exc).__name__)
        return None
    if result.refused or not result.data or not result.data.get("vague"):
        return None
    text = (result.data.get("follow_up") or "").strip()
    return text[:500] or None


async def answer(interview_id: str, question_id: str, text: Optional[str], audio=None) -> dict:
    """Record one answer and move on. Runs in the API request."""
    from app.agent.common import run_db

    def _record(db: Session):
        iv = db.get(TalentInterview, uuid.UUID(interview_id))
        if iv is None or iv.status != "in_progress":
            raise InterviewError("This interview is not in progress.")
        deadline = _deadline(db, iv)
        if deadline and datetime.utcnow() > deadline + TIME_GRACE:
            finish(db, iv, reason="time_limit")
            raise InterviewError("The time for this interview is up — your answers so far have been submitted.")
        current = current_question(iv)
        if current is None or current["id"] != question_id:
            raise InterviewError("That question was already answered.")
        audio_path = store_audio(iv, audio) if audio is not None else None
        db.commit()
        match = db.get(JobMatch, iv.match_id)
        return {"question": current, "audio_path": audio_path, "organization_id": match.organization_id}

    rec = await run_db(_record)
    spoken = None
    status = None
    if rec["audio_path"]:
        spoken = await asyncio.to_thread(transcribe, rec["audio_path"])
        status = "ok" if spoken is not None else "failed"
    body = " ".join(filter(None, [(text or "").strip(), (spoken or "").strip()])).strip()
    if not rec["audio_path"] and len(body) < _MIN_ANSWER_CHARS:
        raise InterviewError("Please write an answer (or say you'd like to skip this question).")
    follow = None
    if not rec["question"]["follow_up"] and body:
        follow = await _follow_up(rec["question"]["text"], body, rec["organization_id"])

    def _save(db: Session):
        iv = db.get(TalentInterview, uuid.UUID(interview_id))
        if current_question(iv) is None or current_question(iv)["id"] != question_id:
            return progress(db, iv)
        turns = _turns(iv)
        turns.append({"turn": "answer", "question_id": question_id, "text": body[:20000],
                      "kind": "audio" if rec["audio_path"] else "text", "audio_path": rec["audio_path"],
                      "transcription": status, "at": datetime.utcnow().isoformat()})
        iv.transcript = turns
        flag_modified(iv, "transcript")
        if follow:
            _ask(iv, f"{question_id}-f", follow)
        else:
            nxt = _next_main(iv)
            if nxt is None:
                finish(db, iv, reason="all_answered")
                return progress(db, iv)
            _ask(iv, nxt["id"], nxt["text"])
        db.commit()
        return progress(db, iv)

    return await run_db(_save)


def finish(db: Session, iv: TalentInterview, reason: str) -> bool:
    if iv.status != "in_progress":
        return False
    match = db.get(JobMatch, iv.match_id)
    iv.status, iv.completed_at = "completed", datetime.utcnow()
    match.status = "interviewed"
    audit(db, actor="candidate" if reason in ("all_answered", "candidate_finished") else "agent",
          action="interview.completed", subject_type="interview", subject_id=iv.id,
          organization_id=match.organization_id, profile_id=match.profile_id,
          detail={"reason": reason, "answers": sum(1 for t in _turns(iv) if t["turn"] == "answer"),
                  "recordings": len(iv.recording_paths or [])})
    db.commit()
    from app.agent.client import emit

    emit("talent/interview.completed", {"interview_id": str(iv.id)})
    return True


def finish_if_overdue(db: Session, interview_id: str) -> bool:
    iv = db.get(TalentInterview, uuid.UUID(interview_id))
    if iv is None or iv.status != "in_progress":
        return False
    deadline = _deadline(db, iv)
    if deadline and datetime.utcnow() >= deadline + TIME_GRACE:
        return finish(db, iv, reason="time_limit")
    return False


def deadline_of(db: Session, interview_id: str) -> Optional[str]:
    iv = db.get(TalentInterview, uuid.UUID(interview_id))
    d = _deadline(db, iv) if iv else None
    return (d + TIME_GRACE).isoformat() if d else None
