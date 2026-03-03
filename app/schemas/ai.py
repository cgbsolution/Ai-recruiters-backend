from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class GenerateJDRequest(BaseModel):
    title: str
    department: str = ""
    experience_min: Optional[int] = 0
    experience_max: Optional[int] = 2
    experience_level: Optional[str] = "0-2 Years"
    job_type: Optional[str] = "Full-time"
    location: Optional[str] = ""


class SkillGroupItem(BaseModel):
    name: str
    skills: List[str]
    criteria: int  # 1=All Mandatory, 2=At Least One, 3=Good to have
    weight: float


class GenerateJDResponse(BaseModel):
    title: str
    summary: str
    experience_min: int
    experience_max: int
    responsibilities: List[str]
    skill_groups: List[SkillGroupItem]
    special_instructions: str
    coding_questions_count: int
    business_context: str


class GenerateQuestionsRequest(BaseModel):
    job_title: str
    department: str
    skills: List[str] = []
    difficulty: str = "intermediate"
    count: int = 10
    mode: str = "TRADITIONAL"


class GenerateQuestionsResponse(BaseModel):
    questions: List[Dict[str, Any]]


class EvaluateCandidateRequest(BaseModel):
    job_title: str
    question: str
    answer: str
    scoring: str = "Standard Evaluation"


class EvaluateCandidateResponse(BaseModel):
    score: int
    feedback: str
    passed: bool


class SuggestQuestionsRequest(BaseModel):
    category: str
    difficulty: str = "intermediate"
    count: int = 5


class SuggestQuestionsResponse(BaseModel):
    questions: List[str]


# ── Resume Parsing ────────────────────────────────────────────────────────────

class ParseResumeRequest(BaseModel):
    resume_text: str
    job_id: Optional[int] = None


class ParsedResumeData(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    experience_years: Optional[float] = None
    current_role: Optional[str] = None
    skills: List[str] = []
    education: List[str] = []
    work_experience: List[Dict[str, Any]] = []
    summary: Optional[str] = None


class ParseResumeResponse(BaseModel):
    parsed_data: ParsedResumeData
    raw_text_length: int


# ── Fit Score ─────────────────────────────────────────────────────────────────

class FitScoreRequest(BaseModel):
    resume_text: str
    job_title: str
    job_description: Optional[str] = None
    required_experience_min: Optional[int] = None
    required_experience_max: Optional[int] = None
    required_skills: List[str] = []
    responsibilities: List[str] = []


class FitScoreBreakdown(BaseModel):
    experience_match: str
    skills_match: str
    tech_stack_alignment: str
    role_alignment: str


class FitScoreResponse(BaseModel):
    fit_level: str                       # "low" | "medium" | "high"
    fit_percentage: int                  # 0–100 semantic JD match score
    summary: str                         # 1-2 sentence overall summary
    key_matching_skills: List[str] = []  # skills from resume that match JD
    relevant_experience_summary: str = ""# concise paragraph of relevant experience
    missing_weak_areas: List[str] = []   # skills/areas missing or weak vs JD
    breakdown: FitScoreBreakdown
    reasoning_points: List[str]
