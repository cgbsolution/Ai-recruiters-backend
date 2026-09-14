import logging
import os
import traceback
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
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
from app.auth import get_current_user
from app.media import UPLOAD_ROOT
from app.services import ai_service

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


def _ai_failure(action: str, exc: Exception) -> HTTPException:
    """500 for a failed AI call. The provider/internal error text goes to the log
    instead of the response body (it can carry SQL, paths or provider details)."""
    logger.error("[AI] %s failed: %s: %s", action, type(exc).__name__, exc, exc_info=exc)
    return HTTPException(status_code=500, detail=f"{action} failed. Please try again.")


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
        raise _ai_failure("AI generation", e)


ALLOWED_JD_EXTENSIONS = {".pdf", ".doc", ".docx"}


@router.post(
    "/parse-jd-file",
    response_model=GenerateJDResponse,
    summary="Parse uploaded JD file into structured data",
    description="Accepts a PDF/DOC/DOCX file, extracts text, and uses AI to parse it into the same structured format as generate-jd.",
)
@limiter.limit("5/minute")
async def parse_jd_file(request: Request, file: UploadFile = File(...)):
    from app.config import settings
    from app.services.resume_service import extract_text

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_JD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only PDF, DOC, and DOCX files are accepted")

    content = await file.read()
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=400, detail=f"File exceeds {settings.MAX_UPLOAD_SIZE_MB}MB limit")

    # Save to temp location for text extraction
    upload_dir = os.path.join(UPLOAD_ROOT, "jd")
    os.makedirs(upload_dir, exist_ok=True)
    filename = f"{uuid.uuid4()}{ext}"
    path = os.path.join(upload_dir, filename)

    try:
        with open(path, "wb") as f:
            f.write(content)

        try:
            jd_text = extract_text(path)
        except RuntimeError as e:
            # extract_text's RuntimeErrors describe the uploaded file itself
            # (e.g. a scanned, image-only PDF) and are meant for the user.
            logging.warning(f"[JD Parse] text extraction failed for file '{file.filename}': {e}")
            raise HTTPException(status_code=500, detail=f"JD parsing failed: {e}")
        if not jd_text.strip():
            raise HTTPException(status_code=422, detail="Could not extract any text from the uploaded file")

        return await ai_service.parse_jd_text(jd_text)
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"[JD Parse] FAILED for file '{file.filename}': {type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="JD parsing failed. Please try again.")
    finally:
        # Clean up temp file
        if os.path.exists(path):
            os.remove(path)


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
        raise _ai_failure("AI generation", e)


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
        raise _ai_failure("AI evaluation", e)


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
        raise _ai_failure("AI suggestion", e)


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
        raise _ai_failure("Resume parsing", e)


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
        raise _ai_failure("Fit score calculation", e)
