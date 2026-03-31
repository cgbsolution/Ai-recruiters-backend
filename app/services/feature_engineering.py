"""
Feature Engineering for Resume–Job Matching.

Extracts a 12-dimension feature vector from a (resume, job) pair.
All features are normalized to 0.0–1.0.  Used by both the algorithmic
scorer and the ML model.

Dimensions:
  0  f_skills_overlap      — fuzzy skill match ratio
  1  f_skills_tfidf        — TF-IDF cosine similarity of skills text
  2  f_title_similarity    — job title vs resume title/designation
  3  f_experience_fit      — experience within required range
  4  f_education_match     — education level alignment
  5  f_certification_match — certification relevance
  6  f_keyword_overlap     — keyword overlap ratio
  7  f_tech_stack_align    — technology stack alignment
  8  f_industry_relevance  — domain/industry match from work history
  9  f_seniority_match     — seniority level alignment
  10 f_location_match      — location compatibility
  11 f_text_similarity     — full resume text vs JD text cosine similarity
"""

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, asdict
from typing import Optional


# ════════════════════════════════════════════════════════════════════════════
# Data classes
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class FeatureVector:
    f_skills_overlap: float = 0.0
    f_skills_tfidf: float = 0.0
    f_title_similarity: float = 0.0
    f_experience_fit: float = 0.0
    f_education_match: float = 0.0
    f_certification_match: float = 0.0
    f_keyword_overlap: float = 0.0
    f_tech_stack_align: float = 0.0
    f_industry_relevance: float = 0.0
    f_seniority_match: float = 0.0
    f_location_match: float = 0.0
    f_text_similarity: float = 0.0

    def to_list(self) -> list[float]:
        return [
            self.f_skills_overlap, self.f_skills_tfidf, self.f_title_similarity,
            self.f_experience_fit, self.f_education_match, self.f_certification_match,
            self.f_keyword_overlap, self.f_tech_stack_align, self.f_industry_relevance,
            self.f_seniority_match, self.f_location_match, self.f_text_similarity,
        ]

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def feature_names() -> list[str]:
        return [
            "skills_overlap", "skills_tfidf", "title_similarity",
            "experience_fit", "education_match", "certification_match",
            "keyword_overlap", "tech_stack_align", "industry_relevance",
            "seniority_match", "location_match", "text_similarity",
        ]


@dataclass
class ResumeData:
    """Structured resume data for feature extraction."""
    skills: list[str]
    keywords: list[str]
    experience_years: Optional[float]
    education: list[str]
    certifications: list[str]
    work_experience: list[dict]
    current_designation: Optional[str]
    location: Optional[str]
    summary: Optional[str]
    raw_text: Optional[str]


@dataclass
class JobData:
    """Structured job data for feature extraction."""
    title: str
    skills: list[str]
    keywords: list[str]
    experience_min: int
    experience_max: int
    education_requirements: list[str]
    location: Optional[str]
    responsibilities: list[str]
    designation: Optional[str]
    raw_jd_text: Optional[str]


# ════════════════════════════════════════════════════════════════════════════
# Skill Synonym Map — groups equivalent skills for smarter matching
# ════════════════════════════════════════════════════════════════════════════

_SKILL_SYNONYMS: dict[str, str] = {}

