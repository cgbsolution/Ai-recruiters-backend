"""
Fast regex-based resume parser for bulk processing.

Extracts metadata WITHOUT any AI/LLM calls — pure text analysis using
regex patterns and keyword matching. Designed for processing 50k+ resumes.

Supports: PDF, DOCX

Extracts:
  - Name, Email, Phone, Location
  - Social Links (LinkedIn, GitHub, Portfolio)
  - Skills (360+ keywords)
  - Work Experience (company, title, dates, description)
  - Education (degree, institution, year)
  - Certifications
  - Keywords for search/matching
"""

import re
import json
import logging
from typing import Optional

from app.services.locations import find_city, place_from_text

# Bump when the parser's output changes in a way worth re-parsing for. Bulk
# re-uploads of an identical file reuse the stored parse only when it was made
# by this version (app.routers.resume_database._claim_bulk_row).
# 2: location from labelled lines / the contact header only, validated against
#    known places ("cost allocation, and profitability" no longer becomes the
#    location "And Profitability"); skills matched as whole words.
FAST_PARSER_VERSION = 2

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Common skill keywords (lowercase)
# ══════════════════════════════════════════════════════════════════════════════

COMMON_SKILLS = {
    # Programming languages
    "python", "java", "javascript", "typescript", "c++", "c#", "ruby", "go",
    "golang", "rust", "php", "swift", "kotlin", "scala", "r", "matlab",
    "perl", "shell", "bash", "powershell", "sql", "html", "css", "sass",
    "less", "dart", "lua", "groovy", "objective-c", "assembly",
    # Frameworks & libraries
    "react", "reactjs", "react.js", "angular", "angularjs", "vue", "vuejs",
    "vue.js", "next.js", "nextjs", "nuxt", "nuxtjs", "svelte", "django",
    "flask", "fastapi", "spring", "spring boot", "express", "expressjs",
    "node.js", "nodejs", "nest.js", "nestjs", "rails", "ruby on rails",
    "laravel", "asp.net", ".net", "dotnet", ".net core", "blazor",
    "jquery", "bootstrap", "tailwind", "tailwindcss", "material ui",
    "flutter", "react native", "ionic", "electron", "three.js",
    "hibernate", "jpa", "mybatis", "struts", "jsf", "jsoup",
    "spring mvc", "spring security", "spring cloud", "spring data",
    # Databases
    "mysql", "postgresql", "postgres", "mongodb", "redis", "elasticsearch",
    "cassandra", "dynamodb", "sqlite", "oracle", "sql server", "mariadb",
    "firebase", "supabase", "neo4j", "couchdb", "influxdb",
    # Cloud & DevOps
    "aws", "amazon web services", "azure", "gcp", "google cloud",
    "docker", "kubernetes", "k8s", "terraform", "ansible", "jenkins",
    "ci/cd", "github actions", "gitlab ci", "circleci", "travis ci",
    "nginx", "apache", "linux", "unix", "ubuntu", "centos",
    "cloudflare", "vercel", "netlify", "heroku", "digitalocean",
    # AI/ML
    "machine learning", "deep learning", "artificial intelligence",
    "tensorflow", "pytorch", "keras", "scikit-learn", "sklearn",
    "nlp", "natural language processing", "computer vision", "opencv",
    "pandas", "numpy", "scipy", "matplotlib", "jupyter",
    "langchain", "openai", "hugging face", "transformers",
    # Data
    "data science", "data engineering", "data analysis", "data analytics",
    "big data", "hadoop", "spark", "apache spark", "kafka", "airflow",
    "etl", "data warehouse", "snowflake", "databricks", "tableau",
    "power bi", "looker", "metabase", "grafana",
    # Mobile
    "android", "ios", "mobile development", "xcode", "android studio",
    "swiftui", "jetpack compose",
    # Testing
    "selenium", "cypress", "jest", "mocha", "pytest", "junit",
    "testing", "unit testing", "integration testing", "qa", "automation",
    "mockito", "testng", "cucumber", "appium",
    # Tools
    "git", "github", "gitlab", "bitbucket", "jira", "confluence",
    "slack", "figma", "sketch", "adobe xd", "postman", "swagger",
    "maven", "gradle", "npm", "yarn", "webpack", "vite",
    # Methodologies
    "agile", "scrum", "kanban", "devops", "microservices", "rest",
    "restful", "graphql", "grpc", "soap", "api", "websocket",
    # Security
    "cybersecurity", "penetration testing", "owasp", "ssl", "tls",
    "oauth", "jwt", "encryption", "firewall",
    # Business / Soft skills
    "project management", "team leadership", "communication",
    "problem solving", "analytical", "strategic planning",
    "stakeholder management", "cross-functional",
    # SAP / ERP
    "sap", "erp", "salesforce", "crm", "hubspot",
    # Certifications keywords
    "aws certified", "azure certified", "google certified",
    "pmp", "scrum master", "cissp", "ccna", "itil",
    # E-commerce / CMS / Web platforms
    "shopify", "wordpress", "woocommerce", "magento", "drupal",
    "contentful", "strapi", "sanity", "webflow", "squarespace",
    "bigcommerce", "prestashop",
    # Additional frameworks & tools
    "spring batch", "spring webflux", "quarkus", "micronaut",
    "remix", "gatsby", "astro", "hugo", "jekyll",
    "storybook", "chromatic", "playwright", "vitest",
    "pnpm", "bun", "deno", "turbopack", "esbuild", "rollup",
    "babel", "parcel", "lerna", "nx", "monorepo",
    # Java ecosystem
    "java", "jdk", "jre", "jvm", "spring framework",
    "maven", "gradle", "ant", "tomcat", "wildfly", "jboss",
    "servlet", "jsp", "jpa", "hibernate", "mybatis",
    "junit", "testng", "mockito", "log4j", "slf4j",
    "lombok", "jackson", "gson", "apache camel", "activemq",
    "rabbitmq", "kafka", "zookeeper",
    # OOP / Patterns
    "object-oriented programming", "oop", "design patterns",
    "solid principles", "solid", "mvc", "mvvm",
    # Data structures
    "data structures", "algorithms", "data structures & algorithms",
    "data structures and algorithms", "dsa",
    # Additional databases & tools
    "cockroachdb", "timescaledb", "clickhouse", "memcached",
    "consul", "vault", "etcd", "minio", "s3",
    # Networking / Infra
    "active directory", "ldap", "dns", "dhcp", "tcp/ip",
    "vpn", "proxy", "load balancer", "haproxy",
    "vmware", "hyper-v", "virtualbox", "vagrant",
    "prometheus", "grafana", "datadog", "new relic", "splunk",
    "elk", "logstash", "kibana", "fluentd",
    "nagios", "zabbix", "pagerduty", "opsgenie",
    # Additional cloud services
    "lambda", "ec2", "s3", "rds", "cloudfront", "route53",
    "api gateway", "sqs", "sns", "ecs", "eks", "fargate",
    "azure devops", "azure functions", "azure ad",
    "cloud run", "cloud functions", "bigquery", "pub/sub",
    # Additional programming
    "haskell", "elixir", "erlang", "clojure", "f#",
    "fortran", "cobol", "prolog", "lisp", "scheme",
    "solidity", "web3", "blockchain", "smart contracts",
    # Version control / collaboration
    "svn", "mercurial", "perforce",
    "trello", "asana", "monday.com", "notion", "linear",
    "microsoft teams", "zoom",
    # OS & admin
    "windows server", "active directory", "group policy",
    "macos", "red hat", "debian", "fedora", "alpine",
    "shell scripting", "cron", "systemd",
    # Misc tech
    "oauth2", "saml", "sso", "ldap", "mfa",
    "websocket", "socket.io", "webrtc",
    "pwa", "service worker", "web assembly", "wasm",
    "xml", "json", "yaml", "toml", "protobuf", "avro",
    "regex", "cron", "makefile", "cmake",
    # Accounting / Finance domain
    "quickbooks", "quickbooks online", "xero", "tally", "tally erp",
    "zoho books", "zoho", "bill.com", "netsuite", "oracle netsuite",
    "sage", "sage 50", "sage intacct", "wave accounting",
    "accounts payable", "accounts receivable", "a/p", "a/r",
    "payroll", "payroll processing", "reconciliation", "bank reconciliation",
    "general ledger", "ledger", "trial balance", "journal entries",
    "financial statements", "balance sheet", "income statement",
    "profit and loss", "p&l", "cash flow", "cash flow management",
    "budgeting", "forecasting", "financial reporting", "financial analysis",
    "tax preparation", "tax filing", "gst", "vat", "tds", "income tax",
    "audit", "internal audit", "statutory audit", "tax audit",
    "us gaap", "gaap", "ifrs", "ind as", "accounting standards",
    "1099", "w-2", "w2", "form 16",
    "vlookup", "pivot table", "pivot tables", "excel", "ms excel",
    "microsoft excel", "google sheets",
    "bookkeeping", "cost accounting", "management accounting",
    "fixed assets", "depreciation", "amortization",
    "intercompany", "consolidation", "month-end closing", "year-end closing",
    "variance analysis", "financial modelling", "financial modeling",
    "erp", "mis", "mis reporting",
    "ca", "cpa", "cfa", "acca", "cma",
}


