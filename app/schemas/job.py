from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


# ── Nested Input Schemas ─────────────────────────────────────────────────────

class ResponsibilityIn(BaseModel):
    description: str


class SkillIn(BaseModel):
    name: str
    skill: str
    skill_criteria: str = "All Mandatory"


class InterviewSettingsIn(BaseModel):
    code_editor_required: bool = False
    resume_based: bool = False
    virtual_avatar: bool = False
    allow_mobile: bool = False
    face_match: bool = False
    webcam_required: bool = False
    secondary_cam: bool = False
    mode: str = "TRADITIONAL"
    max_questions: int = 10
    auto_skip_timeout: int = 30
    special_instructions: str = ""
    scoring: str = "Standard Evaluation"
    manual_interview: bool = False


class QuestionIn(BaseModel):
    text: str
    category: str = "general"
    difficulty: str = "medium"


class PreInterviewFieldIn(BaseModel):
    id: str
    label: str
    type: str
    is_default: bool = False


# ── Request Schemas ──────────────────────────────────────────────────────────

class JobCreate(BaseModel):
    title: str
    department: str
    location: str
    job_type: str
    experience_level: Optional[str] = "0-2 Years"
    experience_min: int = 0
    experience_max: int = 2
    openings: int = 1
    emp_type: Optional[str] = "Permanent"
    status: str = "Draft"
    deadline: Optional[str] = None
    responsibilities: List[ResponsibilityIn] = []
    skills: List[SkillIn] = []
    interview_settings: Optional[InterviewSettingsIn] = None
    manager_name: Optional[str] = ""
    manager_phone: Optional[str] = ""
    manager_email: Optional[str] = ""
    evaluation_scoring: Optional[str] = "Standard Evaluation"
    mix_of_questions: str = "MIX"
    questions: List[QuestionIn] = []
    allow_to_contact: bool = False
    pre_interview_fields: List[PreInterviewFieldIn] = []
    upload_jd: Optional[str] = ""


class JobUpdate(JobCreate):
    pass


# ── Response Schemas ─────────────────────────────────────────────────────────

class ResponsibilityOut(BaseModel):
    id: int
    description: str
    model_config = {"from_attributes": True}


class SkillOut(BaseModel):
    id: int
    name: str
    skill: str
    skill_criteria: str
    model_config = {"from_attributes": True}


class InterviewSettingsOut(BaseModel):
    id: int
    code_editor_required: bool
    resume_based: bool
    virtual_avatar: bool
    allow_mobile: bool
    face_match: bool
    webcam_required: bool
    secondary_cam: bool
    mode: str
    max_questions: int
    auto_skip_timeout: int
    special_instructions: Optional[str]
    scoring: str
    manual_interview: bool
    model_config = {"from_attributes": True}


class QuestionOut(BaseModel):
    id: int
    text: str
    category: str
    difficulty: str
    model_config = {"from_attributes": True}


class PreInterviewFieldOut(BaseModel):
    id: int
    field_id: str
    label: str
    type: str
    is_default: bool
    model_config = {"from_attributes": True}


class JobRead(BaseModel):
    id: int
    title: str
    department: str
    location: str
    job_type: str
    experience_level: Optional[str]
    experience_min: int
    experience_max: int
    openings: int
    emp_type: Optional[str]
    status: str
    deadline: Optional[str]
    applications: int
    interviews: int
    candidates: int
    manager_name: Optional[str]
    manager_phone: Optional[str]
    manager_email: Optional[str]
    evaluation_scoring: Optional[str]
    mix_of_questions: str
    allow_to_contact: bool
    upload_jd: Optional[str]
    created_at: datetime
    updated_at: datetime
    responsibilities: List[ResponsibilityOut] = []
    skills: List[SkillOut] = []
    interview_settings: Optional[InterviewSettingsOut] = None
    questions: List[QuestionOut] = []
    pre_interview_fields: List[PreInterviewFieldOut] = []
    interview_token: Optional[str] = None
    model_config = {"from_attributes": True}


class JobListItem(BaseModel):
    id: int
    title: str
    department: str
    location: str
    job_type: str
    openings: int
    status: str
    created_at: datetime
    applications: int
    interviews: int
    candidates: int
    deadline: Optional[str]
    interview_token: Optional[str] = None
    model_config = {"from_attributes": True}
