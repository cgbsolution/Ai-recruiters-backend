from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user, get_org_id, is_global_admin
from app.database import get_db
from app.models.user import User
from app.schemas.question import QuestionCreate, QuestionRead, QuestionUpdate
from app.services import question_service

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("/", response_model=List[QuestionRead])
def list_questions(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return question_service.get_questions(db, org_id=get_org_id(user), all_orgs=is_global_admin(user))


@router.post("/", response_model=QuestionRead, status_code=status.HTTP_201_CREATED)
def create_question(payload: QuestionCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return question_service.create_question(db, payload, org_id=get_org_id(user))


@router.put("/{question_id}", response_model=QuestionRead)
def update_question(question_id: int, payload: QuestionUpdate, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    # Scoped to the caller's organization (a global super admin may edit any);
    # this used to look the question up by id alone, across every tenant.
    question = question_service.update_question(
        db, question_id, payload, org_id=get_org_id(user), all_orgs=is_global_admin(user)
    )
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    return question


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(question_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ok = question_service.delete_question(
        db, question_id, org_id=get_org_id(user), all_orgs=is_global_admin(user)
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Question not found")
