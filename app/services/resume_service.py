"""
Resume parsing and fit-score calculation.

PDF text extraction uses pdfplumber; DOCX uses python-docx.
LLM calls (Anthropic Claude) are delegated to ai_service helpers.
"""

import json
import os
from pathlib import Path
from typing import Optional

from app.schemas.ai import (
    FitScoreRequest,
    FitScoreResponse,
    ParseResumeRequest,
    ParseResumeResponse,
    ParsedResumeData,
    FitScoreBreakdown,
)


# ── Text Extraction ───────────────────────────────────────────────────────────

def extract_text_from_pdf(file_path: str) -> str:
    """Extract plain text from a PDF file using pdfplumber."""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n".join(text_parts)
    except ImportError:
        raise RuntimeError("pdfplumber is not installed. Run: pip install pdfplumber")
    except Exception as exc:
        raise RuntimeError(f"PDF extraction failed: {exc}") from exc


def extract_text_from_docx(file_path: str) -> str:
    """Extract plain text from a DOCX file using python-docx."""
    try:
        from docx import Document
        doc = Document(file_path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)
    except ImportError:
        raise RuntimeError("python-docx is not installed. Run: pip install python-docx")
    except Exception as exc:
        raise RuntimeError(f"DOCX extraction failed: {exc}") from exc


