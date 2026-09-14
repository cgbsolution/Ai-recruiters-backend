from typing import List, Optional

from sqlalchemy import false
from sqlalchemy.orm import Session

from app.models.question import Question
from app.schemas.question import QuestionCreate, QuestionUpdate


def _scoped(db: Session, org_id: Optional[int], all_orgs: bool):
    """Questions visible to a caller. Only `all_orgs=True` (a global super admin,
    app.auth.is_global_admin) skips the tenant filter — org_id=None never means
    "every organization", it matches nothing."""
    q = db.query(Question)
    if all_orgs:
        return q
    if org_id is None:
        return q.filter(false())
    return q.filter(Question.organization_id == org_id)


def get_questions(db: Session, org_id: Optional[int] = None, *, all_orgs: bool = False) -> List[Question]:
    return _scoped(db, org_id, all_orgs).order_by(Question.created_at.desc()).all()


def get_question_by_id(db: Session, question_id: int, org_id: Optional[int] = None, *,
                       all_orgs: bool = False) -> Optional[Question]:
    return _scoped(db, org_id, all_orgs).filter(Question.id == question_id).first()


def create_question(db: Session, data: QuestionCreate, org_id: Optional[int] = None) -> Question:
    question = Question(
        text=data.text,
        category=data.category,
        difficulty=data.difficulty,
        organization_id=org_id,
    )
    db.add(question)
    db.commit()
    db.refresh(question)
    return question


def update_question(db: Session, question_id: int, data: QuestionUpdate, org_id: Optional[int] = None, *,
                    all_orgs: bool = False) -> Optional[Question]:
    question = get_question_by_id(db, question_id, org_id, all_orgs=all_orgs)
    if not question:
        return None
    if data.text is not None:
        question.text = data.text
    if data.category is not None:
        question.category = data.category
    if data.difficulty is not None:
        question.difficulty = data.difficulty
    db.commit()
    db.refresh(question)
    return question


def delete_question(db: Session, question_id: int, org_id: Optional[int] = None, *,
                    all_orgs: bool = False) -> bool:
    question = get_question_by_id(db, question_id, org_id, all_orgs=all_orgs)
    if not question:
        return False
    db.delete(question)
    db.commit()
    return True
