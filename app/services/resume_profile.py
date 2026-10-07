"""
Derived, searchable facts about a ResumeMetadata row.

The Resume Database search page filters and counts on facts that are spread
over JSON columns and the raw resume text — current company, city, CTC,
notice period, highest education... Working them out per request for 10k+
resumes would be far too slow, so they are stored as plain columns:

- refreshed whenever a row is written (the before_flush hook in
  app.models.resume_metadata calls derive_profile), and
- filled in for rows written before a column existed by backfill_profiles()
  at startup (bump PROFILE_VERSION after changing an extractor).

Everything here is extraction of what the resume actually says. Nothing is
guessed: a fact the resume does not state stays NULL ("Not disclosed").
"""

import json
import logging
import re
import time
from datetime import date
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

PROFILE_VERSION = 1


# ── Small helpers ────────────────────────────────────────────────────────────

def json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value or not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def clean_items(items: Iterable) -> List[str]:
    """Distinct (case-insensitive) non-empty strings, original casing kept."""
    seen, out = set(), []
    for item in items:
        if not isinstance(item, str):
            continue
        text = re.sub(r"\s+", " ", item).strip().strip("|").strip()
        if not text or len(text) > 80:
            continue
        key = text.lower()
        if key not in seen:
            seen.add(key)
            out.append(text)
    return out


def tag_index(items: Iterable) -> Optional[str]:
    """'|Python|Django|AWS|' — lets SQL match one tag exactly (ILIKE '%|aws|%')
    and split the column for facet counts, without parsing JSON per row."""
    values = [v.replace("|", "/") for v in clean_items(items)]
    return "|" + "|".join(values) + "|" if values else None


def work_entries(value) -> List[dict]:
    return [w for w in json_list(value) if isinstance(w, dict)]


# ── Location ─────────────────────────────────────────────────────────────────

_CITY_ALIASES = {
    "bangalore": "Bengaluru", "bengaluru": "Bengaluru", "bengalore": "Bengaluru",
    "gurgaon": "Gurugram", "gurugram": "Gurugram",
    "bombay": "Mumbai", "navi mumbai": "Navi Mumbai", "calcutta": "Kolkata",
    "madras": "Chennai", "new delhi": "Delhi", "delhi ncr": "Delhi NCR", "ncr": "Delhi NCR",
    "greater noida": "Greater Noida", "trivandrum": "Thiruvananthapuram",
    "poona": "Pune", "mysore": "Mysuru", "baroda": "Vadodara", "vizag": "Visakhapatnam",
}
_NOT_A_CITY = {"india", "remote", "na", "n/a", "-", "anywhere"}


def normalize_city(location: Optional[str]) -> Optional[str]:
    """'Noida, Uttar Pradesh, India' -> 'Noida'; 'bangalore' -> 'Bengaluru'."""
    if not location or not isinstance(location, str):
        return None
    for part in re.split(r"[,/|;]|\s-\s", location):
        part = re.sub(r"\s+", " ", part).strip(" .()").strip()
        key = part.lower()
        if not part or key in _NOT_A_CITY or len(part) > 40 or not re.search(r"[a-zA-Z]", part):
            continue
        return _CITY_ALIASES.get(key) or part.title()
    return None


# ── Education ────────────────────────────────────────────────────────────────

EDUCATION_LEVELS = ("doctorate", "postgraduate", "graduate", "diploma", "school")
_EDU_PATTERNS = (
    ("doctorate", re.compile(r"\b(ph\.?\s?d|doctorate|doctor of philosophy)\b", re.I)),
    ("postgraduate", re.compile(
        r"\b(m\.?\s?tech|m\.?\s?e\.?(?=[\s,(]|$)|mca|mba|pgdm|pgdba|m\.?\s?sc|m\.?\s?com|m\.?\s?a\.?(?=[\s,(]|$)|"
        r"m\.?\s?s\.?(?=[\s,(]|$)|master'?s?|post[\s-]?graduat\w*|pg\s?diploma|llm|m\.?\s?pharm|m\.?\s?arch|md\b|ms\b)", re.I)),
    ("graduate", re.compile(
        r"\b(b\.?\s?tech|b\.?\s?e\.?(?=[\s,(]|$)|bca|bba|bms|b\.?\s?sc|b\.?\s?com|b\.?\s?a\.?(?=[\s,(]|$)|"
        r"bachelor'?s?|graduat\w*|under[\s-]?graduat\w*|llb|b\.?\s?pharm|b\.?\s?arch|mbbs|bds|b\.?\s?ed)", re.I)),
    ("diploma", re.compile(r"\b(diploma|polytechnic|iti)\b", re.I)),
    ("school", re.compile(r"\b(12th|xii|hsc|higher secondary|intermediate|10th|ssc|matriculation|cbse|icse)\b", re.I)),
)


def highest_education(entries: Iterable) -> Optional[str]:
    found = set()
    for entry in entries:
        text = entry if isinstance(entry, str) else json.dumps(entry) if isinstance(entry, dict) else ""
        for level, pattern in _EDU_PATTERNS:
            if pattern.search(text):
                found.add(level)
    for level in EDUCATION_LEVELS:
        if level in found:
            return level
    return None


