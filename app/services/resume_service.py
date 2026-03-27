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
    Enterprise-grade ATS fit score engine.
    Deterministic resume-to-JD matching with weighted scoring model.
    Behaves like a production ATS system — no hallucination, no inflation.
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

    prompt = f"""You are an enterprise-grade ATS (Applicant Tracking System) engine.
Your job is to calculate a precise FIT SCORE between a candidate's resume and a job description.
You must behave as a deterministic system, not a chatbot.

---

INPUT:

Job Description:
- Title: {req.job_title}
- Required Experience: {exp_min}-{exp_max} years
- Required Skills: {required_skills_str}
- Key Responsibilities: {responsibilities_str}
{jd_section}

Candidate Resume:
\"\"\"
{resume_text}
\"\"\"

---

STEP 1: PARSE THE RESUME

Read EVERY WORD of the resume. Extract:
- All skills (from ALL sections — summary, experience, projects, certifications, NOT just a skills section)
- All work experience with roles, companies, durations
- Total experience in years
- Education and certifications

SKILL NORMALIZATION — apply before matching:
- React.js / ReactJS → React
- Node.js / NodeJS → Node.js
- Next.js → Next.js (keep)
- Remove duplicates
- Keep only technical + domain skills (ignore "team player", "hardworking")
- BUT keep professional skills: "Team Leadership", "Stakeholder Management", "Project Management"

DOMAIN SKILL EXTRACTION — extract from context:
- "conducted IS audits" → "IS Auditing"
- "PCI-DSS compliance" → "PCI-DSS"
- "managed team" → "Team Leadership"
- "vulnerability assessments" → "Vulnerability Assessment"
- "risk assessment" → "Risk Assessment"
- "ITGC testing" → "ITGC"
- "SOX compliance" → "SOX"
- "ISO 27001" → "ISO 27001"
- "GDPR" → "GDPR"
- "NIST CSF" → "NIST CSF"
- "COBIT" → "COBIT"

ROLE NORMALIZATION:
- SDE / Developer / Software Dev → Software Engineer
- Frontend Dev → Frontend Developer
- IS Auditor / IT Auditor → Information Systems Auditor

---

STEP 2: FIT SCORE CALCULATION (Total = 100 points)

1. SKILLS MATCH (40 points):
   - Required skills matching = 30 points
   - Preferred/additional skills = 10 points
   - Use SEMANTIC matching: React == React.js, ITGC == IT General Controls
   - "Gap analysis" ≈ "Risk Assessment" (related domain)
   - If resume says "conducted PCI-DSS audits" → PCI-DSS is matched
   - Score = (matched_required / total_required) * 30 + (matched_preferred / total_preferred) * 10

2. EXPERIENCE MATCH (30 points):
   - Compare candidate total experience vs required {exp_min}-{exp_max} years
   - If equal or higher → full 30 points
   - If lower → proportional (e.g. 3 years for 5-year req → 3/5 * 30 = 18)
   - Also evaluate RELEVANCE of experience to the JD roles/domain

3. ROLE MATCH (20 points):
   - Match resume job titles/roles against JD title and responsibilities
   - Frontend Developer ≈ React Developer (high match)
   - IS Auditor ≈ Project Leader IS Audit (high match)
   - Software Engineer ≈ Backend Developer if skills align

4. EDUCATION MATCH (10 points):
   - If JD explicitly requires specific education → score based on match
   - If JD has no education requirements → give full 10 points

---

STEP 3: GENERATE OUTPUT

Return ONLY valid JSON — no markdown fences, no extra text:

{{
  "fit_level": "high or medium or low",
  "fit_percentage": 0,
  "summary": "2-3 sentence overall fit assessment like a senior recruiter verdict",

  "score_breakdown": {{
    "core_skills": 0,
    "experience_fit": 0,
    "responsibility_match": 0,
    "tools_technology": 0,
    "leadership_ownership": 0,
    "compliance_domain": 0
  }},

  "strength_mapping": {{
    "strong_matches": ["Skill/area with high alignment — evidence from resume"],
    "partial_matches": ["Skill/area with partial coverage — what is present and weak"],
    "gaps": ["Required skill/area truly absent from resume"]
  }},

  "key_matching_skills": ["Matched skill 1", "Matched skill 2"],
  "missing_weak_areas": ["Missing requirement 1", "Missing requirement 2"],

  "relevant_experience_summary": "3-5 sentence paragraph of candidate experience relevant to this JD",

  "breakdown": {{
    "experience_match": "Assessment of experience years + domain fit",
    "skills_match": "Assessment of skills overlap using semantic matching",
    "tech_stack_alignment": "Specific tools/tech overlap analysis",
    "role_alignment": "How past roles map to JD responsibilities"
  }},

  "reasoning_points": ["Key observation 1", "Key observation 2"],
  "verdict": "One-line hiring recommendation"
}}

---

SCORING BANDS:
- high   (75-100): Strong match — most required skills present, experience aligns, clear role fit
- medium (45-74):  Partial match — some gaps but transferable skills present
- low    (0-44):   Poor match — critical requirements missing

---

CRITICAL RULES:
- NEVER hallucinate — only report what is IN the resume
- NEVER inflate scores — be strict but FAIR
- Match EQUIVALENT skills even if wording differs (semantic matching)
- DO NOT penalize for missing exact keywords if equivalent experience is demonstrated
- DO reward transferable/equivalent experience inferred from context
- DO NOT mark a skill as missing if it can be reasonably inferred from projects/experience
- Penalize ONLY if a required competency is truly absent
- If experience says "performed ITGC audits, SOX testing, PCI-DSS reviews" → those ARE matched skills
- fit_percentage = sum of all dimension scores (must equal the total)
- Output MUST be valid JSON only

IMPORTANT: The fit_percentage MUST equal the sum of score_breakdown values.
Map the 4-dimension model (Skills 40 + Experience 30 + Role 20 + Education 10 = 100)
to the 6-dimension output by splitting Skills into core_skills + tools_technology + compliance_domain,
and Experience into experience_fit + responsibility_match + leadership_ownership.
"""

    result = await _ask_with_usage(prompt, model=SONNET, max_tokens=3000)
    log_usage(result, feature="fit_score")
    data = _parse_json(result.text)

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
