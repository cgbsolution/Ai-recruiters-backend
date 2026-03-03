from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.question import Question
from app.schemas.question import QuestionCreate, QuestionUpdate


def get_questions(db: Session) -> List[Question]:
    return db.query(Question).order_by(Question.created_at.desc()).all()


def get_question_by_id(db: Session, question_id: int) -> Optional[Question]:
    return db.query(Question).filter(Question.id == question_id).first()


def create_question(db: Session, data: QuestionCreate) -> Question:
    question = Question(
        text=data.text,
        category=data.category,
        difficulty=data.difficulty,
    )
    db.add(question)
    db.commit()
    db.refresh(question)
    return question


def update_question(db: Session, question_id: int, data: QuestionUpdate) -> Optional[Question]:
    question = db.query(Question).filter(Question.id == question_id).first()
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


def delete_question(db: Session, question_id: int) -> bool:
    question = db.query(Question).filter(Question.id == question_id).first()
    if not question:
        return False
    db.delete(question)
    db.commit()
    return True