# ══════════════════════════════════════════════════════════════════════════════
# Section heading patterns — used to split resume into logical blocks
# ══════════════════════════════════════════════════════════════════════════════

_SECTION_ALIASES = {
    "experience": [
        "experience", "work experience", "professional experience",
        "employment history", "employment", "work history",
        "professional history", "career history", "career",
        "working experience", "job experience", "experience details",
        "professional background",
    ],
    "education": [
        "education", "educational qualifications", "academic qualifications",
        "academic details", "academic background", "qualifications",
        "educational background", "education details", "academics",
        "academic profile", "scholastic record",
    ],
    "skills": [
        "skills", "technical skills", "key skills", "core skills",
        "core competencies", "competencies", "areas of expertise",
        "expertise", "technologies", "tools & technologies",
        "tools and technologies", "professional skills", "it skills",
        "skill set", "technical proficiency", "technical expertise",
    ],
    "certifications": [
        "certifications", "certificates", "certification",
        "professional certifications", "licenses & certifications",
        "licenses and certifications", "training & certifications",
        "training and certifications", "professional development",
        "courses", "training",
    ],
    "projects": [
        "projects", "key projects", "project details", "project experience",
        "personal projects", "academic projects",
    ],
    "summary": [
        "summary", "profile summary", "professional summary",
        "career summary", "career objective", "objective",
        "about me", "profile", "overview", "introduction",
    ],
}

# Build a reverse map: heading text → section name
_HEADING_MAP: dict[str, str] = {}
for section, aliases in _SECTION_ALIASES.items():
    for alias in aliases:
        _HEADING_MAP[alias] = section


# ══════════════════════════════════════════════════════════════════════════════
# Education
# ══════════════════════════════════════════════════════════════════════════════

EDUCATION_DEGREES = {
    "phd": "PhD", "doctorate": "PhD", "doctoral": "PhD", "ph.d": "PhD",
    "mtech": "M.Tech", "m.tech": "M.Tech", "mba": "MBA", "m.b.a": "MBA",
    "mca": "MCA", "msc": "M.Sc", "m.sc": "M.Sc", "ms": "M.S", "m.s": "M.S",
    "master": "Master's", "m.e": "M.E", "me": "M.E", "masters": "Master's",
    "mcom": "M.Com", "m.com": "M.Com", "mphil": "M.Phil", "m.phil": "M.Phil",
    "ma": "M.A", "m.a": "M.A", "pgdm": "PGDM",
    "btech": "B.Tech", "b.tech": "B.Tech", "be": "B.E", "b.e": "B.E",
    "bsc": "B.Sc", "b.sc": "B.Sc", "bs": "B.S", "b.s": "B.S",
    "bca": "BCA", "bba": "BBA", "bcom": "B.Com", "b.com": "B.Com",
    "bachelor": "Bachelor's", "bachelors": "Bachelor's", "ba": "B.A", "b.a": "B.A",
    "diploma": "Diploma", "associate": "Associate",
    "12th": "12th", "hsc": "HSC", "10th": "10th", "ssc": "SSC",
}

_INSTITUTION_KEYWORDS = [
    "university", "institute", "college", "school", "academy",
    "polytechnic", "iit", "nit", "iiit", "bits", "vit",
]


# ══════════════════════════════════════════════════════════════════════════════
# Designation / Title keywords
# ══════════════════════════════════════════════════════════════════════════════

DESIGNATION_PATTERNS = [
    r"(?:senior|sr\.?|lead|principal|staff|chief|head)\s+\w+\s*(?:engineer|developer|architect|manager|analyst|designer|consultant|scientist|officer)",
    r"(?:junior|jr\.?|associate|entry.level|trainee|intern)\s+\w+\s*(?:engineer|developer|analyst|designer)",
    r"\b(?:software|web|frontend|front-end|backend|back-end|full.?stack|mobile|devops|cloud|data|ml|ai|qa|test|security|network|system|database|ui|ux)\s+(?:engineer|developer|architect|analyst|scientist|designer|administrator|consultant)\b",
    r"\b(?:project|product|program|engineering|technical|it|operations|delivery)\s+(?:manager|director|lead|head|coordinator)\b",
    r"\b(?:cto|ceo|cfo|coo|vp|vice president|director|manager|team lead|tech lead)\b",
    r"\b(?:consultant|analyst|specialist|coordinator|administrator|executive)\b",
]


# ══════════════════════════════════════════════════════════════════════════════
# Text Extraction from files
# ══════════════════════════════════════════════════════════════════════════════

def extract_text_from_file(file_path: str) -> Optional[str]:
    """Extract plain text from PDF or DOCX file. Returns None on failure.

    For PDFs we apply ``page.dedupe_chars(tolerance=1)`` before extracting text.
    This removes characters drawn twice at (near) identical coordinates — a
    common artefact of Canva / InDesign "faux-bold" effects where the same
    glyph is stamped twice with a tiny offset. Without this, every character
    in the styled regions ends up doubled (e.g. ``yyuukkttii@@ggmmaaiill..ccoomm``),
    which breaks email/phone regex extraction and confuses the LLM parser.

    Returns None for purely image/scanned PDFs (no embedded text layer).
    """
    # One reader for every upload path: the Resume Database, job applications
    # and re-parses all go through app.services.resume_service.extract_text
    # (legacy .doc, damaged DOCX/PDF handling lives there).
    from app.services.resume_service import extract_text

    try:
        return extract_text(file_path) or None
    except Exception as e:
        logger.warning("Text extraction failed for %s: %s", file_path, e)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Section Splitting
# ══════════════════════════════════════════════════════════════════════════════

def _split_sections(text: str) -> dict[str, str]:
    """
    Split resume text into named sections based on heading detection.
    Returns {"experience": "...", "education": "...", "skills": "...", ...}
    Also includes "_header" (text before first section) and "_full" (entire text).
    """
    lines = text.split("\n")
    sections: dict[str, list[str]] = {"_header": [], "_full": [text]}
    current_section = "_header"

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_section in sections:
                sections[current_section].append("")
            continue

        # Check if this line is a section heading
        cleaned = re.sub(r"[:\-–—|#*=_]", "", stripped).strip().lower()
        # A heading is typically short, mostly letters, and may be ALL CAPS or Title Case
        is_heading = (
            len(cleaned) < 50
            and cleaned in _HEADING_MAP
            and (stripped.isupper() or stripped.istitle() or stripped.endswith(":") or len(stripped.split()) <= 4)
        )

        if not is_heading and len(cleaned) < 40:
            # Also match if it's just the section name (possibly with extra whitespace/symbols)
            for heading_text, section_name in _HEADING_MAP.items():
                if cleaned == heading_text:
                    is_heading = True
                    break

        if is_heading:
            section_name = _HEADING_MAP.get(cleaned, cleaned)
            current_section = section_name
            if current_section not in sections:
                sections[current_section] = []
        else:
            if current_section not in sections:
                sections[current_section] = []
            sections[current_section].append(stripped)

    return {k: "\n".join(v).strip() for k, v in sections.items()}


# ══════════════════════════════════════════════════════════════════════════════
# Basic Info Extraction
# ══════════════════════════════════════════════════════════════════════════════

