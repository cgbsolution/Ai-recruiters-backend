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

async def parse_resume(req: ParseResumeRequest) -> ParseResumeResponse:
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
  "current_designation": "Current or most recent designation/title or null",
  "skills": ["skill1", "skill2", ...],
  "education": ["Degree, Institution, Year", ...],
  "certifications": ["Certification name", ...],
  "work_experience": [
    {{
      "title": "Job title",
      "company": "Company name",
      "duration": "Start - End",
      "description": "Brief description"
    }}
  ],
  "personal_details": {{
    "date_of_birth": "DOB or null",
    "gender": "Gender or null",
    "languages": ["Language1", "Language2"],
    "nationality": "Nationality or null",
    "marital_status": "Status or null"
  }},
  "keywords": ["keyword1", "keyword2", ...],
  "summary": "2-3 sentence professional summary of the candidate"
}}

Rules:
- experience_years should be a number (e.g. 4.5 for 4 years 6 months)
- skills should be a flat list of individual technologies/tools/soft skills
- certifications should list any professional certifications (AWS, PMP, etc.)
- keywords should be 10-20 important terms from the resume useful for search/matching (job titles, domains, technologies, industries)
- personal_details: extract whatever personal info is available; use null for missing fields
- If a field cannot be determined, use null
- For empty arrays use []
"""

    raw = await _ask(prompt, model=SONNET, max_tokens=2048)
    data = _parse_json(raw)

    parsed = ParsedResumeData(
        name=data.get("name"),
        email=data.get("email"),
        phone=data.get("phone"),
        location=data.get("location"),
        experience_years=data.get("experience_years"),
        current_role=data.get("current_role"),
        current_designation=data.get("current_designation"),
        skills=data.get("skills", []),
        education=data.get("education", []),
        certifications=data.get("certifications", []),
        work_experience=data.get("work_experience", []),
        personal_details=data.get("personal_details"),
        keywords=data.get("keywords", []),
        summary=data.get("summary"),
    )

    return ParseResumeResponse(
        parsed_data=parsed,
        raw_text_length=len(req.resume_text),
    )


# ── LLM-backed Fit Score Calculation ─────────────────────────────────────────

async def calculate_fit_score(req: FitScoreRequest) -> FitScoreResponse:
    """
    Advanced AI Resume-to-JD semantic matching engine.
    Uses a 6-dimension weighted scoring model with skill normalization,
    semantic matching, and contextual understanding — NOT keyword matching.
    """
    from app.services.ai_service import _ask, _parse_json, SONNET

    skills_str = ", ".join(req.required_skills) if req.required_skills else "Not specified"
    responsibilities_str = "\n".join(f"- {r}" for r in req.responsibilities) if req.responsibilities else "Not specified"
    exp_range = f"{req.required_experience_min}-{req.required_experience_max} years" if req.required_experience_min is not None else "Not specified"

    # Include full JD text if available for deeper semantic matching
    jd_text_section = ""
    if req.job_description:
        jd_text_section = f"""
Full Job Description:
\"\"\"
{req.job_description[:4000]}
\"\"\"
"""

    prompt = f"""You are an advanced AI Resume Parsing and JD Matching Engine. Think like a SENIOR HIRING MANAGER.

Your job is to perform DEEP SEMANTIC MATCHING between a candidate's resume and the job description.
Use contextual understanding — NOT simple keyword matching.

CRITICAL RULES:
- Match EQUIVALENT skills even if wording differs (e.g. "ITGC" == "IS Auditing", "Gap analysis" == "Risk Management")
- DO NOT penalize for missing exact keywords if equivalent experience is demonstrated
- DO reward equivalent/transferable experience inferred from context
- DO NOT mark a skill as missing if it can be reasonably inferred from the candidate's projects/experience
- Penalize ONLY if a required competency is truly absent
- Be strict but FAIR — realistic scoring: 70-85 for good candidates, not artificially low

---

JOB DESCRIPTION:
- Title: {req.job_title}
- Experience Required: {exp_range}
- Required Skills: {skills_str}
- Key Responsibilities:
{responsibilities_str}
{jd_text_section}

