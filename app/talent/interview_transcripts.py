"""Retrying audio answers whose transcription failed during the interview,
before the interview is scored."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.orm.attributes import flag_modified

from app.models.talent import TalentInterview
from app.talent.interviews import transcribe


async def retry_failed(interview_id: str) -> int:
    from app.agent.common import run_db

    def _pending(db):
        iv = db.get(TalentInterview, uuid.UUID(interview_id))
        if iv is None:
            return []
        return [(i, t["audio_path"]) for i, t in enumerate(iv.transcript or [])
                if t["turn"] == "answer" and t.get("audio_path") and t.get("transcription") == "failed"]

    pending = await run_db(_pending)
    fixed = {}
    for index, path in pending:
        text = await asyncio.to_thread(transcribe, path)
        if text is not None:
            fixed[index] = text

    def _save(db):
        iv = db.get(TalentInterview, uuid.UUID(interview_id))
        turns = list(iv.transcript or [])
        for index, text in fixed.items():
            turns[index] = {**turns[index], "text": " ".join(filter(None, [turns[index].get("text"), text])).strip(),
                            "transcription": "ok"}
        iv.transcript = turns
        flag_modified(iv, "transcript")
        db.commit()

    if fixed:
        await run_db(_save)
    return len(fixed)
