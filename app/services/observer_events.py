"""SQLAlchemy event listeners that fan interview activity out to the
observer broker.

Imported once at startup. Every new `InterviewMessage` and every status
change on `InterviewSession` is pushed into the broker, where any SSE
observer subscribed to that session_id receives it in real time.

Listeners are sync and non-blocking (broker uses put_nowait). They will
NEVER raise into the interview flow — wrapped defensively.
"""

from __future__ import annotations

import logging

from sqlalchemy import event

from app.models.interview import (
    CodingSubmission,
    InterviewMessage,
    InterviewSession,
    Violation,
)
from app.services.observer_broker import broker

logger = logging.getLogger(__name__)


@event.listens_for(InterviewMessage, "after_insert")
def _on_message_insert(mapper, connection, target: InterviewMessage):
    try:
        broker.publish_event(target.session_id, {
            "type": "message",
            "message": {
                "id": target.id,
                "role": target.role,
                "content": target.content,
                "question_index": target.question_index,
                "created_at": target.created_at.isoformat() if target.created_at else None,
            },
        })
    except Exception as exc:
        logger.debug("observer message event failed: %s", exc)


@event.listens_for(InterviewSession, "after_update")
def _on_session_update(mapper, connection, target: InterviewSession):
    try:
        status_value = target.status.value if hasattr(target.status, "value") else str(target.status)
        broker.publish_event(target.id, {
            "type": "state",
            "state": {
                "session_id": target.id,
                "status": status_value,
                "current_question_index": target.current_question_index or 0,
                "total_questions": target.total_questions or 0,
                "violation_count": target.violation_count or 0,
                "started_at": target.started_at.isoformat() if target.started_at else None,
                "completed_at": target.completed_at.isoformat() if target.completed_at else None,
            },
        })
    except Exception as exc:
        logger.debug("observer state event failed: %s", exc)


@event.listens_for(Violation, "after_insert")
def _on_violation_insert(mapper, connection, target: Violation):
    try:
        broker.publish_event(target.session_id, {
            "type": "violation",
            "violation": {
                "violation_type": target.violation_type.value if hasattr(target.violation_type, "value") else str(target.violation_type),
                "details": target.details,
                "occurred_at": target.occurred_at.isoformat() if target.occurred_at else None,
            },
        })
    except Exception as exc:
        logger.debug("observer violation event failed: %s", exc)


@event.listens_for(CodingSubmission, "after_insert")
def _on_coding_submission_insert(mapper, connection, target: CodingSubmission):
    try:
        broker.publish_event(target.session_id, {
            "type": "coding_submission",
            "coding": {
                "question_index": target.question_index,
                "title": target.title,
                "description": target.description,
                "difficulty": target.difficulty,
                "language": target.language,
                "code": target.code,
                "test_cases_total": target.test_cases_total,
                "test_cases_passed": target.test_cases_passed,
                "submitted_at": target.submitted_at.isoformat() if target.submitted_at else None,
            },
        })
    except Exception as exc:
        logger.debug("observer coding event failed: %s", exc)