_SYNONYM_GROUPS = [
    ["javascript", "js", "ecmascript", "es6", "es2015"],
    ["typescript", "ts"],
    ["python", "python3", "py"],
    ["react", "reactjs", "react.js"],
    ["angular", "angularjs", "angular.js"],
    ["vue", "vuejs", "vue.js"],
    ["node", "nodejs", "node.js"],
    ["express", "expressjs", "express.js"],
    ["next", "nextjs", "next.js"],
    ["nuxt", "nuxtjs", "nuxt.js"],
    ["c#", "csharp", "c sharp", "dotnet", ".net"],
    ["c++", "cpp", "cplusplus"],
    ["java", "jdk", "jre", "jvm", "j2ee", "j2se"],
    ["golang", "go"],
    ["rust", "rustlang"],
    ["ruby", "rb"],
    ["ruby on rails", "rails", "ror"],
    ["php", "php7", "php8"],
    ["laravel", "laravel php"],
    ["django", "django python"],
    ["flask", "flask python"],
    ["fastapi", "fast api"],
    ["spring", "spring boot", "springboot", "spring-boot"],
    ["kubernetes", "k8s"],
    ["docker", "docker container", "containerization"],
    ["aws", "amazon web services"],
    ["azure", "microsoft azure"],
    ["gcp", "google cloud", "google cloud platform"],
    ["postgresql", "postgres", "psql", "pgsql"],
    ["mysql", "mariadb"],
    ["mongodb", "mongo"],
    ["redis", "redis cache"],
    ["elasticsearch", "elastic search", "es"],
    ["machine learning", "ml"],
    ["deep learning", "dl"],
    ["artificial intelligence", "ai"],
    ["natural language processing", "nlp"],
    ["computer vision", "cv"],
    ["tensorflow", "tf"],
    ["pytorch", "torch"],
    ["sql", "structured query language"],
    ["nosql", "no sql", "non-relational"],
    ["rest", "restful", "rest api", "rest apis", "restful api", "restful apis"],
    ["graphql", "graph ql"],
    ["ci/cd", "cicd", "ci cd", "continuous integration", "continuous deployment"],
    ["git", "github", "gitlab", "bitbucket"],
    ["terraform", "iac", "infrastructure as code"],
    ["ansible", "configuration management"],
    ["jenkins", "ci server"],
    ["html", "html5"],
    ["css", "css3"],
    ["sass", "scss"],
    ["tailwind", "tailwindcss", "tailwind css"],
    ["bootstrap", "bootstrap css"],
    ["figma", "ui design"],
    ["agile", "scrum", "kanban", "agile methodology", "agile methodologies",
     "agile scrum", "scrum methodology"],
    ["devops", "dev ops"],
    ["microservices", "micro services", "microservice architecture"],
    ["data science", "data analytics"],
    ["power bi", "powerbi"],
    ["tableau", "data visualization"],
    ["sap", "sap erp"],
    ["salesforce", "sfdc"],
    ["jira", "atlassian jira"],
    ["confluence", "atlassian confluence"],
    ["hibernate", "jpa", "java persistence"],
    ["data structures", "dsa", "data structures and algorithms", "data structures & algorithms"],
    ["oop", "object-oriented programming", "object oriented programming"],
    ["shopify", "shopify development"],
    ["wordpress", "wordpress development"],
    ["active directory", "ad"],
    ["office 365", "microsoft 365", "o365"],
    # ── Microsoft Dynamics 365 / CRM / Power Platform ──
    # Note: compound phrases like "D365 Sales Configuration and Customization" from JDs
    # are handled by containment matching in _compute_skills_overlap, not synonym groups.
    # These groups handle the BASE technology names.
    ["dynamics 365", "d365", "dynamics crm", "microsoft dynamics 365", "microsoft dynamics crm",
     "ms dynamics", "ms crm", "mscrm", "ms dynamics 365", "dynamics 365 crm",
     "dynamics crm 365", "microsoft crm", "d365 crm", "crm 365",
     "microsoft dynamics crm development", "dynamics crm development",
     "microsoft dynamics 365 customer engagement", "d365 customer engagement",
     "dynamics 365 customer engagement", "d365 ce", "dynamics 365 ce",
     "crm customer engagement", "crm model apps",
     "crm model apps configuration and customization",
     "d365 crm model apps"],
    ["dynamics 365 sales", "d365 sales", "dynamics sales", "crm sales module",
     "dynamics 365 sales module", "sales module",
     "d365 sales configuration and customization",
     "d365 sales business application"],
    ["dynamics 365 customer service", "d365 customer service", "customer service module",
     "dynamics customer service"],
    ["dynamics 365 customer insights", "d365 customer insights", "customer insights",
     "d365 customer insights journeys", "customer insights journeys",
     "d365 customer insights data", "customer insights data",
     "d365 marketing", "dynamics 365 marketing", "dynamics marketing",
     "d365 customer insights marketing",
     "customer insights data specialty"],
    ["dynamics 365 field service", "d365 field service", "field service module",
     "dynamics field service"],
    ["dynamics 365 project operations", "d365 project operations",
     "project service automation", "psa", "dynamics project operations"],
    ["power platform", "microsoft power platform"],
    ["power apps", "powerapps", "power app", "canvas apps", "model driven apps",
     "model-driven apps", "mda"],
    ["power automate", "microsoft flow", "ms flow", "power automate flows",
     "flow automation"],
    ["power pages", "power portals", "portal", "power portal"],
    ["power bi", "powerbi", "power b i"],
    ["dataverse", "common data service", "cds", "microsoft dataverse"],
    ["copilot studio", "power virtual agents", "pva", "microsoft copilot studio"],
    ["microsoft copilot", "copilot", "m365 copilot", "copilot for sales",
     "copilot for customer insights"],
    ["azure logic apps", "logic apps", "azure logic app"],
    ["azure functions", "azure function"],
    ["azure devops", "azure pipelines", "ado"],
    ["dynamics 365 finance", "d365 finance", "d365 f&o", "dynamics finance and operations",
     "dynamics 365 finance and operations", "d365 finance and operations"],
    # ── CRM / ERP Consulting Skills ──
    ["requirement gathering", "requirements gathering", "business requirements",
     "gathering requirements", "requirement analysis", "requirements analysis"],
    ["gap analysis", "gap fit analysis", "fit gap analysis", "gap/fit analysis"],
    ["solution design", "solution architecture", "solution design document",
     "functional design", "functional design document", "fdd"],
    ["business process flow", "bpf", "business process flows"],
    ["plugin", "plug-in", "plugins", "plug-ins", "plug in", "plug ins", "crm plugin", "crm plugins"],
    ["custom workflow", "custom workflows", "workflow", "workflows"],
    ["ribbon workbench", "ribbon customization", "ribbon customizations"],
    ["fetchxml", "fetch xml", "advanced find"],
    ["xrm toolbox", "xrm tool box"],
    ["solution deployment", "solution management", "solution export",
     "solution import"],
    ["data migration", "data integration", "data migration and integration"],
    ["stakeholder management", "stakeholder engagement", "client engagement",
     "client management", "client relationship management"],
    ["functional consulting", "functional consultant", "business consulting",
     "crm consulting", "crm consultant"],
    ["end-to-end implementation", "end to end implementation",
     "e2e implementation", "full lifecycle implementation",
     "end to end crm implementation", "end to end microsoft customer insights implementation",
     "end to end d365 implementation"],
    ["user acceptance testing", "uat", "acceptance testing"],
    ["training delivery", "end user training", "end-user training",
     "user training", "training and documentation"],
    ["business requirement document", "brd", "functional requirement document",
     "frd", "functional specification"],
    ["project management", "project delivery", "project lifecycle"],
    ["pre-sales", "presales", "rfp", "rfq", "request for proposal"],
    ["change management", "scope management", "change control"],
    # ── ERP / Integration ──
    ["sap integration", "sap connector", "sap"],
    ["odata", "odata api", "web api"],
    ["rest api", "rest apis", "restful api", "api integration"],
    ["ssis", "sql server integration services"],
    ["ssrs", "sql server reporting services"],
]

