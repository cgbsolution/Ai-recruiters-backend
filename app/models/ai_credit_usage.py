"""
Tracks AI credit consumption per API call.

Every LLM invocation (Anthropic, Gemini, OpenAI) logs a row here so we can
visualise usage by feature, model, and time period on the admin dashboard.
"""

import enum
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, Float, Integer, String, Text
from app.database import Base


class AiFeature(str, enum.Enum):
    INTERVIEW = "interview"
    JD_GENERATION = "jd_generation"
    JD_PARSING = "jd_parsing"
    RESUME_PARSING = "resume_parsing"
    FIT_SCORE = "fit_score"
    QUESTION_GENERATION = "question_generation"
    ANSWER_EVALUATION = "answer_evaluation"
    QUESTION_SUGGESTION = "question_suggestion"
    OTHER = "other"


class AiCreditUsage(Base):
    __tablename__ = "ai_credit_usage"

    id = Column(Integer, primary_key=True, index=True)
    feature = Column(String(50), nullable=False, index=True)
    provider = Column(String(20), nullable=False)          # anthropic, gemini, openai
    model = Column(String(60), nullable=False)
    tier = Column(String(20), nullable=False)               # fast, advanced

    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    estimated_cost = Column(Float, nullable=False, default=0.0)  # USD

    job_id = Column(Integer, nullable=True)
    candidate_id = Column(Integer, nullable=True)
    session_id = Column(Integer, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
