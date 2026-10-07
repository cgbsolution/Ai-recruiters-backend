"""
Resume Database search: keyword query, filters, facet counts and relevance.

Query syntax (the search box): every word must match; "quoted words" match as
an exact phrase; a leading minus excludes (-intern). Words match whole words
anywhere in the profile — name, designation, companies, job titles, skills,
certifications, education, keywords, summary, location.

Facet counts are disjunctive, like job-board filters: each section's counts
apply every OTHER active filter, so ticking "Noida" does not zero out the
other cities.

Filtering runs in SQL on the derived columns kept by app.services.
resume_profile. Keyword matching and tag facets use PostgreSQL regex /
string_to_array; other dialects (the SQLite unit tests) fall back to LIKE and
counting in Python.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import Integer, and_, any_, bindparam, case, func, literal, nulls_last, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session, defer

from app.media import sign_media_url
from app.models.resume_metadata import ResumeMetadata as RM
from app.services.resume_profile import clean_items, json_list, tenure_years, work_entries

# Rows the Resume Database shows: everything except placeholder rows that
# never parsed. A parsed resume with no usable email keeps its placeholder
# email and IS listed (the UI shows "—").
NOT_UNPARSED_PLACEHOLDER = or_(
    ~RM.candidate_email.like("%@pending.local"),
    RM.parsing_status == "completed",
)

NA = "na"
CTC_BUCKETS = (
    ("0-3", "Up to 3 LPA", None, 3), ("3-6", "3 to 6 LPA", 3, 6), ("6-10", "6 to 10 LPA", 6, 10),
    ("10-15", "10 to 15 LPA", 10, 15), ("15-25", "15 to 25 LPA", 15, 25), ("25+", "25 LPA and above", 25, None),
)
NOTICE_BUCKETS = (
    ("0", "Immediate", 0, 0), ("15", "Up to 15 days", 1, 15), ("30", "30 days", 16, 30),
    ("60", "60 days", 31, 60), ("90", "90 days or more", 61, None),
)
EDUCATION = (
    ("doctorate", "Doctorate"), ("postgraduate", "Post-graduate"), ("graduate", "Graduate"),
    ("diploma", "Diploma"), ("school", "School (10th / 12th)"),
)
PIPELINE = (
    ("new", "New"), ("shortlisted", "Shortlisted"), ("contacted", "Contacted"),
    ("interviewing", "Interviewing"), ("on_hold", "On hold"), ("rejected", "Rejected"),
)
PIPELINE_STATUSES = tuple(k for k, _ in PIPELINE)
SOURCES = (("bulk_upload", "Bulk upload"), ("application", "Job application"), ("import", "Imported"))
QUALITY = ("high", "medium", "low")
SORTS = ("relevance", "recent", "experience_desc", "experience_asc", "ctc_desc")

_CTC = func.coalesce(RM.annual_package, RM.current_ctc)  # typed-in value wins over the extracted one
_FRESH = func.coalesce(RM.parsed_at, RM.created_at)


@dataclass
class SearchParams:
    q: str = ""
    location: str = ""
    exp_min: Optional[float] = None
    exp_max: Optional[float] = None
    ctc: List[str] = field(default_factory=list)
    notice: List[str] = field(default_factory=list)
    city: List[str] = field(default_factory=list)
    relocate: bool = False
    skills: List[str] = field(default_factory=list)
    skills_mode: str = "all"            # "all" | "any"
    certs: List[str] = field(default_factory=list)
    education: List[str] = field(default_factory=list)
    freshness_days: Optional[int] = None
    pipeline: List[str] = field(default_factory=list)
    source: List[str] = field(default_factory=list)
    quality: Optional[str] = None
    sort: str = "relevance"
    page: int = 1
    page_size: int = 20
    include_facets: bool = True


# ── Query text ───────────────────────────────────────────────────────────────

_TOKEN = re.compile(r'(-?)"([^"]*)"|(\S+)')
_SKIP_WORDS = {"and", "or", "not", "&&", "||"}


def parse_query(q: str) -> Tuple[List[str], List[str]]:
    """'OutSystems "tech lead" -intern' -> (['outsystems', 'tech lead'], ['intern'])."""
    include: List[str] = []
    exclude: List[str] = []
    for m in _TOKEN.finditer((q or "")[:500]):
        if m.group(3) is not None:
            raw = m.group(3)
            negative = raw.startswith("-") and len(raw) > 1
            term = raw[1:] if negative else raw
        else:
            negative, term = bool(m.group(1)), m.group(2)
        term = re.sub(r"\s+", " ", term).strip().strip('",;').strip().lower()
        if not re.search(r"[a-z0-9]", term) or term in _SKIP_WORDS:
            continue
        target = exclude if negative else include
        if term not in target:
            target.append(term[:60])
    return include[:10], exclude[:10]


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _word_regex(term: str) -> str:
    """Whole-word match that also works for c++, .net, node.js."""
    escaped = re.sub(r"([^a-z0-9 ])", r"\\\1", term).replace(" ", r"\s+")
    return r"(^|[^a-z0-9])" + escaped + r"($|[^a-z0-9])"


def _term_condition(term: str, pg: bool):
    if pg:
        return RM.search_text.op("~")(_word_regex(term))
    return RM.search_text.like(f"%{_like_escape(term)}%", escape="\\")


def _tag_condition(column, value: str):
    """Exact (case-insensitive) member of a '|a|b|' tag index."""
    return column.ilike("%|" + _like_escape(value.replace("|", "/")) + "|%", escape="\\")


# ── Filters ──────────────────────────────────────────────────────────────────

def _range(expr, lo, hi, inclusive_hi: bool):
    parts = []
    if lo is not None:
        parts.append(expr >= lo)
    if hi is not None:
        parts.append(expr <= hi if inclusive_hi else expr < hi)
    return and_(*parts)


def _bucket_condition(expr, buckets, key: str, inclusive_hi: bool):
    if key == NA:
        return expr.is_(None)
    for k, _, lo, hi in buckets:
        if k == key:
            return _range(expr, lo, hi, inclusive_hi)
    return None


def _bucket_expression(expr, buckets, inclusive_hi: bool):
    return case(*[(_range(expr, lo, hi, inclusive_hi), k) for k, _, lo, hi in buckets], else_=NA)


def _filter_conditions(p: SearchParams, now: datetime) -> Dict[str, object]:
    """One combined condition per filter section (sections AND together;
    values inside a section OR together, except "must have all" skills)."""
    f: Dict[str, object] = {}
    if p.exp_min is not None or p.exp_max is not None:
        f["experience"] = _range(RM.experience_years, p.exp_min, p.exp_max, inclusive_hi=True)
    if p.ctc:
        conds = [c for c in (_bucket_condition(_CTC, CTC_BUCKETS, k, False) for k in p.ctc) if c is not None]
        if conds:
            f["ctc"] = or_(*conds)
    if p.notice:
        conds = [c for c in (_bucket_condition(RM.notice_period_days, NOTICE_BUCKETS, k, True) for k in p.notice)
                 if c is not None]
        if conds:
            f["notice"] = or_(*conds)
    if p.city:
        f["city"] = RM.city.in_(p.city)
    if p.relocate:
        f["relocate"] = RM.open_to_relocate.is_(True)
    if p.skills:
        conds = [_tag_condition(RM.skills_index, s) for s in p.skills]
        f["skills"] = or_(*conds) if p.skills_mode == "any" else and_(*conds)
    if p.certs:
        f["certs"] = or_(*[_tag_condition(RM.certs_index, c) for c in p.certs])
    if p.education:
        levels = [e for e in p.education if e != NA]
        conds = [RM.highest_education.in_(levels)] if levels else []
        if NA in p.education:
            conds.append(RM.highest_education.is_(None))
        if conds:
            f["education"] = or_(*conds)
    if p.freshness_days:
        f["freshness"] = _FRESH >= now - timedelta(days=p.freshness_days)
    if p.pipeline:
        f["pipeline"] = RM.pipeline_status.in_(p.pipeline)
    if p.source:
        f["source"] = RM.source.in_(p.source)
    if p.quality == "high":
        f["quality"] = RM.parse_quality >= 80
    elif p.quality == "medium":
        f["quality"] = and_(RM.parse_quality >= 50, RM.parse_quality < 80)
    elif p.quality == "low":
        f["quality"] = or_(RM.parse_quality < 50, RM.parse_quality.is_(None), RM.parsing_status == "failed")
    return f


def _base_conditions(org_id: Optional[int], include: List[str], exclude: List[str],
                     location: str, pg: bool) -> list:
    conds = [NOT_UNPARSED_PLACEHOLDER]
    if org_id is not None:
        conds.append(RM.organization_id == org_id)
    conds += [_term_condition(t, pg) for t in include]
    # A row with no search text has nothing to exclude.
    conds += [or_(RM.search_text.is_(None), ~_term_condition(t, pg)) for t in exclude]
    places = [p.strip() for p in (location or "").split(",") if p.strip()][:5]
    if places:
        conds.append(or_(*[
            or_(RM.city.ilike(_like_escape(p) + "%", escape="\\"),
                RM.location.ilike("%" + _like_escape(p) + "%", escape="\\"))
            for p in places
        ]))
    return conds


# ── Relevance ────────────────────────────────────────────────────────────────

def _match_expression(include: List[str], p: SearchParams, pg: bool):
    """Match % = share of the searched-for things the profile has. A word
    counts fully when it is part of one of the candidate's skills or of their
    current designation, half when it only appears elsewhere in the profile.
    With "any of these" skills, each ticked skill the candidate has counts
    fully. None when nothing was searched for (every profile would score 100)."""
    parts = []
    for term in include:
        if pg:
            regex = _word_regex(term)
            strong = or_(RM.skills_index.op("~*")(regex), RM.current_designation.op("~*")(regex))
        else:
            pattern = "%" + _like_escape(term) + "%"
            strong = or_(RM.skills_index.ilike(pattern, escape="\\"),
                         RM.current_designation.ilike(pattern, escape="\\"))
        parts.append(case((strong, 1.0), else_=0.5))
    if p.skills_mode == "any":
        parts += [case((_tag_condition(RM.skills_index, s), 1.0), else_=0.0) for s in p.skills]
    if not parts:
        return None
    total = parts[0]
    for part in parts[1:]:
        total = total + part
    return total * 100.0 / len(parts)


def _order_by(p: SearchParams, match):
    recency = [_FRESH.desc(), RM.id.desc()]
    if p.sort == "recent":
        return recency
    if p.sort == "experience_desc":
        return [nulls_last(RM.experience_years.desc())] + recency
    if p.sort == "experience_asc":
        return [nulls_last(RM.experience_years.asc())] + recency
    if p.sort == "ctc_desc":
        return [nulls_last(_CTC.desc())] + recency
    first = [nulls_last(match.desc())] if match is not None else []
    return first + [nulls_last(RM.parse_quality.desc())] + recency


# ── Facets ───────────────────────────────────────────────────────────────────

def _group_counts(db: Session, expr, conds) -> Dict[str, int]:
    inner = select(expr.label("k")).where(*conds).subquery()
    return {k: n for k, n in db.execute(select(inner.c.k, func.count()).group_by(inner.c.k)).all()}


def _tag_counts(db: Session, column, conds, pg: bool, limit: int = 200) -> List[dict]:
    if pg:
        inner = (
            select(func.unnest(func.string_to_array(func.btrim(column, "|"), "|")).label("t"))
            .where(column.isnot(None), *conds)
            .subquery()
        )
        key = func.lower(inner.c.t)
        rows = db.execute(
            select(key.label("k"), func.min(inner.c.t), func.count())
            .group_by(key)
            .order_by(func.count().desc(), key)
            .limit(limit)
        ).all()
        return [{"key": k, "label": label, "count": n} for k, label, n in rows if k]
    counts: Dict[str, list] = {}
    for (value,) in db.execute(select(column).where(column.isnot(None), *conds)).all():
        for tag in value.strip("|").split("|"):
            if tag:
                entry = counts.setdefault(tag.lower(), [tag, 0])
                entry[1] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1][1], kv[0]))[:limit]
    return [{"key": k, "label": v[0], "count": v[1]} for k, v in ranked]


def _labelled(options, counts: Dict[str, int], with_na: Optional[str] = None) -> List[dict]:
    out = [{"key": k, "label": label, "count": counts.get(k, 0)} for k, label in options]
    if with_na:
        out.append({"key": NA, "label": with_na, "count": counts.get(NA, 0)})
    return out


def _facets(db: Session, base: list, filters: Dict[str, object], pg: bool) -> dict:
    def conds_without(name: str) -> list:
        return base + [c for k, c in filters.items() if k != name]

    ctc = _group_counts(db, _bucket_expression(_CTC, CTC_BUCKETS, False), conds_without("ctc"))
    notice = _group_counts(db, _bucket_expression(RM.notice_period_days, NOTICE_BUCKETS, True),
                           conds_without("notice"))
    cities = _group_counts(db, RM.city, conds_without("city"))
    education = _group_counts(db, func.coalesce(RM.highest_education, NA), conds_without("education"))
    pipeline = _group_counts(db, RM.pipeline_status, conds_without("pipeline"))
    source = _group_counts(db, func.coalesce(RM.source, NA), conds_without("source"))
    relocate = db.scalar(
        select(func.count()).select_from(RM).where(RM.open_to_relocate.is_(True), *conds_without("relocate"))
    ) or 0
    return {
        "ctc": [{"key": k, "label": label, "count": ctc.get(k, 0)} for k, label, _, _ in CTC_BUCKETS]
        + [{"key": NA, "label": "Not disclosed", "count": ctc.get(NA, 0)}],
        "notice": [{"key": k, "label": label, "count": notice.get(k, 0)} for k, label, _, _ in NOTICE_BUCKETS]
        + [{"key": NA, "label": "Not specified", "count": notice.get(NA, 0)}],
        "city": [
            {"key": c, "label": c, "count": n}
            for c, n in sorted(((c, n) for c, n in cities.items() if c), key=lambda kv: (-kv[1], kv[0]))[:150]
        ],
        "relocate": relocate,
        "skills": _tag_counts(db, RM.skills_index, conds_without("skills"), pg),
        "certs": _tag_counts(db, RM.certs_index, conds_without("certs"), pg, limit=100),
        "education": _labelled(EDUCATION, education, with_na="Not specified"),
        "pipeline": _labelled(PIPELINE, pipeline),
        "source": _labelled(SOURCES, source),
    }


# ── Result cards ─────────────────────────────────────────────────────────────

def _skill_matches(skill: str, terms: List[str]) -> bool:
    s = skill.lower()
    return any(t == s or re.search(r"(^|[^a-z0-9])" + re.escape(t) + r"($|[^a-z0-9])", s) for t in terms)


def card(meta: RM, match: Optional[float], include: List[str], selected_skills: List[str]) -> dict:
    work = work_entries(meta.work_experience)
    current_company = meta.current_company
    previous = next(
        (str(w.get("company")).strip() for w in work[1:]
         if str(w.get("company") or "").strip()
         and str(w.get("company")).strip().lower() != (current_company or "").lower()),
        None,
    )
    skills = clean_items(json_list(meta.skills))
    wanted = {s.lower() for s in selected_skills}
    matched = [s for s in skills if s.lower() in wanted or _skill_matches(s, include)]
    ordered = matched + [s for s in skills if s not in matched]
    education = [e for e in json_list(meta.education) if isinstance(e, str) and e.strip()]
    email = meta.candidate_email or ""
    fresh = meta.parsed_at or meta.created_at
    return {
        "id": meta.id,
        "candidate_name": meta.candidate_name,
        "candidate_email": None if email.endswith("@pending.local") else email,
        "phone": meta.phone,
        "current_designation": meta.current_designation,
        "current_company": current_company,
        "current_tenure_years": tenure_years(work[0].get("duration")) if work else None,
        "previous_company": previous,
        "experience_years": meta.experience_years,
        "current_ctc": meta.annual_package if meta.annual_package is not None else meta.current_ctc,
        "expected_ctc": meta.expected_ctc,
        "location": meta.location,
        "city": meta.city,
        "open_to_relocate": meta.open_to_relocate,
        "notice_period_days": meta.notice_period_days,
        "education": education[0] if education else None,
        "highest_education": meta.highest_education,
        "skills": ordered[:15],
        "skills_total": len(skills),
        "matched_skills": matched,
        "parse_quality": meta.parse_quality,
        "parsing_status": meta.parsing_status,
        "pipeline_status": meta.pipeline_status or "new",
        "source": meta.source,
        "resume_updated_at": fresh.isoformat() if fresh else None,
        "resume_file_url": sign_media_url(meta.resume_file_path),
        "match": round(match) if match is not None else None,
    }


# ── Entry point ──────────────────────────────────────────────────────────────

def search(db: Session, org_id: Optional[int], p: SearchParams) -> dict:
    pg = db.get_bind().dialect.name == "postgresql"
    include, exclude = parse_query(p.q)
    base = _base_conditions(org_id, include, exclude, p.location, pg)
    if pg and (include or exclude or p.location.strip()):
        # Keyword regexes scan every profile's text. Run them once and hand
        # the matching ids to the count, the page and the ~10 facet queries,
        # instead of re-scanning in each (~0.7 s -> ~0.2 s at 11k resumes).
        ids = list(db.scalars(select(RM.id).where(*base)))
        base = [RM.id == any_(bindparam("matched_ids", ids, type_=ARRAY(Integer)))]
    filters = _filter_conditions(p, datetime.utcnow())
    if pg:
        # Same for the tag filters (pattern match over every tag list): match
        # once, reuse the ids in the other sections' counts.
        for name in ("skills", "certs"):
            if name in filters:
                ids = list(db.scalars(select(RM.id).where(*base, filters[name])))
                filters[name] = RM.id == any_(bindparam(f"{name}_ids", ids, type_=ARRAY(Integer)))
    conds = base + list(filters.values())
    match = _match_expression(include, p, pg)

    total = db.scalar(select(func.count()).select_from(RM).where(*conds)) or 0
    stmt = (
        select(RM, (match if match is not None else literal(None)).label("match"))
        .where(*conds)
        .order_by(*_order_by(p, match))
        .offset((p.page - 1) * p.page_size)
        .limit(p.page_size)
        .options(defer(RM.raw_text), defer(RM.search_text))
    )
    items = [card(meta, score, include, p.skills) for meta, score in db.execute(stmt).all()]
    return {
        "items": items,
        "total": total,
        "page": p.page,
        "page_size": p.page_size,
        "total_pages": max(1, (total + p.page_size - 1) // p.page_size),
        "query": {"include": include, "exclude": exclude},
        "facets": _facets(db, base, filters, pg) if p.include_facets else None,
    }
