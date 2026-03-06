from fastapi import APIRouter, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

from app.schemas.ai import (
    EvaluateCandidateRequest,
    EvaluateCandidateResponse,
    FitScoreRequest,
    FitScoreResponse,
    GenerateJDRequest,
    GenerateJDResponse,
    GenerateQuestionsRequest,
    GenerateQuestionsResponse,
    ParseResumeRequest,
    ParseResumeResponse,
    SuggestQuestionsRequest,
    SuggestQuestionsResponse,
)
from app.services import ai_service

router = APIRouter()


@router.post(
    "/generate-jd",
    response_model=GenerateJDResponse,
    summary="Generate Job Description",
    description="Uses Claude Sonnet to generate a complete structured JD from a title/description. Returns title, responsibilities, 2–4 skill groups with criteria/weight, special instructions, and coding question count.",
)
@limiter.limit("5/minute")
async def generate_jd(request: Request, payload: GenerateJDRequest):
    try:
        return await ai_service.generate_jd(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")


@router.post(
    "/generate-questions",
    response_model=GenerateQuestionsResponse,
    summary="Generate interview questions",
    description="Generates `count` interview questions for a given job title, skills, difficulty, and interview mode (TRADITIONAL / SCENARIO) using Claude Sonnet.",
)
@limiter.limit("5/minute")
async def generate_questions(request: Request, payload: GenerateQuestionsRequest):
    try:
        return await ai_service.generate_questions(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")


@router.post(
    "/evaluate-answer",
    response_model=EvaluateCandidateResponse,
    summary="Evaluate candidate interview answer",
    description="Scores a candidate's answer (0–100) against a question using Claude Opus. Returns score, feedback, and pass/fail status.",
)
@limiter.limit("5/minute")
async def evaluate_answer(request: Request, payload: EvaluateCandidateRequest):
    try:
        return await ai_service.evaluate_answer(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI evaluation failed: {str(e)}")


@router.post(
    "/suggest-questions",
    response_model=SuggestQuestionsResponse,
    summary="Suggest questions by category/difficulty",
    description="Returns `count` question suggestions for a given category and difficulty level using Claude Sonnet.",
)
@limiter.limit("5/minute")
async def suggest_questions(request: Request, payload: SuggestQuestionsRequest):
    try:
        return await ai_service.suggest_questions(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI suggestion failed: {str(e)}")


@router.post(
    "/parse-resume",
    response_model=ParseResumeResponse,
    summary="Parse resume text with LLM",
    description=(
        "Extracts structured data from raw resume text using Claude Sonnet.\n\n"
        "Returns: `name`, `email`, `phone`, `location`, `experience_years`, `current_role`, "
        "`skills[]`, `education[]`, `work_experience[]`, `summary`.\n\n"
        "Pass `resume_text` extracted from a PDF/DOCX file. "
        "For file uploads with auto-parsing use `POST /candidates/upload-resume` instead."
    ),
)
@limiter.limit("5/minute")
async def parse_resume(request: Request, payload: ParseResumeRequest):
    try:
        return await ai_service.parse_resume(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resume parsing failed: {str(e)}")


@router.post(
    "/calculate-fit-score",
    response_model=FitScoreResponse,
    summary="Calculate resume fit score against a JD",
    description=(
        "Compares a candidate's resume text against job requirements using Claude Sonnet.\n\n"
        "Returns:\n"
        "- `fit_level`: `low` | `medium` | `high`\n"
        "- `summary`: 1–2 sentence overall fit summary\n"
        "- `breakdown`: experience_match, skills_match, tech_stack_alignment, role_alignment\n"
        "- `reasoning_points`: bullet-point list shown in the fit score tooltip\n\n"
        "**Scoring criteria:**\n"
        "- `high` — experience in range, most skills present, direct role alignment\n"
        "- `medium` — slight mismatch in experience or missing some skills\n"
        "- `low` — significant mismatch or missing core required skills"
    ),
)
@limiter.limit("5/minute")
async def calculate_fit_score(request: Request, payload: FitScoreRequest):
    try:
        return await ai_service.calculate_fit_score(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fit score calculation failed: {str(e)}")
