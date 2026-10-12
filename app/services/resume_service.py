"""
Resume parsing and fit-score calculation.

PDF text extraction uses pdfplumber (PDFium for damaged files); DOCX is read from
its XML parts; legacy .doc through olefile.
LLM calls are delegated to ai_service helpers (provider-agnostic).

Uses an enterprise-grade ATS engine prompt for deterministic,
production-quality resume parsing and JD fit scoring.
"""

import json
import logging
import os
import re
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

# Hard ceilings for untrusted documents. A resume or JD is a few pages and a
# few hundred KB of XML; anything far beyond is either broken or an attack. A
# 200 KB .docx "zip bomb" inflated to 463 MB inside python-docx (which reads
# every part into memory), and a 10 MB one would take the whole single-worker
# server — and every live interview — down with it.
MAX_PDF_PAGES = 60
MAX_DOCX_UNCOMPRESSED_BYTES = 60 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 200


def check_docx_is_safe(file_path: str) -> None:
    """Raise RuntimeError for a .docx whose parts would inflate to an unsafe
    size. Reads only the zip directory, never the part contents."""
    import zipfile

    try:
        with zipfile.ZipFile(file_path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        raise RuntimeError("This file is not a valid DOCX document.")
    total = sum(max(0, i.file_size) for i in infos)
    packed = sum(max(0, i.compress_size) for i in infos) or 1
    if total > MAX_DOCX_UNCOMPRESSED_BYTES or (
        total > 10 * 1024 * 1024 and total / packed > MAX_DOCX_COMPRESSION_RATIO
    ):
        raise RuntimeError("This DOCX file is too large to process. Please upload a smaller document or a PDF.")


def extract_text_from_pdf(file_path: str) -> str:
    """Extract plain text from a PDF file using pdfplumber.

    Uses dedupe_chars(tolerance=1) to strip doubled characters produced by
    Canva / InDesign / scanner PDF artefacts before extracting text.
    Falls back to raw extraction on pages where deduplication fails.
    Raises RuntimeError with a human-readable message when the PDF yields no
    text at all (e.g. image-only / purely scanned PDFs).
    """
    try:
        result = _pdfplumber_text(file_path)
        if not result:
            result = _pdf_text_pdfium(file_path) or ""
        if not result:
            raise RuntimeError(
                "No text could be extracted from this PDF. "
                "It may be a scanned / image-only document. "
                "Please upload a text-based PDF or a DOCX file."
            )
        return result
    except RuntimeError:
        raise
    except ImportError:
        raise RuntimeError("pdfplumber is not installed. Run: pip install pdfplumber")
    except Exception as exc:
        # Damaged files ("Unexpected EOF" from incomplete downloads, "No /Root
        # object"): let qpdf rebuild the structure, else try PDFium.
        recovered = _pdf_text_repaired(file_path) or _pdf_text_pdfium(file_path)
        if recovered:
            logger.info("PDF read after repair (pdfminer failed: %s): %s", exc, file_path)
            return recovered
        # These messages reach API responses; the parser's own text (which
        # includes server file paths) goes to the log only.
        logger.warning("PDF extraction failed for %s: %s", file_path, exc)
        raise RuntimeError("Could not read this PDF file. It may be damaged or password-protected.") from exc


def _pdfplumber_text(source) -> str:
    """Text of a PDF (path or file object) via pdfplumber, page by page."""
    import pdfplumber

    text_parts = []
    with pdfplumber.open(source) as pdf:
        for page in pdf.pages[:MAX_PDF_PAGES]:
            try:
                page_text = page.dedupe_chars(tolerance=1).extract_text() or ""
            except Exception:
                page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(page_text)
    return "\n".join(text_parts).strip().replace("\x00", "")


def _pdf_text_repaired(file_path: str) -> Optional[str]:
    """qpdf (via pikepdf) reconstructs a PDF's cross-reference table and
    trailer by scanning the file — it recovers truncated downloads that both
    pdfminer and PDFium reject. The repaired copy is read in memory; the stored
    file is left as it is."""
    import io

    try:
        import pikepdf

        with pikepdf.open(file_path) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES * 4:
                return None
            repaired = io.BytesIO()
            pdf.save(repaired)
        repaired.seek(0)
        return _pdfplumber_text(repaired) or None
    except Exception:
        return None


def _pdf_text_pdfium(file_path: str) -> Optional[str]:
    """Last reader for PDFs the others give up on. PDFium (Chrome's PDF
    engine) tolerates other kinds of damage. None if it can't."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(file_path)
        try:
            parts = []
            for i in range(min(len(pdf), MAX_PDF_PAGES)):
                page = pdf[i]
                textpage = page.get_textpage()
                parts.append(textpage.get_text_range())
                textpage.close()
                page.close()
        finally:
            pdf.close()
    except Exception:
        return None
    text = "\n".join(parts).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return _tidy_lines(text) or None


def extract_text_from_docx(file_path: str) -> str:
    """Text of a .docx: body, tables, text boxes, headers and footers.

    Reads the WordprocessingML parts directly. python-docx (used before) only
    returned top-level body paragraphs — dropping every table-based resume
    layout and the header with the name/contact block — and failed the whole
    file when any part, even an embedded picture, had a bad checksum ("Bad
    CRC-32 for file 'word/media/image1.png'" on foundit exports).
    """
    import zipfile

    check_docx_is_safe(file_path)
    try:
        with zipfile.ZipFile(file_path) as zf:
            names = set(zf.namelist())
            main = _docx_main_part(zf, names)
            if main is None:
                raise RuntimeError("Could not read this Word file. Please upload a .docx or PDF.")
            headers = sorted(n for n in names if re.fullmatch(r"word/header\d*\.xml", n))
            footers = sorted(n for n in names if re.fullmatch(r"word/footer\d*\.xml", n))
            chunks = []
            for part in headers + [main] + footers:
                try:
                    chunks.append(_wordml_text(zf.read(part)))
                except Exception:
                    if part == main:
                        raise
                    # a damaged header/footer costs only that part
        text = "\n".join(c for c in chunks if c.strip())
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("DOCX extraction failed for %s: %s", file_path, exc)
        raise RuntimeError("Could not read this Word file. Please upload a .docx or PDF.") from exc
    return _tidy_lines(text)


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _docx_main_part(zf, names: set) -> Optional[str]:
    """The main document part named by the package relationships (it is not
    always word/document.xml)."""
    try:
        rels = zf.read("_rels/.rels").decode("utf-8", errors="replace")
        m = re.search(r'Target="/?([^"]+)"[^>]*Type="[^"]*/officeDocument"|'
                      r'Type="[^"]*/officeDocument"[^>]*Target="/?([^"]+)"', rels)
        if m:
            target = m.group(1) or m.group(2)
            if target in names:
                return target
    except KeyError:
        pass
    return "word/document.xml" if "word/document.xml" in names else None


def _wordml_text(xml: bytes) -> str:
    """Paragraph text of one WordprocessingML part, in document order. Text
    boxes are read once (their mc:Fallback copy is skipped); deleted text in
    tracked changes and field codes are not text."""
    from lxml import etree

    # No entity expansion / network: the XML comes from an untrusted upload.
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(xml, parser)
    w = "{%s}" % _W_NS
    fallback = "{%s}Fallback" % _MC_NS
    lines, stack, skip = [], [], 0
    for event, el in etree.iterwalk(root, events=("start", "end")):
        tag = el.tag
        if tag == fallback:
            skip += 1 if event == "start" else -1
            continue
        if skip:
            continue
        if tag == w + "p":
            if event == "start":
                stack.append([])
            elif stack:
                line = "".join(stack.pop()).strip()
                if line:
                    lines.append(line)
        elif event == "start" and stack:
            if tag == w + "t":
                stack[-1].append(el.text or "")
            elif tag == w + "tab":
                stack[-1].append("\t")
            elif tag in (w + "br", w + "cr"):
                stack[-1].append("\n")
    return "\n".join(lines)


def extract_text_from_doc(file_path: str) -> str:
    """Text of a legacy Word 97-2003 binary .doc.

    These were failing with "not a Word file, content type is ...themeManager+xml":
    Word 2007+ embeds the document theme as a small zip package inside the
    binary file, Python's zipfile finds that, and the DOCX reader then opened
    the theme instead of the document. The text is read from the WordDocument
    stream through its piece table ([MS-DOC] 2.8 / 2.9), using olefile.
    """
    import olefile

    unreadable = "Could not read this Word (.doc) file. It may be damaged or from a very old Word version."
    try:
        with olefile.OleFileIO(file_path) as ole:
            if not ole.exists("WordDocument"):
                raise RuntimeError(unreadable)
            word = ole.openstream("WordDocument").read()
            if len(word) < 0x200 or _u16(word, 0) != 0xA5EC:  # FIB magic
                raise RuntimeError(unreadable)
            flags = _u16(word, 0x0A)
            if flags & 0x0100:  # fEncrypted
                raise RuntimeError("This Word file is password-protected. Please upload an unprotected copy or a PDF.")
            table_name = "1Table" if flags & 0x0200 else "0Table"  # fWhichTblStm
            if not ole.exists(table_name):
                raise RuntimeError(unreadable)
            table = ole.openstream(table_name).read()

        # FIB: FibBase(32) | csw + fibRgW | cslw + fibRgLw | cbRgFcLcb + fibRgFcLcb
        pos = 32
        pos += 2 + _u16(word, pos) * 2
        pos += 2 + _u16(word, pos) * 4
        if _u16(word, pos) < 34:  # too old to carry fcClx (Word 6/95)
            raise RuntimeError(unreadable)
        pos += 2
        fc_clx, lcb_clx = _u32(word, pos + 33 * 8), _u32(word, pos + 33 * 8 + 4)
        clx = table[fc_clx:fc_clx + lcb_clx]

        i = 0
        while i < len(clx) and clx[i] == 0x01:  # Prc entries (formatting) — skip
            i += 3 + _u16(clx, i + 1)
        if i >= len(clx) or clx[i] != 0x02:  # Pcdt
            raise RuntimeError(unreadable)
        lcb = _u32(clx, i + 1)
        plc = clx[i + 5:i + 5 + lcb]
        n = (lcb - 4) // 12
        cps = [_u32(plc, 4 * k) for k in range(n + 1)]
        parts = []
        for k in range(n):
            chars = cps[k + 1] - cps[k]
            if chars <= 0 or chars > 2_000_000:
                continue
            fc = _u32(plc, 4 * (n + 1) + 8 * k + 2)
            if fc & 0x40000000:  # compressed: 8-bit cp1252 at fc/2
                start = (fc & 0x3FFFFFFF) // 2
                parts.append(word[start:start + chars].decode("cp1252", errors="replace"))
            else:  # UTF-16LE
                parts.append(word[fc:fc + 2 * chars].decode("utf-16-le", errors="replace"))
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("DOC extraction failed for %s: %s", file_path, exc)
        raise RuntimeError(unreadable) from exc
    return _tidy_lines(_strip_word_controls("".join(parts)))


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "little")


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little")


_WORD_CONTROL_MAP = {
    "\r": "\n", "\x0b": "\n", "\x0c": "\n", "\x0e": "\n",  # paragraph / line / page / column breaks
    "\x07": "\t",  # table cell and row ends
    "\x1e": "-", "\xa0": " ",
}


def _strip_word_controls(text: str) -> str:
    """Binary Word text -> plain text: keep each field's displayed result,
    drop its instruction ("HYPERLINK mailto:..."), map breaks, drop anchors."""
    out, fields = [], []  # fields: True while in a field's instruction part
    for ch in text:
        if ch == "\x13":
            fields.append(True)
        elif ch == "\x14":
            if fields:
                fields[-1] = False
        elif ch == "\x15":
            if fields:
                fields.pop()
        elif any(fields):
            continue
        elif ch in _WORD_CONTROL_MAP:
            out.append(_WORD_CONTROL_MAP[ch])
        elif ch == "\n" or ch == "\t" or ord(ch) >= 0x20:
            out.append(ch)
    return "".join(out)


def _tidy_lines(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.replace("\x00", "").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _sniff_kind(file_path: str) -> Optional[str]:
    """What the file really is, from its first bytes — extensions lie (a .doc
    that is really a .docx, a .docx saved as Word 97-2003...)."""
    try:
        with open(file_path, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        return None
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "doc"
    if head.startswith(b"PK\x03\x04"):
        return "docx"
    if b"%PDF" in head:  # the header may follow a few junk bytes
        return "pdf"
    return None


def extract_text(file_path: str) -> str:
    """Dispatch on what the file really is (its first bytes), falling back to
    the extension."""
    kind = _sniff_kind(file_path)
    ext = Path(file_path).suffix.lower()
    if kind is None:
        kind = {".pdf": "pdf", ".docx": "docx", ".doc": "doc"}.get(ext)
    if kind == "pdf":
        return extract_text_from_pdf(file_path)
    if kind == "docx":
        return extract_text_from_docx(file_path)
    if kind == "doc":
        return extract_text_from_doc(file_path)
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

# Weights for the five LLM-scored dimensions. Sum = 1.00.
LLM_DIMENSION_WEIGHTS = {
    "experience_relevance": 0.25,
    "jd_requirement_match": 0.25,
    "skills_and_tools": 0.25,
    "consulting_alignment": 0.15,  # domain/industry alignment
    "seniority_fit": 0.10,
}


def _composite_from_breakdown(score_breakdown: dict) -> Optional[int]:
    """Weighted 0-100 composite of the five dimension scores.

    Returns None if the model gave us nothing usable, so the caller can fall
    back. Missing individual dimensions are dropped and the remaining weights
    renormalised, so a partial breakdown still produces a sensible number
    instead of being silently penalised toward zero.
    """
    if not isinstance(score_breakdown, dict):
        return None

    total_weight = 0.0
    weighted = 0.0
    for key, weight in LLM_DIMENSION_WEIGHTS.items():
        val = score_breakdown.get(key)
        if val is None:
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        weighted += max(0.0, min(100.0, num)) * weight
        total_weight += weight

    if total_weight <= 0:
        return None
    return max(0, min(100, int(round(weighted / total_weight))))


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
- Core domain experience matters most. Transferable skills ALWAYS count
  (US accounting → Canadian accounting is transferable).
- Similar tools count. QuickBooks experience covers a QBO requirement.
- Missing 1-3 nice-to-have skills is NORMAL and trainable — penalise those
  lightly. Penalise CRITICAL missing requirements heavily.

HOW TO SCORE — do NOT pick an overall percentage directly.
Score each of the five dimensions below independently on a 0-100 scale. The
overall percentage is computed from them afterwards, so your ONLY job is to get
each dimension right on its own terms.

  experience_relevance  — years AND relevance of experience vs the requirement.
                          At/above the range with on-point work = 90-100.
                          Right years but adjacent work = 50-70.
                          Well short of the range = under 40.
  jd_requirement_match  — how many of the JD's actual responsibilities this
                          person has demonstrably OWNED (not merely assisted).
  skills_and_tools      — coverage of the required skills/tools, counting
                          genuine equivalents.
  consulting_alignment  — domain/industry alignment: has this person worked in
                          this business context before?
  seniority_fit         — is this the right LEVEL (ownership, scope, autonomy),
                          not too junior and not overqualified?

DISCRIMINATION IS THE POINT. Two candidates are almost never equally good. If
you find yourself giving the same dimension score to two profiles, look harder
for what separates them: depth of ownership, number of entities/clients handled,
whether they LED or merely supported, breadth of the tool stack, recency.

Use the FULL 0-100 range on every dimension and use precise values — 73, 81, 46,
not just 70, 80, 45. Do not round to multiples of 5.

Return ONLY valid JSON ("fit_percentage" is ignored — the server recomputes it
from score_breakdown, so spend your effort on the five dimension scores):
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

CRITICAL: Keep reasoning_points SHORT (4-6 bullets, each under 20 words). Keep summary to ONE sentence.
Every one of the five score_breakdown values MUST be filled with a precise integer.
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

    # ── Derive the percentage from the five dimensions, don't take the model's ──
    # An LLM asked for a single overall percentage is strongly MODAL: given a
    # band it emits that band's canonical value, so every plausible in-domain
    # applicant to the same job came back 82 (and every mediocre one 58). Real
    # applicant pools are full of *similar* candidates, which is exactly where
    # that collapse bites — the scores stopped ranking anyone.
    #
    # Scoring five independent dimensions and computing the weighted sum here
    # fixes it: each dimension is an easier, more grounded judgement, and the
    # arithmetic spreads the results out on its own. `fit_percentage` from the
    # model is now only a fallback for when score_breakdown is missing.
    score_breakdown = data.get("score_breakdown") or {}
    composite = _composite_from_breakdown(score_breakdown)

    if composite is not None:
        fit_percentage = composite
    else:
        raw_pct = data.get("fit_percentage", 0)
        try:
            fit_percentage = max(0, min(100, int(raw_pct)))
        except (TypeError, ValueError):
            fit_percentage = 0
        logger.warning("Fit score: score_breakdown missing/unusable, fell back to the model's fit_percentage")

    breakdown_data = data.get("breakdown", {})
    # Derive the level from the final percentage rather than trusting the
    # model's own label — otherwise a recomputed percentage and a model-supplied
    # level can disagree (a 68% shown as "HIGH FIT"). Thresholds must match
    # FIT_LEVEL_HIGH/MEDIUM in smart_scoring_service and the frontend badge.
    fit_level = "high" if fit_percentage >= 70 else ("medium" if fit_percentage >= 50 else "low")

    return FitScoreResponse(
        fit_level=fit_level,
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
