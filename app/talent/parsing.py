"""
CV parse with Claude (structured output) → the cv_versions.parsed schema.

Total experience is computed here from the work-history dates (overlapping
roles counted once), not taken from the model or from the CV's own claim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Tuple

import anthropic

from app.talent import claude, llm
from app.talent.config import talent_config
from app.talent.prompts import CV_PARSE_SCHEMA, CV_PARSE_SYSTEM, CV_PARSE_VERSION

logger = logging.getLogger("app.talent.parsing")

MAX_CV_CHARS = 60_000
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class ParseResult:
    parsed: dict
    field_confidence: dict
    confidence: float
    model: str
    prompt_version: str
    changes_summary: List[str]


def _ym(value: Optional[str]) -> Optional[Tuple[int, int]]:
    if not value:
        return None
    try:
        parts = str(value).strip()[:7].split("-")
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        if 1950 <= year <= 2100 and 1 <= month <= 12:
            return year, month
    except (ValueError, IndexError):
        pass
    return None


def total_experience_years(work_history: list, today: Optional[date] = None) -> Optional[float]:
    """Union of role date ranges, in years. Roles without a parseable start are ignored."""
    today = today or date.today()
    spans = []
    for role in work_history or []:
        start = _ym(role.get("start"))
        if start is None:
            continue
        end = _ym(role.get("end")) if role.get("end") else (today.year, today.month)
        if end is None:
            continue
        a, b = start[0] * 12 + start[1] - 1, end[0] * 12 + end[1]
        if b > a:
            spans.append((a, b))
    if not spans:
        return None
    spans.sort()
    months, (cur_a, cur_b) = 0, spans[0]
    for a, b in spans[1:]:
        if a <= cur_b:
            cur_b = max(cur_b, b)
        else:
            months += cur_b - cur_a
            cur_a, cur_b = a, b
    months += cur_b - cur_a
    return round(months / 12, 1)


def _label(ym: Optional[Tuple[int, int]]) -> Optional[str]:
    return f"{_MONTHS[ym[1] - 1]} {ym[0]}" if ym else None


def to_version_schema(data: dict, today: Optional[date] = None) -> dict:
    """Model output → the structure stored in cv_versions.parsed / applied to resume_metadata."""
    work = []
    for role in data.get("work_history") or []:
        start, end = _ym(role.get("start")), _ym(role.get("end"))
        current = bool(role.get("is_current")) or not role.get("end")
        duration = None
        if start:
            duration = f"{_label(start)} - {'Present' if current else (_label(end) or '')}".strip(" -")
        work.append({"title": role.get("title"), "company": role.get("company"), "start_date": role.get("start"),
                     "end_date": None if current else role.get("end"), "duration": duration,
                     "description": role.get("summary")})
    education = []
    for e in data.get("education") or []:
        text = ", ".join(str(x) for x in (e.get("degree"), e.get("field"), e.get("institution")) if x)
        if e.get("year"):
            text = f"{text} ({e['year']})" if text else str(e["year"])
        if text:
            education.append(text)
    stated = data.get("stated") or {}
    identity = data.get("identity") or {}
    return {
        "current_designation": data.get("current_role"),
        "current_company": data.get("current_employer"),
        "industry": data.get("industry"),
        "experience_years": total_experience_years(data.get("work_history") or [], today),
        "skills": [{"name": s["name"].strip(), "years": s.get("years"), "last_used_year": s.get("last_used_year")}
                   for s in data.get("skills") or [] if s.get("name") and s["name"].strip()],
        "work_experience": work,
        "education": education,
        "education_detail": data.get("education") or [],
        "certifications": [c for c in data.get("certifications") or [] if c],
        "location": identity.get("location"),
        "summary": data.get("summary"),
        "current_ctc": stated.get("current_ctc_lpa"),
        "expected_ctc": stated.get("expected_ctc_lpa"),
        "notice_period_days": stated.get("notice_period_days"),
    }


def _clamp01(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def user_content(text: str, previous: Optional[dict]) -> str:
    parts = [claude.untrusted("untrusted_cv", text[:MAX_CV_CHARS])]
    if previous:
        slim = {k: previous.get(k) for k in ("current_designation", "current_company", "skills", "work_experience",
                                             "location", "certifications")}
        parts.append(claude.untrusted("untrusted_previous_version", slim))
    parts.append("Extract the CV above.")
    return claude.joined(parts)


def _blank_to_none(value):
    """Text the schema returns as "" when the CV doesn't state it → None, as the
    rest of the pipeline expects; empty strings in plain lists are dropped."""
    if isinstance(value, dict):
        return {k: _blank_to_none(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_blank_to_none(v) for v in value if not (isinstance(v, str) and not v.strip())]
    if isinstance(value, str) and not value.strip():
        return None
    return value


def from_result(result: llm.Result) -> Optional[ParseResult]:
    if result.refused or not result.data:
        return None
    data = _blank_to_none(result.data)
    confidence = {k: _clamp01(v) for k, v in (data.get("field_confidence") or {}).items()}
    overall = round(sum(confidence.values()) / len(confidence), 3) if confidence else 0.0
    return ParseResult(parsed=to_version_schema(data), field_confidence=confidence, confidence=overall,
                       model=result.model, prompt_version=result.prompt_version,
                       changes_summary=list(data.get("changes_summary") or []))


async def parse_cv(text: str, previous: Optional[dict] = None, profile_id: Optional[int] = None,
                   organization_id: Optional[int] = None) -> Optional[ParseResult]:
    """None when the AI isn't configured, can't be used or declined — callers fall back to the regex parse."""
    if not llm.available():
        return None
    try:
        result = await llm.structured(
            feature="talent_cv_parse", system=CV_PARSE_SYSTEM, user=user_content(text, previous),
            schema=CV_PARSE_SCHEMA, model=talent_config.TALENT_PARSE_MODEL, effort=talent_config.TALENT_PARSE_EFFORT,
            prompt_version=CV_PARSE_VERSION, max_tokens=8000, organization_id=organization_id,
        )
    except llm.Unavailable:
        return None
    except anthropic.BadRequestError as exc:
        # A request the API will never accept (e.g. a schema limit): retrying can't
        # help, so fall back to the regex parse instead of leaving the CV unread.
        logger.error("Claude CV parse refused the request (%s); using the regex parse", exc.request_id or "no id")
        return None
    except Exception as exc:  # noqa: BLE001
        problem = llm.account_problem_from(exc)
        if problem is None:
            raise  # transient: the step is retried
        logger.error("CV parse can't use the AI (%s); using the regex parse", problem)
        return None
    parsed = from_result(result)
    if parsed is not None and parsed.changes_summary:
        parsed.parsed["changes_summary"] = parsed.changes_summary
    return parsed