# Shared / role mailboxes — a consultancy's header ("resumes@vendor.com"), a
# company's HR address, a referee's office inbox. Never the candidate. The
# email is the candidate's identity in the Resume Database (a re-upload with
# the same email replaces the older record), so picking one of these would
# merge different people and delete all but one of them.
_ROLE_MAILBOX = re.compile(
    r"(?:info|hr|careers?|jobs?|recruit\w*|hiring|talent\w*|no-?reply|do-?not-?reply|"
    r"support|admin|contact|enquir\w*|resumes?|cv|placements?|sales|office|mail)"
)


def is_role_mailbox(email: Optional[str]) -> bool:
    """True for a shared inbox (hr@, careers@, resumes@, noreply@ ...)."""
    return bool(email) and "@" in email and bool(_ROLE_MAILBOX.fullmatch(email.split("@", 1)[0].strip().lower()))


def extract_email(text: str) -> Optional[str]:
    """Extract the candidate's email address from text: the first address,
    skipping role mailboxes (hr@, careers@, noreply@ ...) when the text also
    has a personal one.

    Tolerates whitespace inserted around `@` or `.` by PDF text extractors
    (common with Canva / InDesign exports — e.g. ``foo @gmail.com``).
    """
    if not text:
        return None
    # Bounded runs (RFC limits: 64-char local part, 63-char labels), and only
    # the text around each "@" is searched. Unbounded, every start position in
    # a long run scanned to its end — quadratic, 8 s for one 16,000-character
    # token; bounded but run over the whole text it was still ~26 µs a char.
    pattern = r"[a-zA-Z0-9._%+\-]{1,64}\s*@\s*[a-zA-Z0-9\-]{1,63}(?:\s*\.\s*[a-zA-Z0-9\-]{1,63})+"
    first_role = None
    for tried, at in enumerate(re.finditer("@", text)):
        if tried >= 50:  # a real resume has a handful of "@"s
            break
        start = max(0, at.start() - 100)
        window = text[start: at.start() + 300]
        pos = at.start() - start
        # The address around THIS "@" — the window also reaches back over the
        # previous one, which must not be found again.
        match = next((m for m in re.finditer(pattern, window) if m.start() <= pos < m.end()), None)
        if not match:
            continue
        email = re.sub(r"\s+", "", match.group(0)).lower()
        if not re.search(r"\.[a-zA-Z]{2,}$", email):
            continue
        if not is_role_mailbox(email):
            return email
        first_role = first_role or email
    # Only role mailboxes: returned for callers that just need a contact
    # address; the Resume Database refuses them as identity (is_role_mailbox).
    return first_role