def _build_synonym_map():
    for group in _SYNONYM_GROUPS:
        canonical = group[0]
        for term in group:
            _SKILL_SYNONYMS[term.lower().strip()] = canonical.lower().strip()

_build_synonym_map()


def _canonicalize_skill(skill: str) -> str:
    """Normalize a skill string to its canonical form.

    Tries multiple strategies:
    1. Exact match in synonym map
    2. Strip parenthetical content and retry
    3. For long compound skills, try progressively shorter prefixes
    """
    s = re.sub(r'[.\-/]', ' ', skill.lower().strip())
    s = re.sub(r'\s+', ' ', s).strip()

    # Try full string first (e.g., "spring boot" → "spring")
    result = _SKILL_SYNONYMS.get(s)
    if result:
        return result

    # Strip parenthetical content: "Java (JDK 8+)" → "java"
    base = re.sub(r'\s*\(.*?\)', '', s).strip()
    if base and base != s:
        result = _SKILL_SYNONYMS.get(base)
        if result:
            return result

    # For long compound skills (4+ words), try progressively shorter prefixes
    # e.g., "microsoft dynamics 365 customer engagement" → try
    #   "microsoft dynamics 365 customer" → "microsoft dynamics 365" → "microsoft dynamics"
    words = (base if base != s else s).split()
    if len(words) >= 4:
        for end in range(len(words) - 1, 1, -1):
            prefix = " ".join(words[:end])
            result = _SKILL_SYNONYMS.get(prefix)
            if result:
                return result

    return base if base != s else s


# ════════════════════════════════════════════════════════════════════════════
# Education Level Hierarchy
# ════════════════════════════════════════════════════════════════════════════

_EDUCATION_LEVELS = {
    "phd": 6, "doctorate": 6, "ph.d": 6, "doctor": 6,
    "masters": 5, "master": 5, "mba": 5, "msc": 5, "m.sc": 5,
    "m.tech": 5, "mtech": 5, "m.e": 5, "ms": 5, "ma": 5, "m.a": 5,
    "postgraduate": 5, "post graduate": 5, "pg": 5,
    "bachelors": 4, "bachelor": 4, "bsc": 4, "b.sc": 4, "b.tech": 4,
    "btech": 4, "b.e": 4, "ba": 4, "b.a": 4, "bba": 4, "bca": 4,
    "undergraduate": 4, "graduate": 4, "degree": 4, "ug": 4,
    "diploma": 3, "associate": 3, "polytechnic": 3,
    "high school": 2, "hsc": 2, "12th": 2, "secondary": 2, "a-level": 2,
    "ssc": 1, "10th": 1, "matric": 1, "gcse": 1, "o-level": 1,
}


def _extract_education_level(education_list: list[str]) -> int:
    """Return the highest education level (1-6) from a list of education strings."""
    max_level = 0
    combined = " ".join(education_list).lower()
    for term, level in _EDUCATION_LEVELS.items():
        if term in combined:
            max_level = max(max_level, level)
    return max_level


# ════════════════════════════════════════════════════════════════════════════
# Seniority Detection
# ════════════════════════════════════════════════════════════════════════════

_SENIORITY_LEVELS = {
    "intern": 1, "trainee": 1, "fresher": 1, "entry": 1, "graduate": 1,
    "junior": 2, "jr": 2, "associate": 2,
    "mid": 3, "intermediate": 3,
    "senior": 4, "sr": 4,
    "lead": 5, "principal": 5, "staff": 5, "team lead": 5,
    "architect": 6, "manager": 6, "head": 6,
    "director": 7, "vp": 7, "vice president": 7,
    "cto": 8, "ceo": 8, "cfo": 8, "cio": 8, "chief": 8, "c-level": 8,
}


