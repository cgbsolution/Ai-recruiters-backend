"""
Fair-matching redaction: what a ranking prompt or an embedding may see.

Two layers:
1. ALLOWLIST. The input is assembled only from job-relevant structure — role
   titles, employers, role dates, skills with years, degree and field,
   certifications, summary. Nothing else from the profile is ever copied, so
   name, photo, date of birth, gender, marital status, religion, caste,
   address, nationality, contact details, salary and location cannot arrive
   by way of a field. Institutions and graduation years are left out too
   (proxies for age and background); location fit is decided by the SQL
   filter, not the model.
2. SCRUB. Free text inside allowed fields (role descriptions, summary) is
   cleaned of the candidate's own name, contact details, labelled personal
   data ("DOB: …", "Religion: …", "Category: OBC", "Father's name: …"),
   ages, honorifics, marital and gender words, and gendered pronouns.

build_rank_input() returns the payload plus `fields_sent` — the exact list of
field paths that reached the prompt — which is stored on the match and in
the audit log.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, List, Optional, Tuple

# Field paths the rank / embedding input may contain. Anything not listed here
# is never copied out of a profile.
ALLOWED_FIELDS = (
    "current_role",
    "current_employer",
    "industry",
    "total_experience_years",
    "skills[].name",
    "skills[].years",
    "skills[].last_used_year",
    "work_history[].title",
    "work_history[].company",
    "work_history[].start",
    "work_history[].end",
    "work_history[].description",
    "education[].degree_and_field",
    "certifications[]",
    "summary",
)

PROTECTED_ATTRIBUTES = ("name", "photo", "date_of_birth", "age", "gender", "marital_status", "religion", "caste",
                        "address", "nationality", "email", "phone")

_LABELS = (
    r"date\s+of\s+birth|d\.?\s?o\.?\s?b\.?|birth\s*date|born\s+on|age|gender|sex|marital\s+status|religion|"
    r"caste|sub[\s-]?caste|community|category|nationality|citizenship|father'?s\s+name|mother'?s\s+name|"
    r"husband'?s\s+name|spouse(?:'?s\s+name)?|passport(?:\s+(?:no|number))?|pan(?:\s+(?:no|number|card))?|"
    r"aadh?aa?r(?:\s+(?:no|number))?|permanent\s+address|present\s+address|current\s+address|address|"
    r"residence|hometown|languages?\s+known|blood\s+group|height|weight|photo(?:graph)?"
)
# "Label: value" up to the end of the clause: a line break, ; | • ) or the end
# of a sentence. Commas stay inside the value ("Address: 12, MG Road, Pune").
_LABELLED = re.compile(rf"(?i)\b(?:{_LABELS})\s*[:\-–=]\s*(?:[^\n;|•).]|\.(?!\s))*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d \-().]{8,}\d(?![\w-])")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b(?:linkedin|github|facebook|instagram|twitter)\.com/\S*")
_AGE = re.compile(r"(?i)\b(?:aged?\s*\d{2}|\d{2}\s*(?:years?|yrs?)\s*old|\(\s*\d{2}\s*(?:years?|yrs?)\s*\))")
_DATE_OF_BIRTH = re.compile(r"(?i)\bborn\b[^.\n;]*")
_HONORIFIC = re.compile(r"(?i)\b(?:mr|mrs|ms|miss|smt|shri|sri|kumari|km)\.?\s+(?=[A-Z])")
_STANDALONE = re.compile(
    r"(?i)\b(?:male|female|transgender|married|unmarried|divorced|widowed|widow|bachelor\s+status|"
    r"hindu|muslim|christian|sikh|jain|buddhist|parsi|"
    r"brahmin|dalit|obc|sc/st|scheduled\s+(?:caste|tribe)|general\s+category|ews\s+category|"
    r"\w+\s+(?:national|citizen))\b"
)
_PRONOUNS = (
    (re.compile(r"\b(?:he|she)\b", re.I), "they"),
    (re.compile(r"\b(?:him|her)\b(?=\s+(?:team|manager|role|work|own)\b)", re.I), "their"),
    (re.compile(r"\b(?:his|hers)\b", re.I), "their"),
    (re.compile(r"\b(?:him|her)\b", re.I), "them"),
    (re.compile(r"\b(?:himself|herself)\b", re.I), "themselves"),
)


_HONORIFIC_WORDS = {"mr", "mrs", "ms", "miss", "dr", "smt", "shri", "sri", "km"}


def name_tokens(name: Optional[str]) -> List[str]:
    """Every word of the candidate's name (longest first), honorifics excluded."""
    tokens = {t for t in re.findall(r"[A-Za-z]{2,}", name or "") if t.lower() not in _HONORIFIC_WORDS}
    return sorted(tokens, key=len, reverse=True)