def extract_phone(text: str) -> Optional[str]:
    """Extract phone number from text."""
    patterns = [
        r"(?:\+91[\s\-]{0,2})?[6-9]\d{4}[\s\-]?\d{5}",       # Indian mobile (+91- 8746075567 has dash+space)
        r"(?:\+1[\s\-]?)?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4}",  # US/Canada
        r"(?:\+\d{1,3}[\s\-]?)?\d{5,15}",                    # International
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            phone = re.sub(r"[\s\-()]", "", match.group(0))
            if len(phone) >= 10:
                return phone
    return None


def extract_social_links(text: str) -> dict:
    """Extract professional social media / portfolio links."""
    links: dict[str, Optional[str]] = {
        "linkedin": None,
        "github": None,
        "portfolio": None,
        "other": [],
    }

    # LinkedIn
    linkedin = re.search(
        r"(?:https?://)?(?:www\.)?linkedin\.com/in/[a-zA-Z0-9\-_%]+/?",
        text, re.IGNORECASE
    )
    if linkedin:
        url = linkedin.group(0)
        links["linkedin"] = url if url.startswith("http") else f"https://{url}"

    # GitHub
    github = re.search(
        r"(?:https?://)?(?:www\.)?github\.com/[a-zA-Z0-9\-_]+/?",
        text, re.IGNORECASE
    )
    if github:
        url = github.group(0)
        links["github"] = url if url.startswith("http") else f"https://{url}"

    # Portfolio / personal websites (common patterns)
    portfolio_patterns = [
        # Label bounded to 63 chars (the DNS limit): unbounded, every start
        # position in a long letter/digit run scanned to its end (quadratic).
        r"(?:https?://)?(?:www\.)?[a-zA-Z0-9\-]{1,63}\.(?:dev|io|me|com|net|org)/?\b",
        r"(?:portfolio|website|blog)\s*[:\-]?\s*(https?://[^\s]+)",
    ]
    for p in portfolio_patterns:
        match = re.search(p, text, re.IGNORECASE)
        if match:
            url = match.group(1) if match.lastindex else match.group(0)
            # Skip known non-portfolio domains
            skip = ["linkedin.com", "github.com", "gmail.com", "yahoo.com",
                     "outlook.com", "hotmail.com", "google.com", "facebook.com",
                     "twitter.com", "instagram.com", "naukri.com", "foundit.in",
                     "indeed.com", "monster.com"]
            if not any(d in url.lower() for d in skip):
                if not links["portfolio"]:
                    links["portfolio"] = url if url.startswith("http") else f"https://{url}"

    # Other professional links (Stack Overflow, Behance, Dribbble, Medium)
    other_domains = [
        (r"(?:https?://)?(?:www\.)?stackoverflow\.com/users/[^\s]+", "stackoverflow"),
        (r"(?:https?://)?(?:www\.)?behance\.net/[a-zA-Z0-9\-_]+", "behance"),
        (r"(?:https?://)?(?:www\.)?dribbble\.com/[a-zA-Z0-9\-_]+", "dribbble"),
        (r"(?:https?://)?(?:www\.)?medium\.com/@?[a-zA-Z0-9\-_]+", "medium"),
        (r"(?:https?://)?(?:www\.)?kaggle\.com/[a-zA-Z0-9\-_]+", "kaggle"),
    ]
    for pattern, _ in other_domains:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            url = match.group(0)
            links["other"].append(url if url.startswith("http") else f"https://{url}")

    return links


# ══════════════════════════════════════════════════════════════════════════════
# Name Extraction
# ══════════════════════════════════════════════════════════════════════════════

# Common non-name first words in resume lines
_NAME_BLACKLIST = {
    "contact", "contacts", "contact details", "contact info", "contact information",
    "resume", "cv", "curriculum vitae", "profile", "profile summary",
    "summary", "objective", "about", "about me", "personal", "personal info",
    "personal information", "personal details", "details", "information",
    "skills", "experience", "education", "work experience", "professional",
    "professional summary", "professional experience", "professional profile",
    "career objective", "career summary", "career profile", "career highlights",
    "bio", "biodata", "bio data", "address", "email", "core competencies",
    "phone", "mobile", "telephone", "references", "declaration", "hobbies",
    "interests", "languages", "achievements", "certifications", "projects",
    "portfolio", "strengths", "technical skills", "soft skills", "fresher",
    "key skills", "areas of expertise", "competencies", "qualifications",
    "academic details", "academic qualifications", "training", "internship",
    "work history", "employment history", "employment", "professional skills",
    "table of contents", "cover letter", "appendix", "annexure",
    "mission statement", "foundit", "naukri", "indeed",
    "technical support", "customer support", "it support",
    "summary experience", "india experience", "job objective",
    "contact experience", "resume summary", "mission statement",
    "domain expertise", "software expertise", "skills domain expertise",
    "skills domain expertise software expertise",
    "automation and configuration", "automation and configuration management",
    "core competencies", "technical proficiency", "it skills",
    "tools and technologies", "tools & technologies",
    # Common garbage names
    "me projects", "my projects", "experience summary", "work summary",
    "professional details", "job details", "project details",
    "technical summary", "technical overview", "career history",
    "employment details", "job history", "work details",
    "additional information", "other information", "extra information",
    "personal statement", "personal summary", "professional overview",
    "candidate profile", "applicant profile", "job seeker",
}

# Words that are NEVER part of a real name — strip from start/end of candidate lines
_NAME_NOISE_PREFIXES = {
    "personal", "contact", "resume", "cv", "profile", "original", "foundit",
    "naukri", "indeed", "mr", "ms", "mrs", "dr", "prof", "shri", "smt",
    "skills", "domain", "expertise", "software", "automation", "configuration",
    "management", "and",
}

_NAME_NOISE_SUFFIXES = {
    "original", "resume", "cv", "profile", "address", "contact",
    "details", "information", "info", "personal", "management",
    "configuration", "expertise", "domain", "skills",
}

_TITLE_WORDS = {
    "engineer", "developer", "architect", "manager", "analyst", "designer",
    "consultant", "scientist", "officer", "coordinator", "administrator",
    "executive", "specialist", "technician", "intern", "trainee",
    "lead", "head", "director", "president", "supervisor", "associate",
    "assistant", "accountant", "recruiter", "instructor", "professor",
    "teacher", "nurse", "doctor", "pharmacist", "therapist", "auditor",
    "planner", "strategist", "researcher", "programmer", "tester",
    "senior", "junior", "principal", "staff", "chief", "full-stack",
    "fullstack", "frontend", "front-end", "backend", "back-end",
    "devops", "devsecops", "sre",
}


def _is_job_title(text: str) -> bool:
    """Check if a string looks like a job title rather than a person's name."""
    words_lower = text.lower().strip().split()
    for w in words_lower:
        if w in _TITLE_WORDS:
            return True
    for dp in DESIGNATION_PATTERNS:
        if re.search(dp, text, re.IGNORECASE):
            return True
    return False


def _clean_name_line(line: str) -> str:
    """Strip noise prefixes/suffixes and known patterns from a name candidate."""
    # Remove "Foundit/Naukri/Indeed <name> Profile/Resume/CV/Original"
    cleaned = re.sub(r"^(?:Foundit|Naukri|Indeed)\s+", "", line, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+(?:Profile|Resume|CV|Original)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    # Remove dots in names like "Akshat.Mandal" → "Akshat Mandal"
    if "." in cleaned and "@" not in cleaned:
        cleaned = re.sub(r"\.(?=[A-Z])", " ", cleaned)

    # Strip noise prefixes (word by word from the left)
    words = cleaned.split()
    while words and words[0].lower().rstrip(".:,") in _NAME_NOISE_PREFIXES:
        words.pop(0)
    # Strip noise suffixes (word by word from the right)
    while words and words[-1].lower().rstrip(".:,") in _NAME_NOISE_SUFFIXES:
        words.pop()

    return " ".join(words).strip()


def _is_valid_name(text: str) -> bool:
    """Check if a cleaned string looks like a plausible person name."""
    if not text or len(text) < 3 or len(text) > 50:
        return False

    text_lower = text.lower().strip()
    if text_lower in _NAME_BLACKLIST or text_lower.rstrip(":") in _NAME_BLACKLIST:
        return False

    # Reject lines with email, URL, phone, or many non-alpha chars
    if "@" in text or "http" in text.lower() or re.search(r"\d{5,}", text):
        return False
    if re.match(r"^\+?\d[\d\s\-()]{8,}", text):
        return False
    if text.startswith("-") or text.startswith("•") or text.endswith(".") or text.endswith(":"):
        return False
    if any(ch.isdigit() for ch in text):
        return False
    if len(re.findall(r"[^a-zA-Z\s.''\-]", text)) > 1:
        return False

    # Must look like a job title → reject
    if _is_job_title(text):
        return False

    # Check it's also not a section heading
    cleaned_for_heading = re.sub(r"[:\-–—|#*=_]", "", text).strip().lower()
    if cleaned_for_heading in _HEADING_MAP:
        return False

    words = [w for w in text.split() if w]
    # Remove salutations
    words = [w for w in words if w.lower().rstrip(".") not in {"mr", "ms", "mrs", "dr", "prof", "shri", "smt"}]
    if not (2 <= len(words) <= 5):
        return False
    if not all(re.match(r"^[A-Za-z.''\-]+$", w) for w in words):
        return False

    # At least the first word must start with uppercase, and at least 1 more should too
    # (relaxed: some people write "Abhilash jain" — first word uppercase is enough if 2 words)
    upper_count = sum(1 for w in words if w[0].isupper())
    if upper_count == 0:
        return False
    if len(words) >= 3 and upper_count < 2:
        return False

    # Each word should be short-ish (real name words are rarely >15 chars)
    if any(len(w) > 18 for w in words):
        return False

    # Reject company-like names (words like "technologies", "solutions", "pvt", "ltd")
    _company_words = {"technologies", "technology", "solutions", "services", "consulting",
                       "consultants", "enterprises", "systems", "software", "infotech",
                       "infosys", "infosolutions", "pvt", "ltd", "limited", "inc",
                       "corp", "corporation", "llc", "llp", "private", "global",
                       "international", "digital", "labs", "studio", "group",
                       "livnsense", "foundit", "naukri"}
    if any(w.lower() in _company_words for w in words):
        return False

    # Reject if text contains "experience" or "summary" (section headers)
    if any(w.lower() in {"experience", "summary", "projects", "objective",
                          "qualification", "qualifications", "competencies"}
           for w in words):
        return False

    return True


def extract_name(text: str) -> Optional[str]:
    """
    Extract candidate name from resume text.
    Strategy:
    1. Check for explicit "Name:" label
    2. Scan first ~15 lines for name-like text after cleaning noise words
    """
    # Step 1: Check for explicit "Name: <value>" pattern
    name_label = re.search(
        r"(?:^|\n)\s*(?:name|full\s*name|candidate\s*name)\s*[:\-|]\s*([A-Za-z][A-Za-z\s.''\-]{2,50})",
        text[:1500], re.IGNORECASE
    )
    if name_label:
        val = _clean_name_line(name_label.group(1).strip())
        if _is_valid_name(val):
            return val.title()

    # Step 2: Scan first 15 non-empty lines for a name-like line
    lines = text.strip().split("\n")[:20]
    candidates = []

    for i, raw_line in enumerate(lines):
        raw_line = raw_line.strip()
        if not raw_line or len(raw_line) < 3 or len(raw_line) > 80:
            continue

        cleaned = _clean_name_line(raw_line)
        if not cleaned:
            continue

        if not _is_valid_name(cleaned):
            continue

        words = [w for w in cleaned.split() if w]
        words = [w for w in words if w.lower().rstrip(".") not in {"mr", "ms", "mrs", "dr", "prof", "shri", "smt"}]
        name = " ".join(words)

        if name.lower() in _NAME_BLACKLIST:
            continue

        # Score: earlier lines + 2-3 word names preferred
        score = 100 - i * 5
        if len(words) in (2, 3):
            score += 20
        # Bonus if all words are title-case (common for names)
        if all(w[0].isupper() for w in words):
            score += 10

        candidates.append((score, name))

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1].title()
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Skills Extraction
# ══════════════════════════════════════════════════════════════════════════════

def extract_skills(text: str, skills_section: str = "") -> list[str]:
    """Extract skills by matching against the common skills dictionary.
    Prioritize skills section text, but also scan full resume.
    Handles parenthetical formats like 'Web Development (JavaScript, React js)'.
    """
    # Combine skills section (if found) with full text
    search_text = (skills_section + "\n" + text).lower()
    found = set()

    # Step 1: Match against COMMON_SKILLS dictionary — as whole words. A plain
    # substring test found "consul" in "consulting", "excel" in "excellent",
    # "java" in "javascript". The substring test stays as a cheap pre-filter.
    for skill in COMMON_SKILLS:
        if skill in search_text and _skill_pattern(skill).search(search_text):
            found.add(skill)

    # Step 2: Extract items from parenthetical/bracket patterns
    # e.g., "Web Development (JavaScript, React js, Next js, MYSQL)"
    paren_pattern = r"\([^)]{5,200}\)"
    for match in re.finditer(paren_pattern, search_text):
        inner = match.group(0)[1:-1]  # strip parens
        items = re.split(r"[,;|/]", inner)
        for item in items:
            item = item.strip().strip("-").strip("*").strip()
            if 1 < len(item) < 35:
                # Check if this item matches a known skill
                for skill in COMMON_SKILLS:
                    if skill == item.lower() or item.lower() == skill:
                        found.add(skill)
                        break
                else:
                    # Also add as-is if short and clean
                    if len(item.split()) <= 3 and re.match(r"^[A-Za-z0-9#+.\-/ ]+$", item):
                        found.add(item.lower().strip())

    # Step 3: Extract skill-like items from skills section (comma/pipe/bullet separated)
    if skills_section:
        items = re.split(r"[,|•·▪►\uf0b7\n]", skills_section)
        for item in items:
            item = item.strip().strip("-").strip("*").strip()
            # Also split out parenthetical content
            if "(" in item:
                # "Web Development (JS, React)" → extract both the label and inner items
                outer = re.sub(r"\([^)]*\)", "", item).strip()
                inner_match = re.search(r"\(([^)]+)\)", item)
                sub_items = [outer]
                if inner_match:
                    sub_items.extend(re.split(r"[,;|/]", inner_match.group(1)))
                for si in sub_items:
                    si = si.strip()
                    if 1 < len(si) < 35 and len(si.split()) <= 4:
                        found.add(si.lower())
            elif 2 < len(item) < 35 and not any(c.isdigit() for c in item):
                if len(item.split()) <= 3:
                    found.add(item.lower())

    # Step 4: Clean up found skills — strip trailing/leading punctuation
    cleaned_found = set()
    for s in found:
        # Strip punctuation junk from edges
        s = re.sub(r"^[\s\(\[\{,;|/\-*•]+", "", s)
        s = re.sub(r"[\s\)\]\},;|/\-*•.]+$", "", s)
        s = s.strip()
        # If item contains ":" it's likely a label like "Cloud: AWS" — split and take items after colon
        if ":" in s:
            parts = s.split(":", 1)
            after = parts[1].strip()
            if after:
                for sub in re.split(r"[,;|/]", after):
                    sub = sub.strip()
                    if sub and len(sub) < 35:
                        cleaned_found.add(sub.lower())
            # Also add the part before colon if it's a known skill
            before = parts[0].strip().lower()
            if before in COMMON_SKILLS:
                cleaned_found.add(before)
        elif s:
            cleaned_found.add(s.lower())

    # Step 5: Validate and normalize — STRICT filtering
    display = []
    seen_lower = set()

    # Noise phrases to skip
    _skill_noise = {".", "-", ",", "and", "or", "the", "in", "of", "for", "with",
                     "etc", "using", "like", "such", "including", "proficiency",
                     "proficient", "knowledge", "expertise", "experience",
                     "understanding", "familiarity", "working", "hands-on",
                     "good", "strong", "excellent", "basic", "advanced",
                     "intermediate", "beginner", "expert", "spoken", "written",
                     "ms word", "ms excel", "powerpoint", "email", "internet",
                     "english proficiency", "english proficiency [spoken]",
                     "english proficiency [written]",
                     "web development", "web designing", "mobile development",
                     "software development", "application development",
                     "team building & leadership", "team building",
                     "technical reporting", "technical knowledge sharing",
                     "software architecture and planning",
                     "application & production support",
                     "containerization and orchestration",
                     "database administration",
                     "cbse board", "cbse", "board", "school",
                     "college", "university", "institute",
                     }

    for s in sorted(cleaned_found):
        s_lower = s.lower().strip()
        if s_lower in seen_lower or s_lower in _skill_noise:
            continue
        if len(s_lower) < 2:
            continue
        # Skip items that contain brackets/parens (leftover noise)
        if re.search(r"[\[\](){}]", s_lower):
            continue
        # Skip items longer than 35 chars (not a skill)
        if len(s_lower) > 35:
            continue
        # ── STRICT VALIDATION FILTERS ──
        if not _is_valid_skill(s_lower):
            continue
        seen_lower.add(s_lower)
        if s.upper() == s or len(s) <= 4:
            display.append(s.upper() if len(s) <= 4 and s.isalpha() else s.title())
        else:
            display.append(s.title())
    return display[:50]  # Cap at 50 skills


# ── Skill validation ─────────────────────────────────────────────────────────

_SKILL_PATTERNS: dict = {}


def _skill_pattern(skill: str) -> re.Pattern:
    """Whole-word match; longer names may carry a plural, version or "js"
    suffix ("audits", "python3", "reactjs")."""
    pattern = _SKILL_PATTERNS.get(skill)
    if pattern is None:
        suffix = r"(?:js|\d{1,2}|e?s)?" if len(skill) > 3 else ""
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(skill) + suffix + r"(?![a-z0-9])")
        _SKILL_PATTERNS[skill] = pattern
    return pattern


