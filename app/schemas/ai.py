from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, Field

# Bounds for text that is pasted into paid LLM prompts. Unbounded, one request
# could carry ~1 MB per field and bill a near-full context window each time.
# Resume text and job descriptions stay unbounded here: internal callers pass
# whole documents, and the services truncate them before prompting.
ShortText = Annotated[str, Field(max_length=500)]
LongText = Annotated[str, Field(max_length=20_000)]
ListItem = Annotated[str, Field(max_length=2_000)]


class GenerateJDRequest(BaseModel):
    title: ShortText
    department: ShortText = ""
    experience_min: Optional[int] = 0
    experience_max: Optional[int] = 2
    experience_level: Optional[ShortText] = "0-2 Years"
    job_type: Optional[ShortText] = "Full-time"
    location: Optional[ShortText] = ""


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
    job_title: ShortText
    department: ShortText
    skills: List[ShortText] = Field(default=[], max_length=100)
    difficulty: ShortText = "intermediate"
    count: int = Field(default=10, ge=1, le=50)
    mode: ShortText = "TRADITIONAL"


class GenerateQuestionsResponse(BaseModel):
    questions: List[Dict[str, Any]]


class EvaluateCandidateRequest(BaseModel):
    job_title: ShortText
    question: LongText
    answer: LongText
    scoring: ShortText = "Standard Evaluation"


class EvaluateCandidateResponse(BaseModel):
    score: int
    feedback: str
    passed: bool


class SuggestQuestionsRequest(BaseModel):
    category: ShortText
    difficulty: ShortText = "intermediate"
    count: int = Field(default=5, ge=1, le=50)


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
    current_designation: Optional[str] = None
    skills: List[str] = []
    education: List[str] = []
    certifications: List[str] = []
    work_experience: List[Dict[str, Any]] = []
    personal_details: Optional[Dict[str, Any]] = None
    keywords: List[str] = []
    summary: Optional[str] = None


class ParseResumeResponse(BaseModel):
    parsed_data: ParsedResumeData
    raw_text_length: int


# ── Fit Score ─────────────────────────────────────────────────────────────────

class FitScoreRequest(BaseModel):
    resume_text: str
    job_title: ShortText
    job_description: Optional[str] = None
    required_experience_min: Optional[int] = None
    required_experience_max: Optional[int] = None
    required_skills: List[ListItem] = Field(default=[], max_length=300)
    responsibilities: List[ListItem] = Field(default=[], max_length=300)


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
    # Advanced scoring fields
    score_breakdown: Optional[Dict[str, Any]] = None  # 6-dimension numeric scores
    strength_mapping: Optional[Dict[str, Any]] = None  # strong/partial/gap mapping
    verdict: Optional[str] = None                      # one-line hiring recommendation