def _detect_seniority(text: Optional[str], experience_years: Optional[float] = None) -> int:
    """Detect seniority level (1-8) from a title or designation string."""
    if not text:
        # Infer from experience
        if experience_years is None:
            return 3  # default mid
        if experience_years < 1:
            return 1
        if experience_years < 3:
            return 2
        if experience_years < 6:
            return 3
        if experience_years < 10:
            return 4
        if experience_years < 15:
            return 5
        return 6

    lower = text.lower()
    max_level = 0
    for term, level in _SENIORITY_LEVELS.items():
        if term in lower:
            max_level = max(max_level, level)
    return max_level if max_level > 0 else 3


# ════════════════════════════════════════════════════════════════════════════
# Industry / Domain Detection
# ════════════════════════════════════════════════════════════════════════════

_INDUSTRY_KEYWORDS = {
    "fintech": ["finance", "banking", "fintech", "payment", "trading", "investment", "insurance", "lending", "credit"],
    "healthcare": ["health", "medical", "hospital", "pharma", "clinical", "patient", "biotech", "telemedicine"],
    "ecommerce": ["ecommerce", "e-commerce", "retail", "shopping", "marketplace", "product catalog", "checkout"],
    "edtech": ["education", "edtech", "learning", "school", "university", "training", "lms", "course"],
    "saas": ["saas", "subscription", "platform", "b2b", "enterprise software", "cloud platform"],
    "media": ["media", "news", "publishing", "content", "entertainment", "streaming", "video"],
    "logistics": ["logistics", "supply chain", "shipping", "warehouse", "transportation", "fleet", "delivery"],
    "telecom": ["telecom", "telecommunications", "network", "5g", "mobile operator"],
    "gaming": ["gaming", "game", "esports", "virtual reality", "vr", "ar", "metaverse"],
    "cybersecurity": ["security", "cybersecurity", "infosec", "penetration", "compliance", "soc", "siem"],
    "iot": ["iot", "internet of things", "embedded", "sensor", "smart device", "firmware"],
    "automotive": ["automotive", "vehicle", "car", "ev", "electric vehicle", "autonomous"],
    "real_estate": ["real estate", "property", "construction", "proptech"],
    "hr_tech": ["hr", "human resources", "recruitment", "talent", "payroll", "workforce"],
    "travel": ["travel", "hospitality", "hotel", "booking", "tourism", "airline"],
    "agriculture": ["agriculture", "farming", "agritech", "crop"],
    "government": ["government", "public sector", "civic", "municipality"],
    "energy": ["energy", "oil", "gas", "renewable", "solar", "wind"],
}


def _detect_industries(text: str) -> set[str]:
    """Return set of industry tags detected in text."""
    lower = text.lower()
    found = set()
    for industry, keywords in _INDUSTRY_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                found.add(industry)
                break
    return found


# ════════════════════════════════════════════════════════════════════════════
# Location Matching
# ════════════════════════════════════════════════════════════════════════════

_LOCATION_ALIASES = {
    "uae": ["dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah", "fujairah", "umm al quwain", "uae", "united arab emirates"],
    "india": ["india", "mumbai", "delhi", "bangalore", "bengaluru", "hyderabad", "chennai", "pune", "kolkata", "ahmedabad", "noida", "gurgaon", "gurugram"],
    "usa": ["usa", "united states", "us", "new york", "san francisco", "los angeles", "chicago", "seattle", "austin", "boston", "miami"],
    "uk": ["uk", "united kingdom", "london", "manchester", "birmingham", "edinburgh", "glasgow"],
    "canada": ["canada", "toronto", "vancouver", "montreal", "ottawa", "calgary"],
    "saudi": ["saudi", "saudi arabia", "riyadh", "jeddah", "ksa"],
    "qatar": ["qatar", "doha"],
    "singapore": ["singapore"],
    "australia": ["australia", "sydney", "melbourne", "brisbane", "perth"],
    "germany": ["germany", "berlin", "munich", "frankfurt", "hamburg"],
    "remote": ["remote", "work from home", "wfh", "anywhere", "distributed"],
}


def _normalize_location(loc: Optional[str]) -> set[str]:
    """Return a set of region tags for a location string."""
    if not loc:
        return set()
    lower = loc.lower()
    regions = set()
    for region, keywords in _LOCATION_ALIASES.items():
        for kw in keywords:
            if kw in lower:
                regions.add(region)
    return regions


# ════════════════════════════════════════════════════════════════════════════
# TF-IDF Cosine Similarity (lightweight, no sklearn needed)
# ════════════════════════════════════════════════════════════════════════════