# Pieces of sentences and tables that the section/bracket splitting picks up.
_DURATION_RE = re.compile(r"\d+(?:\.\d+)?\s*\+?\s*(?:years?|yrs?|months?|mos?|weeks?|days?)\b")
_LEADING_JOINER_RE = re.compile(r"^(?:and|or|&|of|in|to|with|for|the|a|an|at|by)\b")
_NOT_SKILLS = {
    "present employer", "current employer", "previous employer", "present", "current", "till date",
    "to date", "ongoing", "full time", "part time", "internship", "fresher", "notice period",
}


def is_junk_skill(value: str) -> bool:
    """A duration, a sentence fragment or a table cell, not a skill
    ("2.5+ Years", "And Cf", "Present Employer")."""
    s = (value or "").strip().lower()
    return bool(_DURATION_RE.search(s) or _LEADING_JOINER_RE.match(s) or s in _NOT_SKILLS)

# Indian cities/states that get extracted as "skills"
_LOCATION_WORDS = {
    "mumbai", "delhi", "bangalore", "bengaluru", "hyderabad", "chennai", "kolkata",
    "pune", "ahmedabad", "jaipur", "lucknow", "kanpur", "nagpur", "indore", "thane",
    "bhopal", "visakhapatnam", "pimpri", "patna", "vadodara", "ghaziabad", "ludhiana",
    "agra", "nashik", "faridabad", "meerut", "rajkot", "varanasi", "srinagar",
    "aurangabad", "dhanbad", "amritsar", "allahabad", "ranchi", "howrah", "coimbatore",
    "jabalpur", "gwalior", "vijayawada", "jodhpur", "madurai", "raipur", "kota",
    "chandigarh", "guwahati", "solapur", "hubli", "mysore", "tiruchirappalli",
    "bareilly", "aligarh", "tiruppur", "moradabad", "jalandhar", "bhubaneswar",
    "salem", "warangal", "guntur", "bhiwandi", "saharanpur", "gorakhpur", "bikaner",
    "amravati", "noida", "jamshedpur", "bhilai", "cuttack", "firozabad", "kochi",
    "nellore", "bhavnagar", "dehradun", "durgapur", "asansol", "rourkela", "nanded",
    "kolhapur", "ajmer", "loni", "siliguri", "jhansi", "ulhasnagar", "navi mumbai",
    "jammu", "sangli", "mangalore", "erode", "belgaum", "tirunelveli", "malegaon",
    "gaya", "udaipur", "maheshtala", "davanagere", "kozhikode", "kurnool", "rajpur",
    "rajahmundry", "bokaro", "south dumdum", "bellary", "patiala", "gopalpur",
    "agartala", "bhagalpur", "muzaffarnagar", "bhatpara", "panihati", "latur",
    "dhule", "tirupati", "rohtak", "korba", "bhilwara", "berhampur", "muzaffarpur",
    "ahmednagar", "mathura", "kollam", "avadi", "kadapa", "kamarhati", "sambalpur",
    "bilaspur", "shahjahanpur", "satara", "bijapur", "rampur", "shimoga", "chandrapur",
    "junagadh", "thrissur", "alwar", "bardhaman", "kulti", "nizamabad", "parbhani",
    "tumkur", "khammam", "ozhukarai", "bihar", "sharif", "panipat", "darbhanga",
    "bally", "aizawl", "dewas", "ichalkaranji", "karnal", "bathinda", "jalna",
    "eluru", "kirari", "sulaiman", "barasat", "purnia", "satna", "mau",
    "sonipat", "farrukhabad", "sagar", "rourkela", "durg", "imphal", "ratlam",
    "hapur", "arrah", "karimnagar", "etawah", "ambattur", "north dumdum", "bharatpur",
    "begusarai", "new delhi", "gurugram", "gurgaon", "greater noida",
    # States
    "maharashtra", "karnataka", "tamil nadu", "telangana", "uttar pradesh",
    "west bengal", "rajasthan", "gujarat", "madhya pradesh", "kerala",
    "andhra pradesh", "odisha", "jharkhand", "chhattisgarh", "assam",
    "haryana", "punjab", "uttarakhand", "himachal pradesh", "tripura",
    "meghalaya", "manipur", "nagaland", "goa", "arunachal pradesh",
    "mizoram", "sikkim", "india", "indian",
    # Countries
    "usa", "uk", "canada", "australia", "germany", "singapore", "dubai", "uae",
}