# ── Facts stated in the resume text ──────────────────────────────────────────

_NUM = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_CTC_LABEL = r"(?:ctc|salary|package|compensation|remuneration)"
_CURRENT_CTC = re.compile(
    r"(?:current|present|existing|last\s+drawn)\s*" + _CTC_LABEL + r"[^\d\n]{0,25}?" + _NUM, re.I)
_PLAIN_CTC = re.compile(r"(?<![a-z])ctc\s*[:\-–=]\s*(?:inr|rs\.?|₹)?\s*" + _NUM, re.I)
_EXPECTED_CTC = re.compile(
    r"(?:expected|expectation|desired)\s*(?:" + _CTC_LABEL + r")?[^\d\n]{0,25}?" + _NUM, re.I)


def _to_lpa(number: str, tail: str) -> Optional[float]:
    """Interpret a salary figure as lakhs per annum, or None when unclear."""
    try:
        n = float(number.replace(",", ""))
    except ValueError:
        return None
    t = tail.lower()
    monthly = re.search(r"per\s*month|/\s*month|/\s*m\b|p\.?\s?m\b|monthly|pm\b", t) is not None
    if re.match(r"\s*(lpa|lakhs?|lacs?|lakh|l\b|lp\b)", t):
        lpa = n
    elif re.match(r"\s*(cr|crores?)\b", t):
        lpa = n * 100
    elif re.match(r"\s*(k|thousand)\b", t):
        lpa = n * 1000 / 100000
    elif n >= 10000:
        lpa = n / 100000                      # plain rupees
    elif n < 200 and not monthly:
        lpa = n                               # "CTC: 12" — lakhs, by Indian convention
    else:
        return None                           # 200-9,999 with no unit: ambiguous
    if monthly:
        lpa *= 12
    return round(lpa, 2) if 0.3 <= lpa <= 500 else None


def _find_ctc(pattern: re.Pattern, text: str) -> Optional[float]:
    for m in pattern.finditer(text):
        value = _to_lpa(m.group(1), text[m.end(1): m.end(1) + 30])
        if value is not None:
            return value
    return None


def extract_ctc(text: str, expected: bool = False) -> Optional[float]:
    if not text:
        return None
    if expected:
        return _find_ctc(_EXPECTED_CTC, text)
    return _find_ctc(_CURRENT_CTC, text) or _find_ctc(_PLAIN_CTC, text)


_NOTICE = re.compile(
    r"notice(?:\s*period)?\s*(?:of)?\s*[:\-–=]?\s*(?:is\s*)?"
    r"(immediate(?:ly)?|(\d{1,3})\s*(days?|d\b|months?|mon(?:th)?s?\b|weeks?|wks?))", re.I)
_IMMEDIATE = re.compile(
    r"\b(immediate\s*joiner|join(?:ing)?\s*immediately|available\s*(?:to\s*join\s*)?immediately|"
    r"immediately\s*available)\b", re.I)


def extract_notice_days(text: str) -> Optional[int]:
    if not text:
        return None
    m = _NOTICE.search(text)
    if m:
        if m.group(1).lower().startswith("immediate"):
            return 0
        n = int(m.group(2))
        unit = m.group(3).lower()
        days = n * 30 if unit.startswith("m") else n * 7 if unit.startswith("w") else n
        return days if days <= 365 else None
    return 0 if _IMMEDIATE.search(text) else None


_RELOCATE_NO = re.compile(
    r"\b(?:not|un)\s*willing\s*to\s*relocat|\bnot\s*open\s*to\s*relocat|relocat\w*\s*[:\-–]\s*no\b", re.I)
_RELOCATE_YES = re.compile(
    r"\b(?:willing|open|ready|happy|flexible)\s*(?:to|for)\s*relocat|relocat\w*\s*[:\-–]\s*(?:yes|any)\b|"
    r"preferred\s*location\w*\s*[:\-–]\s*(?:anywhere|any\s*location|pan[\s-]*india)", re.I)


def extract_relocation(text: str) -> Optional[bool]:
    if not text:
        return None
    if _RELOCATE_NO.search(text):
        return False
    if _RELOCATE_YES.search(text):
        return True
    return None


# ── Work history ─────────────────────────────────────────────────────────────

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_DATE = re.compile(
    r"(?:\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[\s,'’-]*|\b(\d{1,2})\s*[/\-.]\s*)?"
    r"\b((?:19|20)\d{2})\b", re.I)
_PRESENT = re.compile(r"present|current|till\s*(?:date|now)|to\s*date|\bnow\b|ongoing|continuing", re.I)


