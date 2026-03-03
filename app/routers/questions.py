from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.question import QuestionCreate, QuestionRead, QuestionUpdate
from app.services import question_service

router = APIRouter()


@router.get("/", response_model=List[QuestionRead])
def list_questions(db: Session = Depends(get_db)):
    return question_service.get_questions(db)


@router.post("/", response_model=QuestionRead, status_code=status.HTTP_201_CREATED)
def create_question(payload: QuestionCreate, db: Session = Depends(get_db)):
    return question_service.create_question(db, payload)


@router.put("/{question_id}", response_model=QuestionRead)
def update_question(question_id: int, payload: QuestionUpdate, db: Session = Depends(get_db)):
    question = question_service.update_question(db, question_id, payload)
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    return question


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(question_id: int, db: Session = Depends(get_db)):
    ok = question_service.delete_question(db, question_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Question not found")
