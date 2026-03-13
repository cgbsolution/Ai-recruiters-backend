"""
Fast regex-based resume parser for bulk processing.

Extracts metadata WITHOUT any AI/LLM calls — pure text analysis using
regex patterns and keyword matching. Designed for processing 50k+ resumes.

For AI-powered deep parsing, use resume_service.parse_resume() instead.
"""

import re
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Common skill keywords (lowercase) ────────────────────────────────────────
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
    # Tools
    "git", "github", "gitlab", "bitbucket", "jira", "confluence",
    "slack", "figma", "sketch", "adobe xd", "postman", "swagger",
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
}

# ── Designation / Title keywords ─────────────────────────────────────────────
DESIGNATION_PATTERNS = [
    r"(?:senior|sr\.?|lead|principal|staff|chief|head)\s+\w+\s*(?:engineer|developer|architect|manager|analyst|designer|consultant|scientist|officer)",
    r"(?:junior|jr\.?|associate|entry.level|trainee|intern)\s+\w+\s*(?:engineer|developer|analyst|designer)",
    r"\b(?:software|web|frontend|front-end|backend|back-end|full.?stack|mobile|devops|cloud|data|ml|ai|qa|test|security|network|system|database|ui|ux)\s+(?:engineer|developer|architect|analyst|scientist|designer|administrator|consultant)\b",
    r"\b(?:project|product|program|engineering|technical|it|operations|delivery)\s+(?:manager|director|lead|head|coordinator)\b",
    r"\b(?:cto|ceo|cfo|coo|vp|vice president|director|manager|team lead|tech lead)\b",
    r"\b(?:consultant|analyst|specialist|coordinator|administrator|executive)\b",
]

# ── Education keywords ───────────────────────────────────────────────────────
EDUCATION_PATTERNS = [
    r"\b(?:b\.?tech|b\.?e\.?|bachelor|b\.?sc|b\.?s\.?|bca|bba|b\.?com)\b",
    r"\b(?:m\.?tech|m\.?e\.?|master|m\.?sc|m\.?s\.?|mca|mba|m\.?com|m\.?phil)\b",
    r"\b(?:ph\.?d|doctorate|doctoral)\b",
    r"\b(?:diploma|certification|certificate|associate degree)\b",
    r"\b(?:computer science|information technology|electronics|electrical|mechanical|civil|chemical)\b",
]

EDUCATION_DEGREES = {
    "phd": "PhD", "doctorate": "PhD", "doctoral": "PhD",
    "mtech": "M.Tech", "m.tech": "M.Tech", "mba": "MBA",
    "mca": "MCA", "msc": "M.Sc", "m.sc": "M.Sc", "ms": "M.S",
    "master": "Master's", "m.e": "M.E", "me": "M.E",
    "mcom": "M.Com", "m.com": "M.Com", "mphil": "M.Phil",
    "btech": "B.Tech", "b.tech": "B.Tech", "be": "B.E", "b.e": "B.E",
    "bsc": "B.Sc", "b.sc": "B.Sc", "bs": "B.S", "b.s": "B.S",
    "bca": "BCA", "bba": "BBA", "bcom": "B.Com", "b.com": "B.Com",
    "bachelor": "Bachelor's", "diploma": "Diploma",
}


def extract_text_from_file(file_path: str) -> Optional[str]:
    """Extract plain text from PDF or DOCX file. Returns None on failure."""
    import os
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
        elif ext in (".doc", ".docx"):
            from docx import Document
            doc = Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs)
    except Exception as e:
        logger.error("Text extraction failed for %s: %s", file_path, e)
    return None


def extract_email(text: str) -> Optional[str]:
    """Extract the first email address from text."""
    match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    return match.group(0).lower() if match else None