def tenure_years(duration: Optional[str], today: Optional[date] = None) -> Optional[float]:
    """Years spent in a role from its 'Jan 2022 - Present' style duration."""
    if not duration or not isinstance(duration, str):
        return None
    dates = list(_DATE.finditer(duration))
    if not dates:
        return None

    def month_index(m) -> int:
        month = _MONTHS.get((m.group(1) or "")[:3].lower()) or (int(m.group(2)) if m.group(2) else 1)
        return int(m.group(3)) * 12 + max(1, min(12, month)) - 1

    start = month_index(dates[0])
    if _PRESENT.search(duration):
        today = today or date.today()
        end = today.year * 12 + today.month - 1
    elif len(dates) >= 2:
        end = month_index(dates[-1])
    else:
        return None
    months = end - start
    if months < 0 or months > 50 * 12:
        return None
    return round(months / 12, 1)


# ── Parse quality ────────────────────────────────────────────────────────────

def parse_quality(meta) -> int:
    """0-100: how much of a usable profile the parser got out of the resume."""
    if meta.parsing_status == "failed":
        return 0
    email = meta.candidate_email or ""
    skills = clean_items(json_list(meta.skills))
    score = 0
    score += 15 if meta.candidate_name else 0
    score += 15 if email and not email.endswith("@pending.local") else 0
    score += 10 if meta.phone else 0
    score += 10 if meta.current_designation else 0
    score += 10 if meta.experience_years is not None else 0
    score += 15 if len(skills) >= 5 else 8 if skills else 0
    score += 10 if json_list(meta.education) else 0
    score += 10 if work_entries(meta.work_experience) else 0
    score += 5 if meta.location else 0
    return score


# ── Search text ──────────────────────────────────────────────────────────────

def _search_text(meta, work: List[dict]) -> str:
    parts: List[str] = [
        meta.candidate_name or "",
        meta.current_designation or "",
        meta.location or "",
        meta.summary or "",
    ]
    for w in work[:15]:
        parts += [str(w.get("title") or ""), str(w.get("company") or "")]
    for column in (meta.skills, meta.certifications, meta.education, meta.keywords):
        parts += [str(v) for v in json_list(column) if isinstance(v, (str, int, float))]
    text = " | ".join(p for p in parts if p)
    return re.sub(r"\s+", " ", text).lower()[:20000]


# ── Entry points ─────────────────────────────────────────────────────────────

def derive_profile(meta, text_changed: bool = True) -> None:
    """Refresh the derived columns of `meta` (an ORM object) in place."""
    work = work_entries(meta.work_experience)
    meta.skills_index = tag_index(json_list(meta.skills))
    meta.certs_index = tag_index(json_list(meta.certifications))
    meta.current_company = (str(work[0].get("company") or "").strip()[:255] or None) if work else None
    meta.city = normalize_city(meta.location)
    meta.highest_education = highest_education(json_list(meta.education))
    meta.parse_quality = parse_quality(meta)
    meta.search_text = _search_text(meta, work) or None

    # Facts stated in the resume text are re-read only when the text changes
    # (or PROFILE_VERSION moves), and only overwrite with what the new text
    # states — an unrelated write such as a status change never clears them.
    if text_changed:
        text = meta.raw_text or ""
        for column, value in (
            ("current_ctc", extract_ctc(text)),
            ("expected_ctc", extract_ctc(text, expected=True)),
            ("notice_period_days", extract_notice_days(text)),
            ("open_to_relocate", extract_relocation(text)),
        ):
            if value is not None:
                setattr(meta, column, value)
    meta.derived_version = PROFILE_VERSION


def backfill_profiles(batch_size: int = 200) -> int:
    """Startup: derive the columns for rows stored before they existed (or
    before PROFILE_VERSION last changed). Also labels the source of rows that
    predate the `source` column. Returns the number of rows updated."""
    from sqlalchemy import or_

    from app.database import SessionLocal
    from app.models.candidate import Candidate
    from app.models.resume_metadata import ResumeMetadata

    updated = 0
    db = SessionLocal()
    try:
        linked = {
            rid for (rid,) in db.query(Candidate.resume_metadata_id)
            .filter(Candidate.resume_metadata_id.isnot(None)).distinct()
        }
        last_id = 0
        while True:
            rows = (
                db.query(ResumeMetadata)
                .filter(
                    ResumeMetadata.id > last_id,
                    or_(ResumeMetadata.derived_version.is_(None),
                        ResumeMetadata.derived_version < PROFILE_VERSION),
                    # Rows still queued are derived when their parse is saved;
                    # touching them here could race the bulk workers.
                    ResumeMetadata.parsing_status.notin_(("pending", "processing")),
                )
                .order_by(ResumeMetadata.id)
                .limit(batch_size)
                .all()
            )
            if not rows:
                break
            for meta in rows:
                derive_profile(meta, text_changed=True)
                if meta.source is None:
                    if meta.id in linked:
                        meta.source = "application"
                    elif meta.batch_id or meta.resume_file_path:
                        meta.source = "bulk_upload"
                    else:
                        meta.source = "import"
                last_id = meta.id
            db.commit()
            updated += len(rows)
            time.sleep(0.1)  # a one-off job: leave the CPU to live interviews between batches
        return updated
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
