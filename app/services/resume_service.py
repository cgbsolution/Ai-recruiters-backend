"""
Resume parsing and fit-score calculation.

PDF text extraction uses pdfplumber; DOCX uses python-docx.
LLM calls are delegated to ai_service helpers (provider-agnostic).

Uses an enterprise-grade ATS engine prompt for deterministic,
production-quality resume parsing and JD fit scoring.
"""

import json
import logging
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

logger = logging.getLogger(__name__)


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
    """
    Enterprise-grade ATS resume parser.
    Reads every word of the resume and extracts structured data deterministically.
    """
    from app.services.ai_service import _ask_with_usage, _parse_json, SONNET
    from app.services.credit_service import log_usage

    resume_text = req.resume_text[:10000]

    prompt = f"""You are an enterprise-grade ATS (Applicant Tracking System) resume parser.
Your job is to extract ALL structured information from the resume with maximum accuracy.
You must behave as a deterministic system, not a chatbot.

RESUME TEXT:
\"\"\"
{resume_text}
\"\"\"

Return ONLY valid JSON — no markdown fences, no extra text.

{{
  "name": "Full name or null",
  "email": "Email address or null",
  "phone": "Phone number or null",
  "location": "City, Country or null",
  "experience_years": null,
  "current_role": "Most recent job title or null",
  "current_designation": "Current designation/title or null",
  "skills": [],
  "education": [],
  "certifications": [],
  "work_experience": [],
  "personal_details": {{}},
  "keywords": [],
  "summary": ""
}}

SKILLS EXTRACTION (CRITICAL — THIS IS THE MOST IMPORTANT PART):

Many resumes do NOT have a separate "Skills" section. You MUST extract skills from EVERY section:
- Professional summary / objective
- Work experience bullet points and descriptions
- Project descriptions
- Achievement statements
- Certifications and education

SKILL NORMALIZATION RULES:
- React.js / ReactJS / React JS → "React"
- Node.js / NodeJS → "Node.js"
- Next.js → "Next.js" (keep as is)
- PostgreSQL / Postgres → "PostgreSQL"
- MongoDB / Mongo → "MongoDB"
- Amazon Web Services → "AWS"
- Google Cloud Platform → "GCP"
- Microsoft Azure → "Azure"
- Kubernetes / K8s → "Kubernetes"
- CI/CD / CICD → "CI/CD"

DOMAIN SKILL EXTRACTION — extract from CONTEXT, not just keywords:
- "conducted IS audits" → "IS Auditing"
- "PCI-DSS compliance assessments" → "PCI-DSS"
- "managed team of 5" → "Team Leadership"
- "performed vulnerability assessments" → "Vulnerability Assessment"
- "risk assessment and gap analysis" → "Risk Assessment", "Gap Analysis"
- "ITGC testing" → "ITGC", "IT Controls"
- "SOX compliance" → "SOX"
- "ISO 27001 audits" → "ISO 27001"
- "GDPR implementation" → "GDPR"
- "stakeholder management" → "Stakeholder Management"
- "project management" → "Project Management"
- "developed security policies" → "Security Policy Development"

RULES:
- Remove duplicate skills (after normalization)
- Keep ONLY technical + domain skills (ignore generic soft skills like "hardworking", "team player")
- BUT keep measurable/professional skills: "Team Leadership", "Stakeholder Management", "Project Management"
- Be EXHAUSTIVE — extract 20-60 skills for experienced candidates
- Each skill = SHORT label (1-4 words)

EXPERIENCE EXTRACTION:
- Extract ALL positions — most recent first
- Each entry: {{"title": "", "company": "", "duration": "Start - End", "description": "responsibilities and achievements"}}
- If company missing → discard entry
- If role unclear → DO NOT guess
- Calculate experience_years from work history (e.g. 4.5 for 4 years 6 months). Round to 1 decimal.

EDUCATION:
- Format: ["Degree, Institution, Year"]
- Only extract if clearly present

CERTIFICATIONS:
- Extract ALL: CISA, CISSP, AWS SAA, PMP, ISO Lead Auditor, CEH, OSCP, etc.

KEYWORDS:
- 15-25 terms useful for search/matching (job titles, domains, technologies, industries, certifications)

SUMMARY:
- 2-3 sentence professional summary

PERSONAL DETAILS:
- {{"date_of_birth": null, "gender": null, "languages": [], "nationality": null, "marital_status": null}}
- Extract whatever is available, null for missing

STRICT RULES:
- NEVER hallucinate data — if not present, return null or empty
- NEVER guess missing information
- Output MUST be valid JSON only
"""

    result = await _ask_with_usage(prompt, model=SONNET, max_tokens=3500)
    log_usage(result, feature="resume_parsing")
    data = _parse_json(result.text)

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
    Context-aware AI fit score engine using weighted semantic evaluation.
    Uses LLM to understand meaning, not just keywords.
    """
    from app.services.ai_service import _ask_with_usage, _parse_json, SONNET
    from app.services.credit_service import log_usage

    required_skills_list = req.required_skills if req.required_skills else []
    responsibilities_list = req.responsibilities if req.responsibilities else []

    required_skills_str = json.dumps(required_skills_list)
    responsibilities_str = json.dumps(responsibilities_list)

    # Build JD context
    jd_section = ""
    if req.job_description:
        jd_section = f"""