def extract_phone(text: str) -> Optional[str]:
    """Extract phone number from text."""
    patterns = [
        r"(?:\+91[\s\-]?)?[6-9]\d{4}[\s\-]?\d{5}",  # Indian mobile
        r"(?:\+1[\s\-]?)?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4}",  # US/Canada
        r"(?:\+\d{1,3}[\s\-]?)?\d{5,15}",  # International
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            phone = re.sub(r"[\s\-()]", "", match.group(0))
            if len(phone) >= 10:
                return phone
    return None


def extract_name(text: str) -> Optional[str]:
    """
    Heuristic: the candidate name is usually in the first few lines.
    Look for a line that looks like a name (2-4 capitalized words, no special chars).
    """
    lines = text.strip().split("\n")[:10]
    for line in lines:
        line = line.strip()
        if not line or len(line) < 3 or len(line) > 60:
            continue
        # Skip lines that look like emails, phones, URLs, addresses
        if "@" in line or "http" in line.lower() or re.search(r"\d{5,}", line):
            continue
        # Skip lines with too many special characters
        if len(re.findall(r"[^a-zA-Z\s.''\-]", line)) > 2:
            continue
        # Check if it looks like a name: 2-4 words, mostly alphabetic
        words = line.split()
        if 1 <= len(words) <= 5:
            alpha_ratio = sum(1 for w in words if w[0].isupper() or w[0] == "'") / len(words)
            if alpha_ratio >= 0.5 and all(re.match(r"^[A-Za-z.''\-]+$", w) for w in words):
                return line.title()
    return None


def extract_skills(text: str) -> list[str]:
    """Extract skills by matching against the common skills dictionary."""
    text_lower = text.lower()
    found = set()

    for skill in COMMON_SKILLS:
        # Use word boundary matching for short skills to avoid false positives
        if len(skill) <= 3:
            if re.search(r"\b" + re.escape(skill) + r"\b", text_lower):
                found.add(skill)
        else:
            if skill in text_lower:
                found.add(skill)

    # Normalize display names
    display = []
    for s in sorted(found):
        # Capitalize nicely
        if s.upper() == s or len(s) <= 4:
            display.append(s.upper() if len(s) <= 4 else s.title())
        else:
            display.append(s.title())
    return display


def extract_experience_years(text: str) -> Optional[float]:
    """Extract years of experience from text."""
    patterns = [
        r"(\d+\.?\d*)\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp)",
        r"(?:experience|exp)\s*(?::|of)\s*(\d+\.?\d*)\+?\s*(?:years?|yrs?)",
        r"(?:total|overall)\s+(?:experience|exp)\s*(?::|of)?\s*(\d+\.?\d*)\+?\s*(?:years?|yrs?)",
        r"(\d+\.?\d*)\+?\s*(?:years?|yrs?)\s+(?:in\s+)?(?:IT|software|development|engineering|industry)",
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


def extract_location(text: str) -> Optional[str]:
    """Extract location from common patterns in resumes."""
    patterns = [
        r"(?:location|address|city|based in|residing|resident)\s*[:\-]?\s*([A-Za-z\s,]+?)(?:\n|$|\|)",
        r"(?:currently\s+(?:in|at|based))\s+([A-Za-z\s,]+?)(?:\n|$|\|)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            loc = match.group(1).strip().strip(",").strip()
            if 2 < len(loc) < 60:
                return loc.title()

    # Fallback: look for known Indian cities in first 20 lines
    indian_cities = [
        "Mumbai", "Delhi", "Bangalore", "Bengaluru", "Hyderabad", "Chennai",
        "Kolkata", "Pune", "Ahmedabad", "Jaipur", "Lucknow", "Noida",
        "Gurgaon", "Gurugram", "Chandigarh", "Indore", "Bhopal", "Kochi",
        "Coimbatore", "Trivandrum", "Thiruvananthapuram", "Nagpur", "Surat",
        "Visakhapatnam", "Patna", "Ranchi", "Bhubaneswar", "Mysore",
        "Mangalore", "Vadodara", "Nashik", "Thane", "Faridabad",
    ]
    first_section = "\n".join(text.split("\n")[:20])
    for city in indian_cities:
        if re.search(r"\b" + re.escape(city) + r"\b", first_section, re.IGNORECASE):
            return city
    return None


def extract_education(text: str) -> list[str]:
    """Extract education qualifications."""
    found = set()
    text_lower = text.lower()
    for key, label in EDUCATION_DEGREES.items():
        if re.search(r"\b" + re.escape(key) + r"\b", text_lower):
            found.add(label)
    return sorted(found)


def extract_designation(text: str) -> Optional[str]:
    """Extract current designation/title from resume."""
    # Look in first 15 lines for designation-like text
    first_section = "\n".join(text.split("\n")[:15])
    for pattern in DESIGNATION_PATTERNS:
        match = re.search(pattern, first_section, re.IGNORECASE)
        if match:
            return match.group(0).strip().title()

    # Also check for explicit "designation" or "current role" labels
    label_match = re.search(
        r"(?:designation|current\s+role|job\s+title|position|current\s+title)\s*[:\-]\s*(.+?)(?:\n|$)",
        text, re.IGNORECASE
    )
    if label_match:
        val = label_match.group(1).strip()
        if 3 < len(val) < 80:
            return val.title()
    return None


def extract_keywords(text: str, skills: list[str]) -> list[str]:
    """Generate searchable keywords from the resume text."""
    keywords = set(s.lower() for s in skills)

    # Add education keywords
    for pattern in EDUCATION_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            keywords.add(match.group(0).lower().strip())

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


def fast_parse_resume(text: str) -> dict:
    """
    Parse a resume using regex and keyword matching ONLY — no AI calls.
    Returns a dict compatible with ResumeMetadata fields.
    Designed for bulk processing of 50k+ resumes.
    """
    email = extract_email(text)
    phone = extract_phone(text)
    name = extract_name(text)
    skills = extract_skills(text)
    experience = extract_experience_years(text)
    location = extract_location(text)
    education = extract_education(text)
    designation = extract_designation(text)
    keywords = extract_keywords(text, skills)

    # Build a brief summary from first ~200 chars of content
    summary_text = text[:500].replace("\n", " ").strip()
    summary = summary_text[:200] + "..." if len(summary_text) > 200 else summary_text

    return {
        "candidate_email": email,
        "candidate_name": name,
        "phone": phone,
        "current_designation": designation,
        "experience_years": experience,
        "skills": skills,
        "education": education,
        "certifications": [],  # Hard to extract reliably without AI
        "work_experience": [],  # Structure too complex for regex
        "location": location,
        "personal_details": {},
        "keywords": keywords,
        "summary": summary,
    }
