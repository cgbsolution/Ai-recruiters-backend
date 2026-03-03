from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class QuestionCreate(BaseModel):
    text: str
    category: str
    difficulty: str


class QuestionUpdate(BaseModel):
    text: Optional[str] = None
    category: Optional[str] = None
    difficulty: Optional[str] = None


class QuestionRead(BaseModel):
    id: int
    text: str
    category: str
    difficulty: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    model_config = {"from_attributes": True}
