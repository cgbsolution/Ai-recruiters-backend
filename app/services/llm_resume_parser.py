"""
LLM-enhanced resume parser — uses Claude for high-accuracy extraction.

Used as a quality layer on top of the fast regex parser:
  - Validates/corrects regex output for low-confidence results
  - Extracts structured data when regex fails (bad name, no skills, etc.)

Cost: ~$0.003 per resume (Haiku) — affordable even at scale.
Speed: ~1-2 seconds per resume.

Usage:
  result = await llm_parse_resume(raw_text)
  # Returns same dict format as fast_parse_resume
"""

import json
import logging
import re
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Use Haiku for cost efficiency — still very accurate for extraction
_MODEL = "claude-haiku-4-5-20251001"


async def llm_parse_resume(raw_text: str, existing_data: dict | None = None) -> dict:
    """
    Parse a resume using Claude for high-accuracy extraction.

    If existing_data is provided, the LLM validates and corrects it.
    Otherwise, extracts from scratch.
    """
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    # Truncate to avoid token limits — first 6000 chars is enough for metadata
    text_excerpt = raw_text[:6000] if raw_text else ""

    # Build the prompt
    if existing_data:
        prompt = _build_correction_prompt(text_excerpt, existing_data)
    else:
        prompt = _build_extraction_prompt(text_excerpt)

    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )

        content = response.content[0].text.strip()
        parsed = _parse_llm_response(content)

        if parsed:
            return parsed
        else:
            logger.warning("LLM returned unparseable response")
            return existing_data or {}

    except Exception as e:
        logger.warning("LLM resume parse failed: %s", e)
        return existing_data or {}


def _build_extraction_prompt(text: str) -> str:
    return f"""Extract the following metadata from this resume. Return ONLY a JSON object with these fields:

- "candidate_name": full name of the person (NOT company name, NOT "Foundit", NOT section headers)
- "phone": phone number
- "current_designation": current or most recent job title (max 60 chars, just the primary role)
- "experience_years": total years of experience as a number (null if unknown)
- "location": current city/location
- "skills": array of TECHNICAL skills only (programming languages, frameworks, tools, databases, cloud services). NO soft skills, NO dates, NO locations, NO education, NO hobbies. Max 30 items.
- "education": array of strings like "B.Tech in CS, XYZ University, 2020"
- "certifications": array of certification names

RESUME TEXT:
{text}

Return ONLY valid JSON, no markdown code blocks, no explanation."""


def _build_correction_prompt(text: str, existing: dict) -> str:
    return f"""I have extracted resume metadata using regex but it may contain errors. Review the resume text and correct the data.

CURRENT EXTRACTED DATA:
- Name: {existing.get('candidate_name', 'N/A')}
- Designation: {existing.get('current_designation', 'N/A')}
- Experience: {existing.get('experience_years', 'N/A')} years
- Location: {existing.get('location', 'N/A')}
- Skills: {json.dumps(existing.get('skills', [])[:15])}

RESUME TEXT:
{text}

Return ONLY a JSON object with these corrected fields:
- "candidate_name": real person name (NOT company, NOT "Foundit X Profile", NOT section header)
- "current_designation": primary job title only (max 60 chars)
- "experience_years": number or null
- "location": city name only
- "skills": array of TECHNICAL skills only (NO dates, NO cities, NO education, NO hobbies, NO soft skills). Max 30 items.
- "education": array of education strings
- "certifications": array

Return ONLY valid JSON, no markdown, no explanation."""


def _parse_llm_response(text: str) -> dict | None:
    """Parse the LLM JSON response, handling common formatting issues."""
    # Strip markdown code blocks
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())

    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return None

        # Validate and normalize
        result = {}
        if data.get("candidate_name"):
            name = str(data["candidate_name"]).strip()
            if 2 < len(name) < 60:
                result["candidate_name"] = name

        if data.get("phone"):
            result["phone"] = str(data["phone"]).strip()

        if data.get("current_designation"):
            desig = str(data["current_designation"]).strip()
            if len(desig) > 60:
                desig = desig[:57] + "..."
            result["current_designation"] = desig

        if data.get("experience_years") is not None:
            try:
                years = float(data["experience_years"])
                if 0 < years <= 50:
                    result["experience_years"] = years
            except (ValueError, TypeError):
                pass

        if data.get("location"):
            result["location"] = str(data["location"]).strip()

        if isinstance(data.get("skills"), list):
            result["skills"] = [str(s).strip() for s in data["skills"] if s and len(str(s).strip()) > 1][:40]

        if isinstance(data.get("education"), list):
            result["education"] = [str(e).strip() for e in data["education"] if e][:10]

        if isinstance(data.get("certifications"), list):
            result["certifications"] = [str(c).strip() for c in data["certifications"] if c][:10]

        return result if result else None

    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM JSON: %s", text[:200])
        return None


def needs_llm_correction(data: dict) -> bool:
    """Check if regex-parsed data looks suspicious and needs LLM correction."""
    name = data.get("candidate_name", "") or ""
    skills = data.get("skills", []) or []
    desig = data.get("current_designation", "") or ""

    # Bad name patterns
    name_lower = name.lower()
    if any(w in name_lower for w in ["foundit", "naukri", "profile", "indeed",
                                       "experience", "summary", "projects",
                                       "technologies", "solutions", "pvt", "ltd"]):
        return True
    if not name or len(name) < 3:
        return True

    # Skills contain obvious garbage
    garbage_count = 0
    for s in skills[:15]:
        s_lower = s.lower().strip()
        if re.match(r"^\d+$", s_lower):
            garbage_count += 1
        elif re.match(r"^\d{4}\s*[-–]", s_lower):
            garbage_count += 1
        elif "???" in s_lower:
            garbage_count += 1
    if garbage_count >= 2:
        return True

    # No skills at all
    if len(skills) < 2:
        return True

    # Very long designation
    if len(desig) > 80:
        return True

    return False