def _tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens, removing short/stop words."""
    _STOP_WORDS = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "shall", "this", "that", "these",
        "those", "i", "you", "he", "she", "it", "we", "they", "me", "him",
        "her", "us", "them", "my", "your", "his", "its", "our", "their",
        "not", "no", "nor", "so", "if", "as", "very", "also", "just",
        "about", "up", "out", "all", "more", "some", "any", "each", "etc",
    }
    words = re.findall(r'[a-z0-9#+.]+', text.lower())
    return [w for w in words if len(w) >= 2 and w not in _STOP_WORDS]


def _tfidf_cosine(text_a: str, text_b: str) -> float:
    """Compute cosine similarity between two texts using TF (no IDF needed for pairs)."""
    if not text_a or not text_b:
        return 0.0
    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)
    if not tokens_a or not tokens_b:
        return 0.0

    counter_a = Counter(tokens_a)
    counter_b = Counter(tokens_b)
    all_terms = set(counter_a.keys()) | set(counter_b.keys())

    dot = sum(counter_a.get(t, 0) * counter_b.get(t, 0) for t in all_terms)
    mag_a = math.sqrt(sum(v * v for v in counter_a.values()))
    mag_b = math.sqrt(sum(v * v for v in counter_b.values()))

    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ════════════════════════════════════════════════════════════════════════════
# Feature Extraction Functions
# ════════════════════════════════════════════════════════════════════════════

def _compute_skills_overlap(
    resume_skills: list[str], job_skills: list[str], raw_text: Optional[str] = None
) -> float:
    """Fuzzy skill matching with synonym awareness, containment matching, and raw text fallback. Returns 0.0–1.0."""
    if not job_skills:
        return 0.0  # no job skills defined → can't score

    canonical_resume = {_canonicalize_skill(s) for s in resume_skills}
    canonical_job = {_canonicalize_skill(s) for s in job_skills}

    if not canonical_job:
        return 0.0

    # Pass 1: Direct match (exact canonical match)
    matched = canonical_resume & canonical_job

    # Pass 2: Containment matching — if a resume skill is CONTAINED in a job skill
    # or vice versa, count it. This handles compound domain skills:
    # e.g., JD: "dynamics 365 customer engagement" matches resume: "dynamics 365"
    # e.g., JD: "data integration and migration" matches resume: "data integration"
    unmatched_job = canonical_job - matched
    for jskill in list(unmatched_job):
        j_tokens = set(jskill.split())
        if len(j_tokens) < 2:
            continue  # skip single-word skills for containment — too many false positives
        for rskill in canonical_resume:
            r_tokens = set(rskill.split())
            if len(r_tokens) < 2:
                continue
            # Resume skill is fully contained in job skill (or vice versa)
            # e.g., "dynamics 365" ⊂ "dynamics 365 customer engagement"
            if r_tokens.issubset(j_tokens) or j_tokens.issubset(r_tokens):
                matched.add(jskill)
                break

    # Pass 3: Multi-word token overlap matching (lowered threshold to 50%)
    # Handles cases like "d365 sales configuration and customization" vs "dynamics 365 sales"
    unmatched_job = canonical_job - matched
    for jskill in list(unmatched_job):
        j_tokens = set(jskill.split())
        if len(j_tokens) < 2:
            continue
        # Remove common filler words for better matching
        _FILLER = {"and", "or", "the", "of", "in", "for", "with", "on", "to", "a", "an"}
        j_meaningful = j_tokens - _FILLER
        if not j_meaningful:
            j_meaningful = j_tokens
        for rskill in canonical_resume:
            r_tokens = set(rskill.split())
            r_meaningful = r_tokens - _FILLER
            if not r_meaningful:
                r_meaningful = r_tokens
            # Match if 50%+ of meaningful job skill tokens overlap with resume
            overlap = len(j_meaningful & r_meaningful)
            if overlap >= 1 and overlap / len(j_meaningful) >= 0.50:
                matched.add(jskill)
                break

    # Pass 4: Raw text fallback — scan resume full text for job skill phrases
    if raw_text and (canonical_job - matched):
        raw_lower = raw_text.lower()
        still_unmatched = canonical_job - matched
        for jskill in list(still_unmatched):
            # Check canonical form with word boundaries
            if re.search(r'\b' + re.escape(jskill) + r'\b', raw_lower):
                matched.add(jskill)
                continue
            # Check original skill names with word boundaries
            for orig_skill in job_skills:
                canon = _canonicalize_skill(orig_skill)
                if canon == jskill:
                    orig_lower = orig_skill.lower()
                    if re.search(r'\b' + re.escape(orig_lower) + r'\b', raw_lower):
                        matched.add(jskill)
                        break
            if jskill in matched:
                continue
            # Check individual meaningful tokens (2+ chars) in raw text
            # If most of the job skill's key tokens appear in the resume text, count it
            j_tokens = set(jskill.split()) - {"and", "or", "the", "of", "in", "for", "with", "on", "to", "a", "an"}
            if len(j_tokens) >= 2:
                token_hits = sum(1 for t in j_tokens if len(t) > 2 and re.search(r'\b' + re.escape(t) + r'\b', raw_lower))
                if token_hits / len(j_tokens) >= 0.60:
                    matched.add(jskill)

    return min(1.0, len(matched) / len(canonical_job))


def _compute_skills_tfidf(resume_skills: list[str], job_skills: list[str]) -> float:
    """TF-IDF cosine similarity between skills lists as text."""
    return _tfidf_cosine(" ".join(resume_skills), " ".join(job_skills))


def _compute_title_similarity(resume_designation: Optional[str], job_title: str) -> float:
    """Token overlap between resume designation and job title. Returns 0.0–1.0.

    Generic role words (developer, engineer, analyst, etc.) are treated as
    low-weight tokens — they only contribute 0.2 each instead of 1.0.
    Domain-specific words (java, python, data, frontend, etc.) carry full weight.
    This prevents 'Web Developer' matching 'Java Developer' at 50%.
    """
    if not resume_designation or not job_title:
        return 0.0

    # Seniority filler — completely ignored
    _SENIORITY = {"junior", "senior", "lead", "principal", "staff", "intern",
                  "associate", "head", "chief", "sr", "jr", "i", "ii", "iii",
                  "trainee", "fresher", "entry", "level", "mid"}

    # Generic role words — low weight (0.2 instead of 1.0)
    _ROLE_GENERIC = {"developer", "engineer", "analyst", "consultant", "specialist",
                     "architect", "administrator", "manager", "coordinator", "executive",
                     "designer", "programmer", "tester", "officer", "technician"}

    r_tokens = set(re.findall(r'[a-z0-9#+]+', resume_designation.lower())) - _SENIORITY
    j_tokens = set(re.findall(r'[a-z0-9#+]+', job_title.lower())) - _SENIORITY

    if not j_tokens:
        return 0.0

    # Canonicalize tokens using synonym map
    r_canon = {_SKILL_SYNONYMS.get(t, t) for t in r_tokens}
    j_canon = {_SKILL_SYNONYMS.get(t, t) for t in j_tokens}

    # Weighted overlap: domain words = 1.0, generic role words = 0.2
    total_weight = 0.0
    matched_weight = 0.0
    for token in j_canon:
        w = 0.2 if token in _ROLE_GENERIC else 1.0
        total_weight += w
        if token in r_canon:
            matched_weight += w

    return matched_weight / total_weight if total_weight > 0 else 0.0


def _compute_experience_fit(
    candidate_years: Optional[float], required_min: int, required_max: int
) -> float:
    """Score how well candidate experience fits the required range. Returns 0.0–1.0."""
    if candidate_years is None:
        return 0.3  # unknown → partial credit

    if required_min <= candidate_years <= required_max:
        return 1.0

    if candidate_years > required_max:
        # Overqualified — gradual penalty
        overshoot = candidate_years - required_max
        return max(0.3, 1.0 - (overshoot * 0.08))

    # Underqualified
    shortfall = required_min - candidate_years
    return max(0.0, 1.0 - (shortfall * 0.2))


def _compute_education_match(resume_education: list[str], job_education: list[str]) -> float:
    """Compare education levels. Returns 0.0–1.0."""
    resume_level = _extract_education_level(resume_education)
    job_level = _extract_education_level(job_education)

    if job_level == 0:
        return 0.5  # no requirement specified

    if resume_level == 0:
        return 0.2  # no education detected

    if resume_level >= job_level:
        return 1.0  # meets or exceeds

    # Below requirement
    diff = job_level - resume_level
    return max(0.0, 1.0 - (diff * 0.25))


def _compute_certification_match(resume_certs: list[str], job_skills: list[str], job_keywords: list[str]) -> float:
    """Check if resume certifications are relevant to the job. Returns 0.0–1.0."""
    if not resume_certs:
        return 0.0  # no certs → no credit

    # Build reference set from job
    reference = set()
    for s in job_skills + job_keywords:
        reference.update(re.findall(r'[a-z0-9#+]+', s.lower()))

    # Check cert relevance
    matched = 0
    for cert in resume_certs:
        cert_tokens = set(re.findall(r'[a-z0-9#+]+', cert.lower()))
        if cert_tokens & reference:
            matched += 1

    return min(1.0, matched / max(len(resume_certs), 1))


def _compute_keyword_overlap(
    resume_keywords: list[str], job_keywords: list[str], raw_text: Optional[str] = None
) -> float:
    """Keyword overlap ratio with canonicalization + raw text fallback. Returns 0.0–1.0."""
    if not job_keywords:
        return 0.0  # no job keywords → can't score

    r_canon = {_canonicalize_skill(k) for k in resume_keywords}
    j_canon = {_canonicalize_skill(k) for k in job_keywords}

    # Also tokenize multi-word keywords for partial matching
    r_tokens = set()
    for k in resume_keywords:
        r_tokens.update(re.findall(r'[a-z0-9#+]+', k.lower()))
    j_tokens = set()
    for k in job_keywords:
        j_tokens.update(re.findall(r'[a-z0-9#+]+', k.lower()))

    # Raw text token boost — scan resume full text for job keyword tokens
    # Use word boundary matching to avoid "java" matching inside "javascript"
    if raw_text:
        raw_lower = raw_text.lower()
        for jt in j_tokens:
            if jt not in r_tokens and len(jt) >= 2:
                if re.search(r'\b' + re.escape(jt) + r'\b', raw_lower):
                    r_tokens.add(jt)

    # Combined: direct + token overlap
    direct_overlap = len(r_canon & j_canon) / len(j_canon) if j_canon else 0.0
    token_overlap = len(r_tokens & j_tokens) / len(j_tokens) if j_tokens else 0.0

    return min(1.0, direct_overlap * 0.4 + token_overlap * 0.6)


def _compute_tech_stack_alignment(
    resume_skills: list[str], job_skills: list[str], raw_text: Optional[str] = None
) -> float:
    """Deeper tech stack matching — considers skill clusters + raw text. Returns 0.0–1.0."""
    _TECH_CLUSTERS = {
        "frontend": {"react", "angular", "vue", "html", "css", "javascript", "typescript", "sass", "tailwind", "bootstrap", "next", "nuxt", "svelte"},
        "backend": {"node", "python", "java", "c#", "go", "ruby", "php", "rust", "express", "django", "flask", "fastapi", "spring", "laravel", "rails"},
        "database": {"postgresql", "mysql", "mongodb", "redis", "elasticsearch", "dynamodb", "cassandra", "sqlite", "sql", "oracle", "mariadb"},
        "cloud": {"aws", "azure", "gcp", "docker", "kubernetes", "terraform", "ansible", "jenkins", "ci/cd"},
        "mobile": {"react native", "flutter", "swift", "kotlin", "ios", "android", "dart"},
        "data": {"python", "r", "sql", "spark", "hadoop", "kafka", "airflow", "pandas", "numpy", "tableau", "power bi", "machine learning", "deep learning"},
    }

    resume_canon = {_canonicalize_skill(s) for s in resume_skills}
    job_canon = {_canonicalize_skill(s) for s in job_skills}

    # Also scan raw text for tech terms (with word boundary to avoid false positives)
    if raw_text:
        raw_lower = raw_text.lower()
        for cluster_techs in _TECH_CLUSTERS.values():
            for tech in cluster_techs:
                if re.search(r'\b' + re.escape(tech) + r'\b', raw_lower):
                    resume_canon.add(tech)

    if not job_canon:
        return 0.0

    # Determine which clusters the job needs
    job_clusters = set()
    for cluster, tech_set in _TECH_CLUSTERS.items():
        if job_canon & tech_set:
            job_clusters.add(cluster)

    if not job_clusters:
        # Fallback to plain overlap
        overlap = resume_canon & job_canon
        return min(1.0, len(overlap) / len(job_canon)) if job_canon else 0.0

    # Score per cluster
    cluster_scores = []
    for cluster in job_clusters:
        tech_set = _TECH_CLUSTERS[cluster]
        job_in_cluster = job_canon & tech_set
        resume_in_cluster = resume_canon & tech_set
        if job_in_cluster:
            cluster_scores.append(len(resume_in_cluster & job_in_cluster) / len(job_in_cluster))
        else:
            cluster_scores.append(0.0)

    return sum(cluster_scores) / len(cluster_scores) if cluster_scores else 0.0


def _compute_industry_relevance(resume_data: ResumeData, job_data: JobData) -> float:
    """Check if candidate has relevant industry experience. Returns 0.0–1.0."""
    # Build resume industry signal from work experience + summary
    resume_text_parts = []
    for exp in resume_data.work_experience:
        if isinstance(exp, dict):
            resume_text_parts.append(exp.get("title", ""))
            resume_text_parts.append(exp.get("company", ""))
            resume_text_parts.append(exp.get("description", ""))
    if resume_data.summary:
        resume_text_parts.append(resume_data.summary)

    resume_industries = _detect_industries(" ".join(resume_text_parts))

    # Build job industry signal
    job_text = f"{job_data.title} {' '.join(job_data.responsibilities)} {job_data.raw_jd_text or ''}"
    job_industries = _detect_industries(job_text)

    if not job_industries:
        return 0.3  # no clear industry → low neutral

    if not resume_industries:
        return 0.1  # can't determine → minimal credit

    overlap = resume_industries & job_industries
    return len(overlap) / len(job_industries) if job_industries else 0.0


def _compute_seniority_match(resume_data: ResumeData, job_data: JobData) -> float:
    """Compare seniority levels. Returns 0.0–1.0."""
    resume_seniority = _detect_seniority(resume_data.current_designation, resume_data.experience_years)
    job_seniority = _detect_seniority(job_data.designation, float((job_data.experience_min + job_data.experience_max) / 2))

    diff = abs(resume_seniority - job_seniority)
    if diff == 0:
        return 1.0
    if diff == 1:
        return 0.8  # one level off — still strong
    if diff == 2:
        return 0.5
    return max(0.0, 1.0 - diff * 0.2)


def _compute_location_match(resume_location: Optional[str], job_location: Optional[str]) -> float:
    """Location compatibility. Returns 0.0–1.0."""
    r_regions = _normalize_location(resume_location)
    j_regions = _normalize_location(job_location)

    if not j_regions or "remote" in j_regions:
        return 1.0  # remote or unspecified → always match

    if not r_regions:
        return 0.3  # unknown location

    if r_regions & j_regions:
        return 1.0  # same region

    return 0.2  # different region


def _compute_text_similarity(
    resume_text: Optional[str], jd_text: Optional[str], job: Optional["JobData"] = None
) -> float:
    """Full document cosine similarity. Returns 0.0–1.0."""
    if not resume_text:
        return 0.0

    # Build JD text from structured data if raw_jd_text is empty
    if not jd_text and job:
        parts = [job.title or ""]
        parts.extend(job.skills)
        parts.extend(job.keywords)
        parts.extend(job.responsibilities)
        jd_text = " ".join(parts)

    if not jd_text:
        return 0.0

    # Limit text length for performance
    return _tfidf_cosine(resume_text[:5000], jd_text[:5000])


# ════════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ════════════════════════════════════════════════════════════════════════════

def extract_features(resume: ResumeData, job: JobData) -> FeatureVector:
    """Extract a 12-dimension feature vector from a resume–job pair."""
    raw = resume.raw_text
    return FeatureVector(
        f_skills_overlap=_compute_skills_overlap(resume.skills, job.skills, raw),
        f_skills_tfidf=_compute_skills_tfidf(resume.skills, job.skills),
        f_title_similarity=_compute_title_similarity(resume.current_designation, job.title),
        f_experience_fit=_compute_experience_fit(resume.experience_years, job.experience_min, job.experience_max),
        f_education_match=_compute_education_match(resume.education, job.education_requirements),
        f_certification_match=_compute_certification_match(resume.certifications, job.skills, job.keywords),
        f_keyword_overlap=_compute_keyword_overlap(resume.keywords, job.keywords, raw),
        f_tech_stack_align=_compute_tech_stack_alignment(resume.skills, job.skills, raw),
        f_industry_relevance=_compute_industry_relevance(resume, job),
        f_seniority_match=_compute_seniority_match(resume, job),
        f_location_match=_compute_location_match(resume.location, job.location),
        f_text_similarity=_compute_text_similarity(resume.raw_text, job.raw_jd_text, job),
    )


# ════════════════════════════════════════════════════════════════════════════
# Helper: Build data objects from DB models
# ════════════════════════════════════════════════════════════════════════════

def _safe_json(val) -> list:
    if not val:
        return []
    if isinstance(val, list):
        return val
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def resume_data_from_metadata(meta) -> ResumeData:
    """Build ResumeData from a ResumeMetadata ORM object."""
    return ResumeData(
        skills=_safe_json(meta.skills),
        keywords=_safe_json(meta.keywords),
        experience_years=meta.experience_years,
        education=_safe_json(meta.education),
        certifications=_safe_json(meta.certifications),
        work_experience=_safe_json(meta.work_experience),
        current_designation=meta.current_designation,
        location=meta.location,
        summary=meta.summary,
        raw_text=meta.raw_text,
    )


def job_data_from_db(job, job_meta=None, db=None) -> JobData:
    """Build JobData from a Job ORM object + optional JobMetadata."""
    from app.models.job import JobSkill, JobResponsibility

    skills = [s.skill for s in job.skills] if job.skills else []
    responsibilities = [r.description for r in job.responsibilities] if job.responsibilities else []

    # Merge with metadata if available
    meta_skills = _safe_json(job_meta.required_skills) if job_meta else []
    meta_keywords = _safe_json(job_meta.keywords) if job_meta else []
    education_reqs = _safe_json(job_meta.education_requirements) if job_meta else []

    # Deduplicate skills
    all_skills = list({_canonicalize_skill(s): s for s in skills + meta_skills}.values())

    # Build keywords from multiple sources
    all_keywords = list(set(meta_keywords))
    if not all_keywords:
        # Fallback: generate keywords from title + skills
        tokens = set(re.findall(r'[a-z0-9#+]+', job.title.lower())) if job.title else set()
        for s in all_skills:
            tokens.update(re.findall(r'[a-z0-9#+]+', s.lower()))
        all_keywords = list(tokens)

    return JobData(
        title=job.title or "",
        skills=all_skills,
        keywords=all_keywords,
        experience_min=job.experience_min or 0,
        experience_max=job.experience_max or 99,
        education_requirements=education_reqs,
        location=job.location,
        responsibilities=responsibilities,
        designation=job_meta.designation if job_meta else None,
        raw_jd_text=job_meta.raw_jd_text if job_meta else None,
    )