def _name_pattern(token: str) -> re.Pattern:
    """Title-case or upper-case only: a name that is also a word ("Will",
    "Grace") must not erase "will" or "grace" from a sentence."""
    title, upper = token[:1].upper() + token[1:].lower(), token.upper()
    return re.compile(rf"\b(?:{re.escape(title)}|{re.escape(upper)})\b")


def scrub_text(text: Optional[str], names: Iterable[str] = (), counts: Optional[Counter] = None) -> Optional[str]:
    if not text:
        return text
    counts = counts if counts is not None else Counter()
    out = str(text)

    def sub(pattern: re.Pattern, replacement: str, kind: str):
        nonlocal out
        out, n = pattern.subn(replacement, out)
        if n:
            counts[kind] += n

    sub(_LABELLED, "", "labelled_personal_data")
    sub(_EMAIL, "", "email")
    sub(_URL, "", "url")
    sub(_PHONE, "", "phone")
    sub(_AGE, "", "age")
    sub(_DATE_OF_BIRTH, "", "date_of_birth")
    sub(_HONORIFIC, "", "honorific")
    for token in names:
        sub(_name_pattern(token), "the candidate", "name")
    sub(_STANDALONE, "", "protected_term")
    for pattern, replacement in _PRONOUNS:
        sub(pattern, replacement, "pronoun")
    out = re.sub(r"(?:the candidate\s*){2,}", "the candidate ", out)
    # Debris left where values were cut out: "()", ".;..", dangling separators.
    out = re.sub(r"\(\s*[,;:\-|]*\s*\)", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([,.;:|])", r"\1", out)
    out = re.sub(r"([,.;:|])(?:\s*[,.;:|])+", r"\1", out)
    out = re.sub(r"^[\s,.;:|]+|[\s,;:|]+$", "", out)
    return out.strip() or None


def _clean(value, names, counts, limit: int = 400):
    if value is None:
        return None
    return (scrub_text(str(value), names, counts) or "")[:limit] or None


def _ym(value) -> Optional[str]:
    if not value:
        return None
    m = re.match(r"^\s*(\d{4})(?:-(\d{1,2}))?", str(value))
    return f"{m.group(1)}-{int(m.group(2) or 1):02d}" if m else None


def _degree_and_field(entry) -> Optional[str]:
    """Degree and field only — never the institution or the year."""
    if isinstance(entry, dict):
        parts = [entry.get("degree"), entry.get("field")]
        return ", ".join(str(p) for p in parts if p) or None
    text = str(entry or "")
    text = re.sub(r"\(\s*\d{4}\s*\)|\b(19|20)\d{2}\b", "", text)
    first = text.split(",")[0].strip()  # "B.Tech CS, XYZ University" -> "B.Tech CS"
    return first or None


def build_rank_input(parsed: dict, candidate_name: Optional[str] = None) -> Tuple[dict, List[str], dict]:
    """(payload, fields_sent, redaction_counts) for one profile version."""
    names = name_tokens(candidate_name)
    counts: Counter = Counter()
    fields: set = set()

    def put(target: dict, key: str, value, path: str):
        if value not in (None, "", [], {}):
            target[key] = value
            fields.add(path)

    payload: dict = {}
    put(payload, "current_role", _clean(parsed.get("current_designation"), names, counts, 160), "current_role")
    put(payload, "current_employer", _clean(parsed.get("current_company"), names, counts, 160), "current_employer")
    put(payload, "industry", _clean(parsed.get("industry"), names, counts, 120), "industry")
    if isinstance(parsed.get("experience_years"), (int, float)):
        put(payload, "total_experience_years", round(float(parsed["experience_years"]), 1), "total_experience_years")

    skills = []
    for s in parsed.get("skills") or []:
        item = s if isinstance(s, dict) else {"name": s}
        name = _clean(item.get("name"), names, counts, 80)
        if not name:
            continue
        entry = {"name": name}
        fields.add("skills[].name")
        if isinstance(item.get("years"), (int, float)):
            entry["years"] = item["years"]
            fields.add("skills[].years")
        if isinstance(item.get("last_used_year"), int):
            entry["last_used_year"] = item["last_used_year"]
            fields.add("skills[].last_used_year")
        skills.append(entry)
    if skills:
        payload["skills"] = skills[:80]

    work = []
    for w in parsed.get("work_experience") or []:
        if not isinstance(w, dict):
            continue
        entry = {}
        for key, src, limit in (("title", "title", 160), ("company", "company", 160)):
            value = _clean(w.get(src), names, counts, limit)
            if value:
                entry[key] = value
                fields.add(f"work_history[].{key}")
        for key, src in (("start", "start_date"), ("end", "end_date")):
            value = _ym(w.get(src))
            if value:
                entry[key] = value
                fields.add(f"work_history[].{key}")
        if not w.get("end_date") and entry.get("start"):
            entry["end"] = "present"
        description = _clean(w.get("description"), names, counts, 1200)
        if description:
            entry["description"] = description
            fields.add("work_history[].description")
        if entry:
            work.append(entry)
    if work:
        payload["work_history"] = work[:20]

    education = [d for d in (_degree_and_field(e) for e in (parsed.get("education_detail") or parsed.get("education") or [])) if d]
    education = [scrub_text(d, names, counts) for d in education]
    if any(education):
        payload["education"] = [{"degree_and_field": d} for d in education if d][:6]
        fields.add("education[].degree_and_field")
    certs = [c for c in (_clean(c, names, counts, 160) for c in parsed.get("certifications") or []) if c]
    if certs:
        payload["certifications"] = certs[:20]
        fields.add("certifications[]")
    put(payload, "summary", _clean(parsed.get("summary"), names, counts, 1500), "summary")

    assert fields <= set(ALLOWED_FIELDS), f"field outside the allowlist: {fields - set(ALLOWED_FIELDS)}"
    return payload, sorted(fields), dict(counts)


def embedding_text(parsed: dict, candidate_name: Optional[str] = None) -> str:
    """Plain text for the profile embedding, from the same redacted payload."""
    payload, _, _ = build_rank_input(parsed, candidate_name)
    lines = []
    if payload.get("current_role"):
        lines.append(f"Current role: {payload['current_role']}" + (
            f" at {payload['current_employer']}" if payload.get("current_employer") else ""))
    if payload.get("industry"):
        lines.append(f"Industry: {payload['industry']}")
    if payload.get("total_experience_years") is not None:
        lines.append(f"Experience: {payload['total_experience_years']} years")
    if payload.get("skills"):
        lines.append("Skills: " + ", ".join(s["name"] for s in payload["skills"]))
    for w in payload.get("work_history", []):
        lines.append(" ".join(x for x in (w.get("title"), "at" if w.get("company") else None, w.get("company")) if x)
                     + (f": {w['description']}" if w.get("description") else ""))
    for e in payload.get("education", []):
        lines.append(f"Education: {e['degree_and_field']}")
    if payload.get("certifications"):
        lines.append("Certifications: " + ", ".join(payload["certifications"]))
    if payload.get("summary"):
        lines.append(payload["summary"])
    return "\n".join(lines)[:8000]
