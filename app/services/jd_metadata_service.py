"""
Extract structured metadata from a Job Description.
Used to build the job_metadata table for fast candidate matching.

Two modes:
1. Fast (no LLM): builds metadata directly from DB fields — instant, always works
2. LLM-enhanced: uses AI to extract richer keywords — optional, runs in background
"""

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.job import Job, JobSkill, JobResponsibility
from app.models.job_metadata import JobMetadata

logger = logging.getLogger(__name__)


def build_job_metadata_fast(db: Session, job_id: int) -> Optional[JobMetadata]:
    """
    Build JobMetadata using ONLY database fields — no LLM calls.
    This always succeeds and runs instantly.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return None

    skills = [s.skill for s in db.query(JobSkill).filter(JobSkill.job_id == job_id).all() if s.skill]
    responsibilities = [r.description for r in db.query(JobResponsibility).filter(JobResponsibility.job_id == job_id).all()]

    # Build keywords from title, skills, department, responsibilities
    keywords = set()

    # Add all skills as keywords
    for s in skills:
        keywords.add(s.strip().lower())

    # Extract words from job title
    if job.title:
        for word in job.title.lower().split():
            if len(word) > 2 and word not in {"the", "and", "for", "with", "from"}:
                keywords.add(word)
        # Also add full title as keyword
        keywords.add(job.title.strip().lower())

    # Extract key terms from department
    if job.department:
        keywords.add(job.department.strip().lower())

    # Extract key terms from responsibilities
    for r in responsibilities:
        # Extract tech-sounding words (3+ chars)
        for word in r.lower().split():
            cleaned = word.strip(".,;:()[]{}\"'")
            if len(cleaned) > 3 and cleaned not in {
                "will", "with", "that", "this", "from", "have", "been",
                "must", "should", "able", "work", "team", "experience",
                "role", "also", "such", "using", "used", "other", "well",
                "good", "need", "make", "like", "into", "about", "their",
                "they", "what", "when", "where", "which", "would", "could",
            }:
                keywords.add(cleaned)

    # Build responsibilities summary
    resp_summary = ". ".join(responsibilities[:3]) if responsibilities else ""

    # Infer designation from experience range
    exp_min = job.experience_min or 0
    exp_max = job.experience_max or 2
    if exp_min >= 10:
        designation = "Senior / Lead / Principal"
    elif exp_min >= 5:
        designation = "Senior"
    elif exp_min >= 3:
        designation = "Mid-Level"
    elif exp_min >= 1:
        designation = "Junior / Mid-Level"
    else:
        designation = "Junior / Entry-Level"

    # Upsert
    meta = db.query(JobMetadata).filter(JobMetadata.job_id == job_id).first()
    if not meta:
        meta = JobMetadata(job_id=job_id)
        db.add(meta)

    meta.parsed_title = job.title
    meta.required_skills = json.dumps(skills)
    meta.experience_range = f"{exp_min}-{exp_max} years"
    meta.location = job.location or ""
    meta.education_requirements = json.dumps([])
    meta.designation = designation
    meta.keywords = json.dumps(list(keywords))
    meta.responsibilities_summary = resp_summary[:500] if resp_summary else ""

    db.commit()
    db.refresh(meta)
    return meta


async def build_job_metadata(db: Session, job_id: int) -> Optional[JobMetadata]:
    """
    Build JobMetadata. First builds fast (no LLM) version,
    then tries to enhance with LLM for richer keywords.
    If LLM fails, the fast version is still usable.
    """
    # Step 1: Always build the fast version first
    meta = build_job_metadata_fast(db, job_id)
    if not meta:
        return None

    # Step 2: Try LLM enhancement (optional — failure is OK)
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        skills = [s.skill for s in db.query(JobSkill).filter(JobSkill.job_id == job_id).all() if s.skill]
        responsibilities = [r.description for r in db.query(JobResponsibility).filter(JobResponsibility.job_id == job_id).all()]

        # Try to extract text from uploaded JD file
        raw_jd_text = None
        if job.upload_jd:
            try:
                from app.services.resume_service import extract_text
                raw_jd_text = extract_text(job.upload_jd)
                meta.raw_jd_text = raw_jd_text
            except Exception:
                logger.warning("Could not extract text from JD file %s", job.upload_jd)

        data = await _extract_jd_metadata_llm(
            job_title=job.title,
            skills=skills,
            responsibilities=responsibilities,
            experience_min=job.experience_min or 0,
            experience_max=job.experience_max or 2,
            location=job.location or "",
            raw_jd_text=raw_jd_text,
        )

        # Merge LLM-extracted keywords with fast-extracted ones
        existing_keywords = set(json.loads(meta.keywords or "[]"))
        llm_keywords = set(data.get("keywords", []))
        llm_skills = set(data.get("required_skills", []))
        all_keywords = existing_keywords | {k.lower() for k in llm_keywords} | {s.lower() for s in llm_skills}

        meta.required_skills = json.dumps(data.get("required_skills", skills))
        meta.keywords = json.dumps(list(all_keywords))
        if data.get("designation"):
            meta.designation = data["designation"]
        if data.get("education_requirements"):
            meta.education_requirements = json.dumps(data["education_requirements"])
        if data.get("responsibilities_summary"):
            meta.responsibilities_summary = data["responsibilities_summary"]

        db.commit()
        db.refresh(meta)
        logger.info("Enhanced job metadata with LLM for job %s", job_id)

    except Exception as exc:
        logger.warning("LLM metadata extraction failed for job %s: %s (fast metadata is still available)", job_id, exc)

    return meta


async def _extract_jd_metadata_llm(
    job_title: str,
    skills: list[str],
    responsibilities: list[str],
    experience_min: int,
    experience_max: int,
    location: str,
    raw_jd_text: Optional[str] = None,
) -> dict:
    """Use LLM to extract structured metadata from a JD."""
    from app.services.ai_service import _ask, _parse_json, SONNET

    skills_str = ", ".join(skills) if skills else "Not specified"
    resp_str = "\n".join(f"- {r}" for r in responsibilities) if responsibilities else "Not specified"
    jd_section = f"\n\nFull JD Text:\n\"\"\"\n{raw_jd_text[:5000]}\n\"\"\"" if raw_jd_text else ""

    prompt = f"""You are an expert HR metadata extractor.
Given the following job description data, extract structured metadata for candidate matching.
Return ONLY valid JSON — no markdown fences, no extra text.

Job Title: {job_title}
Experience: {experience_min}-{experience_max} years
Location: {location or "Not specified"}
Required Skills: {skills_str}
Responsibilities:
{resp_str}
{jd_section}

Return JSON in EXACTLY this format:
{{
  "parsed_title": "Clean, standardized job title",
  "required_skills": ["skill1", "skill2", ...],
  "experience_range": "{experience_min}-{experience_max} years",
  "location": "{location or 'Not specified'}",
  "education_requirements": ["Required degree/education 1", ...],
  "designation": "Expected designation level (e.g. Junior, Senior, Lead, Manager)",
  "keywords": ["keyword1", "keyword2", ...],
  "responsibilities_summary": "2-3 sentence summary of key responsibilities"
}}

Rules:
- required_skills: comprehensive list including both explicit skills and implied ones from responsibilities
- keywords: 10-20 searchable terms (technologies, domains, certifications, soft skills, industry terms)
- education_requirements: infer from role level if not explicitly stated
- designation: infer from title and experience range
"""

    raw = await _ask(prompt, model=SONNET, max_tokens=1024)
    return _parse_json(raw)