Full Job Description Text:
\"\"\"
{req.job_description[:5000]}
\"\"\"
"""

    exp_min = req.required_experience_min if req.required_experience_min is not None else 0
    exp_max = req.required_experience_max if req.required_experience_max is not None else 0

    resume_text = req.resume_text[:10000]

    prompt = f"""You are an ATS scoring engine. Evaluate this resume against the job description.

JOB: {req.job_title}
Experience Required: {exp_min}-{exp_max} years
Skills: {required_skills_str}
Responsibilities: {responsibilities_str}
{jd_section}

RESUME:
\"\"\"
{resume_text}
\"\"\"

SCORING PHILOSOPHY — Score like a REAL recruiter who wants to FILL the role:
- If the candidate can DO the job with minimal training → HIGH FIT (75%+)
- Core domain experience matters most. A bookkeeper with 3+ years = strong candidate.
- Transferable skills ALWAYS count. US accounting → Canadian accounting = transferable.
- Similar tools count. QuickBooks experience covers QBO requirement.
- Missing 1-3 specific skills is NORMAL and trainable — do NOT heavily penalize.
- Experience at or above the required range = FULL experience credit (25/25).
- If candidate has 50%+ of listed skills + right experience → score 70%+.

CALIBRATION EXAMPLES (follow these score levels):
- 3+ years same domain + most skills + right tools → 75-85% HIGH
- 3+ years same domain + some skill gaps → 60-74% MEDIUM
- Related experience + major gaps → 45-59% MEDIUM
- Wrong domain or very junior → below 45% LOW

SCORING BANDS:
- 75-100 HIGH FIT: Can do the job, has domain experience + most skills
- 50-74 MEDIUM FIT: Relevant background, some trainable gaps
- 0-49 LOW FIT: Wrong domain or missing critical competencies

Return ONLY valid JSON:
{{
  "fit_level": "high/medium/low",
  "fit_percentage": 0,
  "summary": "One concise sentence about the candidate's fit.",
  "reasoning_points": [
    "Short bullet 1 — key strength or gap (max 15 words)",
    "Short bullet 2",
    "Short bullet 3",
    "Short bullet 4",
    "Short bullet 5"
  ],
  "verdict": "One-line recommendation",
  "key_matching_skills": ["skill1", "skill2"],
  "missing_weak_areas": ["gap1", "gap2"],
  "score_breakdown": {{
    "experience_relevance": 0,
    "jd_requirement_match": 0,
    "skills_and_tools": 0,
    "consulting_alignment": 0,
    "seniority_fit": 0
  }},
  "strength_mapping": {{
    "strong_matches": ["strength1"],
    "partial_matches": ["partial1"],
    "gaps": ["gap1"]
  }},
  "breakdown": {{
    "experience_match": "",
    "skills_match": "",
    "tech_stack_alignment": "",
    "role_alignment": ""
  }},
  "relevant_experience_summary": ""
}}

CRITICAL: Keep reasoning_points SHORT (4-6 bullets, each under 20 words). Keep summary to ONE sentence. Score GENEROUSLY for domain-relevant experience.
"""

    result = await _ask_with_usage(prompt, model=SONNET, max_tokens=3500)
    log_usage(result, feature="fit_score")
    try:
        data = _parse_json(result.text)
    except json.JSONDecodeError:
        # Try to fix truncated JSON by closing open braces/brackets
        raw = result.text.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        # Count open vs close braces/brackets and close them
        open_braces = raw.count("{") - raw.count("}")
        open_brackets = raw.count("[") - raw.count("]")
        # Remove trailing incomplete string/value
        raw = raw.rstrip()
        if raw and raw[-1] not in "]}\"0123456789":
            # Truncated mid-value — try to close the current string
            if '"' in raw:
                last_quote = raw.rfind('"')
                # Check if we're inside an unclosed string
                before_last = raw[:last_quote]
                if before_last.count('"') % 2 == 0:
                    raw = raw[:last_quote + 1]  # keep up to last complete string
            raw = raw.rstrip(",: \n\t")
        raw += "]" * max(0, open_brackets) + "}" * max(0, open_braces)
        try:
            data = json.loads(raw)
            logger.warning("Fixed truncated JSON for fit score (closed %d braces, %d brackets)", open_braces, open_brackets)
        except json.JSONDecodeError:
            logger.error("Could not parse fit score JSON even after fix attempt")
            raise

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