# Education terms that are NOT skills
_EDUCATION_WORDS = {
    "bachelor", "bachelors", "master", "masters", "phd", "doctorate", "diploma",
    "degree", "btech", "b.tech", "b tech", "mtech", "m.tech", "m tech",
    "bsc", "b.sc", "msc", "m.sc", "bca", "mca", "bba", "mba",
    "b.com", "bcom", "m.com", "mcom", "b.e", "m.e", "b.a", "m.a",
    "pgdm", "pgdca", "10th", "12th", "sslc", "hsc", "ssc",
    "cbse", "icse", "board", "school", "college", "university", "institute",
    "graduation", "post graduation", "postgraduation", "undergraduate",
    "graduate", "postgraduate", "education", "academic", "semester",
    "matriculation", "intermediate", "higher secondary", "secondary",
    "bachelor of science", "bachelor of arts", "bachelor of technology",
    "bachelor of engineering", "master of science", "master of technology",
    "master of business administration", "bachelor of computer application",
    "master of computer application",
}

# Hobbies / non-technical items
_HOBBY_WORDS = {
    "reading", "writing", "travelling", "traveling", "music", "cooking", "dancing",
    "singing", "painting", "drawing", "photography", "gardening", "swimming",
    "cricket", "football", "badminton", "chess", "yoga", "meditation",
    "hiking", "trekking", "cycling", "bike riding", "running", "jogging",
    "watching movies", "listening music", "playing games", "blogging",
    "volunteering", "social work", "sports", "fitness", "gym",
}

# Generic/meaningless words that appear in resume sections
_GENERIC_NOISE = {
    "declaration", "hereby", "declare", "passport", "nationality", "gender",
    "male", "female", "date of birth", "dob", "marital status", "married",
    "unmarried", "single", "father", "mother", "husband", "wife",
    "permanent address", "current address", "present address", "address",
    "reference", "references", "referee", "available on request",
    "designation", "company", "organization", "organisation",
    "project", "projects", "me projects", "my projects",
    "dispatcher", "barcode generator", "blue dart",
    "e.g", "e.g.", "i.e", "i.e.", "etc", "etc.",
    "act", "new", "dark", "dart", "present", "current",
    "communication", "communication skill", "communication skills",
    "problem solving", "team work", "teamwork", "leadership",
    "time management", "analytical", "critical thinking",
    "attention to detail", "decision making", "creativity",
    "adaptability", "flexibility", "multitasking", "self motivated",
    "quick learner", "hard working", "hardworking", "punctual",
    "honest", "sincere", "dedicated", "responsible", "confident",
    # Languages (not technical skills)
    "english", "hindi", "telugu", "tamil", "kannada", "malayalam",
    "marathi", "gujarati", "bengali", "punjabi", "urdu", "odia",
    "assamese", "sanskrit", "french", "german", "spanish", "japanese",
    "chinese", "korean", "arabic", "portuguese", "italian", "russian",
    "read", "write", "speak", "spoken", "written", "fluent",
    "mother tongue", "native", "proficient", "intermediate", "basic",
    "languages known", "language skills", "linguistic skills",
}

# Date patterns
_DATE_RE = re.compile(
    r"^(?:\d{1,2}[-/.]?\s*)?(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"[\s,.''-]*\d{2,4}",
    re.IGNORECASE,
)


def _is_valid_skill(s: str) -> bool:
    """Return True only if s looks like a real technical skill."""
    s = s.strip().lower()

    # ── Durations, sentence fragments, table cells ──
    if _DURATION_RE.search(s) or _LEADING_JOINER_RE.match(s) or s in _NOT_SKILLS:
        return False

    # ── Pure numbers / date-like ──
    if re.match(r"^\d+$", s):
        return False
    if re.match(r"^\d{4}\s*[-–—to]\s*(?:\d{4}|present|current)", s, re.IGNORECASE):
        return False
    if re.match(r"^\d{2,4}\s*[-–—/.]\s*\w+\s*\d{2,4}$", s):
        return False
    if _DATE_RE.match(s):
        return False
    # Strings that are just "YYYY" years
    if re.match(r"^(19|20)\d{2}$", s):
        return False
    # "2016- April 2024" pattern
    if re.match(r"^\d{4}\s*[-–]\s*\w+", s) and any(m in s for m in [
        "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
        "present", "current", "till",
    ]):
        return False
    # Any string starting with a year range
    if re.match(r"^\d{4}\s*[-–—to]\s*\d{4}$", s):
        return False
    # Short pure digits (page numbers, list numbers)
    if re.match(r"^\d{1,3}$", s):
        return False

    # ── Location / city check ──
    if s in _LOCATION_WORDS:
        return False
    # Multi-word locations
    for loc in _LOCATION_WORDS:
        if s == loc or (len(s) > 4 and s in loc):
            return False

    # ── Education terms ──
    if s in _EDUCATION_WORDS:
        return False
    # Patterns like "F.M University Odisha", "Acharya Nagarjuna University"
    if "university" in s or "college" in s or "institute" in s or "academy" in s:
        return False
    if re.match(r"^(?:bachelor|master|diploma|degree)\b", s):
        return False

    # ── Hobbies ──
    if s in _HOBBY_WORDS:
        return False

    # ── Generic noise ──
    if s in _GENERIC_NOISE:
        return False

    # ── Single very short non-standard tokens ──
    if len(s) == 1:
        return False

    # ── Language proficiency patterns ──
    _languages = {"english", "hindi", "telugu", "tamil", "kannada", "malayalam",
                  "marathi", "gujarati", "bengali", "punjabi", "urdu", "odia",
                  "french", "german", "spanish", "japanese", "chinese", "korean",
                  "arabic", "portuguese", "italian", "russian", "sanskrit"}
    # Check if skill is or starts with a language name
    for lang in _languages:
        if s == lang or s.startswith(lang + " ") or s.startswith(lang + "("):
            return False

    # ── Contains "???" or other obvious garbage ──
    if "???" in s or "..." in s:
        return False

    # ── Mostly digits (like "405b instruct", "70b") — ML model size refs ──
    digits = sum(1 for c in s if c.isdigit())
    if digits > 0 and digits / len(s) > 0.5 and s not in COMMON_SKILLS:
        return False

    # ── Unless it's a known skill, reject if it's a single non-alpha-only token < 3 chars ──
    if len(s) <= 2 and s not in COMMON_SKILLS:
        return False

    return True


# ══════════════════════════════════════════════════════════════════════════════
# Experience Extraction
# ══════════════════════════════════════════════════════════════════════════════

