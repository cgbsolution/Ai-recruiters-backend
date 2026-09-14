"""
Extract structured metadata from a Job Description.
Used to build the job_metadata table for fast candidate matching.

Two modes:
1. Fast (no LLM): builds metadata directly from DB fields — instant, always works
2. LLM-enhanced: uses AI to extract richer keywords — optional, runs in background
"""

import json
import logging
import os
from typing import Optional

from sqlalchemy.orm import Session

from app import media
from app.models.job import Job, JobSkill, JobResponsibility
from app.models.job_metadata import JobMetadata

logger = logging.getLogger(__name__)

JD_FILE_EXTENSIONS = {".pdf", ".doc", ".docx"}
_JD_STORED_PREFIXES = ("/tmp/uploads/jd/", "tmp/uploads/jd/", "./uploads/jd/", "/uploads/jd/", "uploads/jd/", "jd/")


def resolve_jd_file(upload_jd: Optional[str]) -> Optional[str]:
    """Absolute path of an uploaded JD file, or None.

    Job.upload_jd is client-supplied (JobCreate/JobUpdate), so it must never be
    opened as-is — it used to reach extract_text() with any path on the host.
    Only an existing .pdf/.doc/.docx file DIRECTLY inside <UPLOAD_ROOT>/jd is
    accepted, given as a bare file name or as "/uploads/jd/<name>"."""
    if not upload_jd or not isinstance(upload_jd, str):
        return None
    name = upload_jd.strip().replace("\\", "/")
    if "\x00" in name or "?" in name or "#" in name or ":" in name:  # ":" — drive-relative paths on Windows
        return None
    for prefix in _JD_STORED_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if not name or "/" in name or name.startswith("."):
        return None
    if os.path.splitext(name)[1].lower() not in JD_FILE_EXTENSIONS:
        return None
    root = os.path.realpath(os.path.join(media.UPLOAD_ROOT, "jd"))
    full = os.path.realpath(os.path.join(root, name))
    try:
        if os.path.commonpath([root, full]) != root or os.path.dirname(full) != root:
            return None
    except ValueError:  # different drives on Windows
        return None
    return full if os.path.isfile(full) else None


def build_job_metadata_fast(db: Session, job_id: int) -> Optional[JobMetadata]:
    """
    Build JobMetadata using ONLY database fields — no LLM calls.
    This always succeeds and runs instantly.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return None

    raw_skills = [s.skill for s in db.query(JobSkill).filter(JobSkill.job_id == job_id).all() if s.skill]
    responsibilities = [r.description for r in db.query(JobResponsibility).filter(JobResponsibility.job_id == job_id).all()]

    # Normalize compound skills into shorter matchable terms
    # e.g., "Microsoft Dynamics 365 Customer Engagement" → also add "Dynamics 365", "CRM"
    skills = list(raw_skills)  # keep originals
    for s in raw_skills:
        # Break skills at common connectors
        lower = s.lower()
        # Add sub-terms split by "and", commas, parentheses
        for sep in [" and ", ", ", " / ", " or "]:
            if sep in lower:
                for part in s.split(sep if sep != lower else sep):
                    part = part.strip().strip("()[]")
                    if len(part) > 2 and part not in skills:
                        skills.append(part)
        # Extract parenthetical content as separate skills
        import re as _re
        paren_match = _re.findall(r'\(([^)]+)\)', s)
        for pm in paren_match:
            for sub in pm.split(","):
                sub = sub.strip()
                if len(sub) > 2 and sub not in skills:
                    skills.append(sub)

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

        # Try to extract text from uploaded JD file (only from the JD upload folder)
        raw_jd_text = None
        jd_path = resolve_jd_file(job.upload_jd)
        if jd_path:
            try:
                from app.services.resume_service import extract_text
                raw_jd_text = extract_text(jd_path)
                meta.raw_jd_text = raw_jd_text
            except Exception:
                logger.warning("Could not extract text from the JD file of job %s", job_id)

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

    prompt = f"""You are an expert HR metadata extractor for an ATS (Applicant Tracking System).
Your job is to extract structured metadata from a job description for CANDIDATE MATCHING.

The skills you extract will be matched against skills parsed from candidate resumes.
Therefore, you MUST normalize skills to SHORT, STANDARD forms that match how resumes list them.

Job Title: {job_title}
Experience: {experience_min}-{experience_max} years
Location: {location or "Not specified"}
Required Skills: {skills_str}
Responsibilities:
{resp_str}
{jd_section}

Return ONLY valid JSON — no markdown fences, no extra text.

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

CRITICAL RULES FOR required_skills:

1. NORMALIZE to SHORT labels (1-3 words max) that match how resumes list skills:
   BAD:  "Microsoft Dynamics 365 Customer Engagement"
   GOOD: "Dynamics 365", "CRM"

   BAD:  "D365 Sales Configuration and Customization"
   GOOD: "D365 Sales", "CRM Customization"

   BAD:  "End-to-End CRM Implementation"
   GOOD: "CRM Implementation"

   BAD:  "Proficiency in configuring and customizing D365 Sales business application"
   GOOD: "D365 Sales", "CRM Configuration"

   BAD:  "Strong understanding of data integration and migration processes"
   GOOD: "Data Integration", "Data Migration"

2. BREAK compound skills into individual matchable terms:
   "D365 CRM Model Apps (Sales, Customer Insight, Services)" → "Dynamics 365", "CRM", "D365 Sales", "Customer Insights", "D365 Customer Service"

3. Extract skills from ALL sections — not just the skills list:
   - From responsibilities: "Gather requirements" → "Requirement Gathering"
   - From responsibilities: "Deliver end-user training" → "Training Delivery"
   - From responsibilities: "Act as trusted advisor" → "Client Advisory"

4. Include BOTH the specific AND the general form:
   "React.js" → include both "React" AND "React.js"
   "D365 Customer Insights" → include both "Customer Insights" AND "Dynamics 365"

5. Include professional/consulting skills if required:
   "Stakeholder Management", "Solution Design", "Project Management", etc.

6. Target 15-30 skills for a typical JD
7. Each skill should be 1-3 words (occasionally 4 for specific tech names)

RULES FOR keywords:
- 15-25 searchable terms (technologies, domains, certifications, methodologies, industries)
- Include variations: "D365" AND "Dynamics 365" AND "CRM"
- Include certifications mentioned in JD
- Include industry/domain terms
"""

    raw = await _ask(prompt, model=SONNET, max_tokens=1024)
    return _parse_json(raw)