CANDIDATE RESUME:
\"\"\"
{req.resume_text[:6000]}
\"\"\"

---

SCORING MODEL (6 dimensions, total 100):

1. Core Skills Match (40 points):
   - % of required skills matched via SEMANTIC comparison
   - Map raw skills to normalized categories (e.g. "Nessus, Qualys" → "Vulnerability Management")
   - Include skills found in experience/projects sections, not just listed skills

2. Experience Fit (15 points):
   - Years of experience match
   - Domain/industry relevance
   - Seniority level alignment

3. Responsibility Match (15 points):
   - How well past roles align with JD responsibilities
   - End-to-end ownership signals
   - Project complexity alignment

4. Tools & Technology (10 points):
   - Specific tools, platforms, frameworks match
   - Adjacent/equivalent tool experience counts

5. Leadership & Ownership (10 points):
   - Team management, mentoring, stakeholder management
   - Project ownership and initiative signals
   - Cross-functional collaboration

6. Compliance & Domain Coverage (10 points):
   - Regulatory frameworks (GDPR, SOX, HIPAA, PCI-DSS, etc.)
   - Industry-specific domain knowledge
   - Certifications relevance

---

Return ONLY valid JSON — no markdown fences, no extra text:
{{
  "fit_level": "high" | "medium" | "low",
  "fit_percentage": <integer 0-100>,
  "summary": "2-3 sentence overall fit assessment written like a senior recruiter's verdict",

  "strength_mapping": {{
    "strong_matches": [
      "Skill/area with high alignment — brief evidence from resume"
    ],
    "partial_matches": [
      "Skill/area with partial coverage — what's present and what's weak"
    ],
    "gaps": [
      "Required skill/area that is truly absent from the resume"
    ]
  }},

  "score_breakdown": {{
    "core_skills": <0-40>,
    "experience_fit": <0-15>,
    "responsibility_match": <0-15>,
    "tools_technology": <0-10>,
    "leadership_ownership": <0-10>,
    "compliance_domain": <0-10>
  }},

  "key_matching_skills": [
    "Matched skill with evidence (3-10 items)"
  ],
  "relevant_experience_summary": "Concise 3-5 sentence paragraph synthesizing ONLY the candidate's experience directly relevant to this JD. Do NOT copy-paste resume text.",
  "missing_weak_areas": [
    "JD requirement NOT clearly evidenced in resume (2-6 items)"
  ],
  "breakdown": {{
    "experience_match": "Detailed assessment of experience years + domain fit",
    "skills_match": "Detailed assessment of skills overlap (semantic, not keyword)",
    "tech_stack_alignment": "Specific tools/tech overlap analysis",
    "role_alignment": "How past roles map to this JD's responsibilities"
  }},
  "reasoning_points": [
    "Key positive or negative observation (4-6 items)"
  ],
  "verdict": "One-line hiring recommendation (e.g. 'Strong fit for Senior role, slightly junior for Lead position')"
}}

Scoring bands:
- high   (75-100): Strong match — experience aligns, most core skills present, clear role fit
- medium (45-74):  Partial match — some gaps but transferable skills present
- low    (0-44):   Poor match — critical requirements missing
"""

    raw = await _ask(prompt, model=SONNET, max_tokens=2500)
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
        # New advanced fields
        score_breakdown=data.get("score_breakdown"),
        strength_mapping=data.get("strength_mapping"),
        verdict=data.get("verdict"),
    )


# ── File-based Helpers ────────────────────────────────────────────────────────

async def parse_resume_file(file_path: str, job_id: Optional[int] = None) -> dict:
    """
    Full pipeline: extract text → LLM parse → return dict.
    Returns both the raw extracted text and the parsed structured data.
    """
    text = extract_text(file_path)
    req = ParseResumeRequest(resume_text=text, job_id=job_id)
    result = await parse_resume(req)
    return {
        "text": text,
        "parsed": result.parsed_data.model_dump(),
    }