def extract_experience_years(text: str) -> Optional[float]:
    """Extract years of experience from text."""
    # The number is bounded (1-2 digits, optional 1-2 decimals) and may not
    # start mid-number. The old `(\d+\.?\d*)` could split a digit run in O(n^2)
    # ways at each of n start positions: a resume containing 1,000 digits took
    # 44 s and 10,000 would take hours, freezing whichever worker parsed it.
    # Only 0 < years <= 50 is ever accepted, so nothing valid is lost.
    num = r"(?<![\d.])(\d{1,2}(?:\.\d{1,2})?)"
    patterns = [
        num + r"\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp)",
        r"(?:experience|exp)\s*(?::|of)\s*" + num + r"\+?\s*(?:years?|yrs?)",
        r"(?:total|overall)\s+(?:experience|exp)\s*(?::|of)?\s*" + num + r"\+?\s*(?:years?|yrs?)",
        num + r"\+?\s*(?:years?|yrs?)\s+(?:in\s+)?(?:IT|software|development|engineering|industry)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                years = float(match.group(1))
                if 0 < years <= 50:
                    return years
            except (ValueError, IndexError):
                continue
    return None


# Date pattern for work experience entries
_DATE_PATTERN = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s*[''']?\s*\d{2,4}"
_DATE_RANGE_PATTERN = rf"({_DATE_PATTERN})\s*(?:[-–—to]+|to)\s*({_DATE_PATTERN}|[Pp]resent|[Cc]urrent|[Tt]ill\s+[Dd]ate|[Oo]ngoing)"
_YEAR_RANGE_PATTERN = r"(\d{4})\s*(?:[-–—to]+|to)\s*(\d{4}|[Pp]resent|[Cc]urrent|[Tt]ill\s+[Dd]ate|[Oo]ngoing)"


def extract_work_experience(text: str, experience_section: str = "") -> list[dict]:
    """
    Extract structured work experience entries.
    Each entry: {title, company, start_date, end_date, duration, description}
    """
    if not experience_section:
        return []

    entries = []
    lines = experience_section.split("\n")

    current_entry: Optional[dict] = None
    description_lines: list[str] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Check if line contains a date range (signals new entry)
        date_match = re.search(_DATE_RANGE_PATTERN, line, re.IGNORECASE)
        if not date_match:
            date_match = re.search(_YEAR_RANGE_PATTERN, line)

        if date_match:
            # Save previous entry
            if current_entry:
                current_entry["description"] = " ".join(description_lines).strip()[:500]
                entries.append(current_entry)
                description_lines = []

            start = date_match.group(1).strip()
            end = date_match.group(2).strip()

            # Extract title and company from surrounding text
            rest = line[:date_match.start()].strip().rstrip("-–—|").strip()
            if not rest:
                rest = line[date_match.end():].strip().lstrip("-–—|").strip()

            # Clean the rest text
            rest = re.sub(r"\s+", " ", rest).strip()
            title, company = _parse_title_company(rest)

            # Skip entries where title is a section heading
            if title and title.lower().strip() in _HEADING_MAP:
                title = None

            current_entry = {
                "title": title,
                "company": company,
                "start_date": start,
                "end_date": end,
                "duration": None,
                "description": "",
            }
        elif current_entry:
            # This line is part of the current entry description
            # But first check if it might be a title or company line
            if not current_entry.get("title") and _looks_like_job_title(line):
                current_entry["title"] = line.strip()
            elif not current_entry.get("company") and _looks_like_company(line):
                current_entry["company"] = line.strip()
            else:
                description_lines.append(line)
        else:
            # Before any date found — might be first entry without clear dates
            # Check if this looks like a job title followed by company
            if _looks_like_job_title(line):
                current_entry = {
                    "title": line.strip(),
                    "company": None,
                    "start_date": None,
                    "end_date": None,
                    "duration": None,
                    "description": "",
                }
                description_lines = []

    # Save last entry
    if current_entry:
        current_entry["description"] = " ".join(description_lines).strip()[:500]
        entries.append(current_entry)

    return entries[:10]  # Cap at 10 entries


def _parse_title_company(text: str) -> tuple[Optional[str], Optional[str]]:
    """Try to split a line into job title and company name."""
    if not text:
        return None, None

    # Common separators: at, @, |, –, -, ","
    for sep in [" at ", " @ ", " | ", " – ", " - ", ", "]:
        if sep in text:
            parts = text.split(sep, 1)
            return parts[0].strip(), parts[1].strip()

    # If no separator, treat entire text as title
    return text.strip(), None


def _looks_like_job_title(text: str) -> bool:
    """Check if text looks like a job title."""
    text_lower = text.lower().strip()
    title_words = {"engineer", "developer", "analyst", "manager", "designer",
                   "consultant", "architect", "scientist", "lead", "director",
                   "specialist", "coordinator", "administrator", "executive",
                   "officer", "intern", "trainee"}
    return any(w in text_lower for w in title_words)


def _looks_like_company(text: str) -> bool:
    """Check if text looks like a company name."""
    company_indicators = {"pvt", "ltd", "llc", "inc", "corp", "limited",
                          "solutions", "technologies", "services", "systems",
                          "consulting", "software", "group", "labs", "tech"}
    return any(w in text.lower() for w in company_indicators)


# ══════════════════════════════════════════════════════════════════════════════
# Education Extraction
# ══════════════════════════════════════════════════════════════════════════════

def extract_education(text: str, education_section: str = "") -> list[str]:
    """
    Extract education qualifications as structured strings.
    Returns: ["B.Tech in Computer Science, XYZ University, 2020", ...]
    """
    search_text = education_section if education_section else text
    found = []

    # Skip very short ambiguous keys when not in education section context
    _AMBIGUOUS_DEGREE_KEYS = {"ms", "me", "ma", "ba", "bs", "be"}

    # Strategy 1: Find degree mentions and try to extract institution + year nearby
    for key, label in EDUCATION_DEGREES.items():
        # Skip ambiguous 2-letter keys unless searching the education section specifically
        if key in _AMBIGUOUS_DEGREE_KEYS and not education_section:
            continue
        pattern = r"\b" + re.escape(key) + r"\b"
        for match in re.finditer(pattern, search_text, re.IGNORECASE):
            # Get surrounding context (100 chars before and after)
            start = max(0, match.start() - 20)
            end = min(len(search_text), match.end() + 150)
            context = search_text[start:end]

            # Extract field of study
            field = None
            field_match = re.search(
                r"(?:in|of)\s+([A-Za-z\s&]+?)(?:\s*[,\n\|]|\s+from|\s+at|\s*\d{4}|$)",
                context[match.start() - start:], re.IGNORECASE
            )
            if field_match:
                f = field_match.group(1).strip()
                # Must be short-ish and a plausible field
                _valid_fields = {"computer", "science", "engineering", "technology", "information",
                                 "electronics", "electrical", "mechanical", "civil", "business",
                                 "commerce", "arts", "mathematics", "physics", "chemistry",
                                 "biology", "management", "finance", "accounting", "marketing",
                                 "communication", "design", "architecture", "data", "software"}
                if len(f) < 60 and any(w in f.lower() for w in _valid_fields):
                    field = f

            # Extract institution
            institution = None
            for kw in _INSTITUTION_KEYWORDS:
                inst_match = re.search(
                    rf"([A-Za-z\s.''\-]+{kw}[A-Za-z\s.''\-]*)",
                    context, re.IGNORECASE
                )
                if inst_match:
                    institution = inst_match.group(1).strip()[:80]
                    break

            # Extract year
            year_match = re.search(r"\b(19|20)\d{2}\b", context)
            year = year_match.group(0) if year_match else None

            # Build the education string
            parts = [label]
            if field and len(field) > 2 and len(field) < 50:
                parts[0] = f"{label} in {field.title()}"
            if institution:
                parts.append(institution.title())
            if year:
                parts.append(year)

            entry = ", ".join(parts)
            # Avoid duplicates
            if not any(label in existing for existing in found):
                found.append(entry)

    # If no structured results, fall back to simple degree detection
    if not found:
        text_lower = text.lower()
        for key, label in EDUCATION_DEGREES.items():
            if key in _AMBIGUOUS_DEGREE_KEYS:
                continue  # skip ambiguous short keys in fallback
            if re.search(r"\b" + re.escape(key) + r"\b", text_lower):
                if label not in found:
                    found.append(label)

    return found[:8]


# ══════════════════════════════════════════════════════════════════════════════
# Certification Extraction
# ══════════════════════════════════════════════════════════════════════════════

_CERT_PATTERNS = [
    r"(?:AWS|Amazon)\s+(?:Certified|Solutions?\s+Architect|Developer|SysOps|DevOps|Cloud\s+Practitioner)[^\n,]*",
    r"(?:Azure|Microsoft)\s+(?:Certified|Administrator|Developer|Solutions?\s+Architect|Data\s+Engineer)[^\n,]*",
    r"(?:Google|GCP)\s+(?:Certified|Cloud|Professional|Associate)[^\n,]*",
    r"(?:PMP|Project Management Professional)[^\n,]*",
    r"(?:Certified\s+)?(?:Scrum\s+Master|CSM|PSM)[^\n,]*",
    r"(?:CISSP|CISM|CEH|CompTIA\s+Security\+?)[^\n,]*",
    r"(?:CCNA|CCNP|CCIE)[^\n,]*",
    r"(?:ITIL)\s*(?:v?\d|Foundation|Expert)?[^\n,]*",
    r"(?:Oracle\s+Certified)[^\n,]*",
    r"(?:Certified\s+Kubernetes)[^\n,]*",
    r"(?:Terraform\s+(?:Certified|Associate))[^\n,]*",
    r"(?:Docker\s+Certified)[^\n,]*",
    r"(?:Salesforce\s+(?:Certified|Administrator|Developer))[^\n,]*",
    r"(?:SAP\s+(?:Certified|Consultant))[^\n,]*",
    r"(?:Six\s+Sigma|Lean\s+Six\s+Sigma)[^\n,]*",
    r"(?:CPA|CFA|CMA|FRM)\b[^\n,]*",
    r"(?:TOGAF|CBAP|PRINCE2)[^\n,]*",
]


def extract_certifications(text: str, cert_section: str = "") -> list[str]:
    """Extract professional certifications."""
    search_text = cert_section if cert_section else text
    found = []
    seen = set()

    for pattern in _CERT_PATTERNS:
        for match in re.finditer(pattern, search_text, re.IGNORECASE):
            cert = match.group(0).strip().rstrip(",;.")
            cert_lower = cert.lower()
            if cert_lower not in seen and len(cert) > 3:
                seen.add(cert_lower)
                found.append(cert.strip())

    # Also extract from cert section line-by-line (bullet points)
    if cert_section:
        _cert_keywords = {"certified", "certification", "certificate", "licensed",
                          "accredited", "aws", "azure", "google", "oracle", "cisco",
                          "pmp", "scrum", "itil", "comptia", "six sigma", "ccna",
                          "ccnp", "ccie", "togaf", "prince2", "salesforce", "sap"}
        for line in cert_section.split("\n"):
            line = line.strip().lstrip("•·▪►-*\uf0b7\uf020").strip()
            if 5 < len(line) < 80 and line.lower() not in seen:
                if line.lower() in _HEADING_MAP:
                    continue
                # Only include if it's short enough to be a cert name (not a description)
                if len(line.split()) <= 10 and any(kw in line.lower() for kw in _cert_keywords):
                    seen.add(line.lower())
                    found.append(line)

    return found[:15]


# ══════════════════════════════════════════════════════════════════════════════
# Location & Designation Extraction
# ══════════════════════════════════════════════════════════════════════════════

_LOCATION_LABEL = re.compile(
    # A label at the start of a line (or after a separator), followed by ":" or
    # "-". The old pattern had neither anchor nor word boundary, so it fired
    # inside "allocation", "capacity", "addressing" and took the rest of the line.
    r"(?im)(?:^|[|•·,;]\s*)(?:current\s+|present\s+|permanent\s+)?"
    r"(?:location|address|city|place|residence)\s*[:\-–]\s*([^\n|]{2,160})"
)
_LOCATION_PHRASE = re.compile(
    r"(?i)\b(?:based\s+(?:out\s+of|in|at)|residing\s+(?:at|in)|resident\s+of|"
    r"currently\s+(?:based|located|living)\s+(?:in|at))\s+([A-Za-z][^\n|;]{1,100})"
)
_EMAIL_OR_URL = re.compile(r"\S+@\S+|https?://\S+|www\.\S+", re.IGNORECASE)


def _looks_like_name_line(line: str) -> bool:
    words = line.split()
    return 0 < len(words) <= 4 and not re.search(r"[\d@:,|]", line)


# Header lines that mention a city without it being where the candidate lives:
# a preference, a college ("Delhi Public School", "Pune University") or prose.
_NOT_RESIDENCE_LINE = re.compile(
    r"prefer|relocat|willing|open\s+to|school|college|universit|institute|academy|\biit\b|\bnit\b", re.IGNORECASE
)


def _could_hold_address(line: str) -> bool:
    if _NOT_RESIDENCE_LINE.search(line):
        return False
    # Contact lines are short or separated ("Noida | +91 ..."); prose is neither.
    return len(line.split()) <= 10 or bool(re.search(r"[|,•·]|\d{6}", line))


def extract_location(text: str) -> Optional[str]:
    """The candidate's city: from a "Location:"/"Address:" line or a "based in"
    phrase, else from a known city in the contact header (the first lines).
    Only known places are returned (see app.services.locations) — never a
    sentence fragment. None when the resume doesn't say."""
    if not text:
        return None
    clean = _EMAIL_OR_URL.sub(" ", text)  # "rahul.pune@gmail.com" is not a location
    for pattern in (_LOCATION_LABEL, _LOCATION_PHRASE):
        for match in pattern.finditer(clean):
            place = place_from_text(match.group(1))
            if place:
                return place
    header = [line for line in clean.split("\n") if line.strip()][:15]
    if header and _looks_like_name_line(header[0]):
        header = header[1:]  # the candidate's name ("Anand Kumar")
    for line in header:
        city = find_city(line) if _could_hold_address(line) else None
        if city:
            return city
    return None


def extract_designation(text: str) -> Optional[str]:
    """Extract current designation/title from resume."""
    first_section = "\n".join(text.split("\n")[:15])
    for pattern in DESIGNATION_PATTERNS:
        match = re.search(pattern, first_section, re.IGNORECASE)
        if match:
            return _clean_designation(match.group(0).strip().title())

    label_match = re.search(
        r"(?:designation|current\s+role|job\s+title|position|current\s+title)\s*[:\-]\s*(.+?)(?:\n|$)",
        text, re.IGNORECASE
    )
    if label_match:
        val = label_match.group(1).strip()
        if 3 < len(val) < 80:
            return _clean_designation(val.title())
    return None


def _clean_designation(title: str) -> str:
    """Clean and truncate a designation — keep only the primary role."""
    if not title:
        return title
    # Split on separators and take the first meaningful part
    parts = re.split(r"\s*[,/|&]\s*", title)
    # Keep only the first 2 parts max, each capped at reasonable length
    cleaned_parts = []
    for p in parts[:2]:
        p = p.strip()
        if len(p) > 3:
            cleaned_parts.append(p)
    result = " / ".join(cleaned_parts) if len(cleaned_parts) > 1 else (cleaned_parts[0] if cleaned_parts else title)
    # Hard cap at 60 chars
    if len(result) > 60:
        result = result[:57] + "..."
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Keywords Extraction
# ══════════════════════════════════════════════════════════════════════════════

def extract_keywords(text: str, skills: list[str], education: list[str]) -> list[str]:
    """Generate searchable keywords from the resume text."""
    keywords = set(s.lower() for s in skills)

    # Add education keywords
    for edu in education:
        for word in re.findall(r"[a-z0-9.#+]+", edu.lower()):
            if len(word) >= 3:
                keywords.add(word)

    # Add domain keywords
    domain_keywords = [
        "fintech", "healthcare", "ecommerce", "e-commerce", "saas", "b2b", "b2c",
        "banking", "insurance", "telecom", "retail", "logistics", "supply chain",
        "edtech", "hrtech", "real estate", "automotive", "manufacturing",
        "media", "entertainment", "gaming", "travel", "hospitality",
        "startup", "enterprise", "government", "non-profit",
    ]
    text_lower = text.lower()
    for kw in domain_keywords:
        if kw in text_lower:
            keywords.add(kw)

    return sorted(keywords)[:30]


# ══════════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ══════════════════════════════════════════════════════════════════════════════

def fast_parse_resume(text: str) -> dict:
    """
    Parse a resume using regex and keyword matching ONLY — no AI calls.
    Returns a dict compatible with ResumeMetadata fields.
    Designed for bulk processing of 50k+ resumes.
    """
    # Split into sections for targeted extraction
    sections = _split_sections(text)

    email = extract_email(text)
    phone = extract_phone(text)
    name = extract_name(text)
    skills = extract_skills(text, sections.get("skills", ""))
    experience = extract_experience_years(text)
    location = extract_location(text)
    education = extract_education(text, sections.get("education", ""))
    designation = extract_designation(text)
    certifications = extract_certifications(text, sections.get("certifications", ""))
    work_experience = extract_work_experience(text, sections.get("experience", ""))
    social_links = extract_social_links(text)
    keywords = extract_keywords(text, skills, education)

    # If designation not found but we have work experience, use the first title
    if not designation and work_experience:
        designation = _clean_designation(work_experience[0].get("title") or "")
    elif designation:
        designation = _clean_designation(designation)

    # Build a meaningful summary
    parts = []
    if name:
        parts.append(name)
    if designation:
        parts.append(designation)
    if experience:
        parts.append(f"{experience} years experience")
    if location:
        parts.append(f"based in {location}")
    if skills[:3]:
        parts.append("Skills: " + ", ".join(skills[:5]))
    summary = " | ".join(parts) if parts else text[:200].replace("\n", " ").strip()

    return {
        "candidate_email": email,
        "candidate_name": name,
        "phone": phone,
        "current_designation": designation,
        "experience_years": experience,
        "skills": skills,
        "education": education,
        "certifications": certifications,
        "work_experience": work_experience,
        "location": location,
        "personal_details": {"social_links": social_links} if any(v for v in social_links.values()) else {},
        "keywords": keywords,
        "summary": summary,
    }