def extract_text(file_path: str) -> str:
    """Dispatch to the correct extractor based on file extension."""
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(file_path)
    elif ext in (".doc", ".docx"):
        return extract_text_from_docx(file_path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")


# ── LLM-backed Resume Parsing ─────────────────────────────────────────────────

def parse_resume(req: ParseResumeRequest) -> ParseResumeResponse:
    """Use Claude to extract structured data from raw resume text."""
    from app.services.ai_service import _ask, _parse_json, SONNET

    prompt = f"""You are an expert HR assistant that extracts structured information from resumes.
Given the following resume text, extract all relevant information.
Return ONLY valid JSON — no markdown fences, no extra text.

Resume Text:
\"\"\"
{req.resume_text[:6000]}
\"\"\"

Return JSON in EXACTLY this format:
{{
  "name": "Full name of the candidate or null",
  "email": "Email address or null",
  "phone": "Phone number or null",
  "location": "City, Country or null",
  "experience_years": <number or null>,
  "current_role": "Most recent job title or null",
  "skills": ["skill1", "skill2", ...],
  "education": ["Degree, Institution, Year", ...],
  "work_experience": [
    {{
      "title": "Job title",
      "company": "Company name",
      "duration": "Start - End",
      "description": "Brief description"
    }}
  ],
  "summary": "2-3 sentence professional summary of the candidate"
}}

Rules:
- experience_years should be a number (e.g. 4.5 for 4 years 6 months)
- skills should be a flat list of individual technologies/tools
- If a field cannot be determined, use null
"""

    raw = _ask(prompt, model=SONNET, max_tokens=2048)
    data = _parse_json(raw)

    parsed = ParsedResumeData(
        name=data.get("name"),
        email=data.get("email"),
        phone=data.get("phone"),
        location=data.get("location"),
        experience_years=data.get("experience_years"),
        current_role=data.get("current_role"),
        skills=data.get("skills", []),
        education=data.get("education", []),
        work_experience=data.get("work_experience", []),
        summary=data.get("summary"),
    )

    return ParseResumeResponse(
        parsed_data=parsed,
        raw_text_length=len(req.resume_text),
    )


# ── LLM-backed Fit Score Calculation ─────────────────────────────────────────

def calculate_fit_score(req: FitScoreRequest) -> FitScoreResponse:
    """Use Claude to compare a resume against a job description and produce a fit score."""
    from app.services.ai_service import _ask, _parse_json, SONNET

    skills_str = ", ".join(req.required_skills) if req.required_skills else "Not specified"
    responsibilities_str = "\n".join(f"- {r}" for r in req.responsibilities) if req.responsibilities else "Not specified"
    exp_range = f"{req.required_experience_min}-{req.required_experience_max} years" if req.required_experience_min is not None else "Not specified"

    prompt = f"""You are an expert AI technical recruiter performing a semantic resume-to-JD match analysis.
Compare the candidate's resume against the job description and return a structured analysis.
Return ONLY valid JSON — no markdown fences, no extra text.

JOB DESCRIPTION:
- Title: {req.job_title}
- Experience Required: {exp_range}
- Required Skills: {skills_str}
- Key Responsibilities:
{responsibilities_str}

CANDIDATE RESUME:
\"\"\"
{req.resume_text[:5000]}
\"\"\"

Perform a deep semantic comparison and return JSON in EXACTLY this format:
{{
  "fit_level": "high" | "medium" | "low",
  "fit_percentage": <integer 0-100>,
  "summary": "1-2 sentence overall fit summary",
  "key_matching_skills": [
    "Skill or technology from resume that directly matches JD requirement",
    "Another matching skill",
    "..."
  ],
  "relevant_experience_summary": "A concise 3-5 sentence paragraph summarizing ONLY the candidate's experience, roles, and achievements that are directly relevant to this specific JD. Do NOT repeat the full resume — synthesize only what matters for this role.",
  "missing_weak_areas": [
    "Skill or requirement from JD that is absent or under-represented in the resume",
    "Another gap or weak area"
  ],
  "breakdown": {{
    "experience_match": "How well candidate's years/type of experience matches the JD requirement",
    "skills_match": "How well candidate's skills overlap with required skills",
    "tech_stack_alignment": "Degree of overlap between candidate's tech stack and JD tech stack",
    "role_alignment": "How closely candidate's past roles/responsibilities align with this JD"
  }},
  "reasoning_points": [
    "Key positive or negative observation 1",
    "Key observation 2",
    "Key observation 3",
    "Key observation 4"
  ]
}}

Scoring criteria:
- high   (fit_percentage 75-100): Strong match — experience within range, most required skills present, direct role alignment
- medium (fit_percentage 40-74): Partial match — slightly outside experience range OR missing some key skills
- low    (fit_percentage 0-39):  Poor match — significantly outside range AND/OR missing critical required skills

Rules:
- fit_percentage must reflect ACTUAL content comparison (e.g. 82 for strong match, 55 for partial, 25 for poor)
- key_matching_skills: list only skills explicitly found in BOTH the resume AND the JD (3-8 items)
- missing_weak_areas: list only JD requirements NOT clearly evidenced in the resume (2-5 items)
- relevant_experience_summary: synthesize, do NOT copy-paste resume text
"""

    raw = _ask(prompt, model=SONNET, max_tokens=1500)
    data = _parse_json(raw)

    raw_pct = data.get("fit_percentage", 0)
    try:
        fit_percentage = max(0, min(100, int(raw_pct)))
    except (TypeError, ValueError):
        fit_percentage = 0

    breakdown_data = data.get("breakdown", {})
    return FitScoreResponse(
        fit_level=data.get("fit_level", "low"),
        fit_percentage=fit_percentage,
        summary=data.get("summary", ""),
        key_matching_skills=data.get("key_matching_skills", []),
        relevant_experience_summary=data.get("relevant_experience_summary", ""),
        missing_weak_areas=data.get("missing_weak_areas", []),
        breakdown=FitScoreBreakdown(
            experience_match=breakdown_data.get("experience_match", ""),
            skills_match=breakdown_data.get("skills_match", ""),
            tech_stack_alignment=breakdown_data.get("tech_stack_alignment", ""),
            role_alignment=breakdown_data.get("role_alignment", ""),
        ),
        reasoning_points=data.get("reasoning_points", []),
    )


# ── File-based Helpers ────────────────────────────────────────────────────────

def parse_resume_file(file_path: str, job_id: Optional[int] = None) -> dict:
    """
    Full pipeline: extract text → LLM parse → return dict.
    Returns both the raw extracted text and the parsed structured data.
    """
    text = extract_text(file_path)
    req = ParseResumeRequest(resume_text=text, job_id=job_id)
    result = parse_resume(req)
    return {
        "text": text,
        "parsed": result.parsed_data.model_dump(),
    }
