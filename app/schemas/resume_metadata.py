from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ResumeMetadataRead(BaseModel):
    id: int
    candidate_email: str
    candidate_name: Optional[str] = None
    phone: Optional[str] = None
    current_designation: Optional[str] = None
    experience_years: Optional[float] = None
    skills: Optional[str] = None           # JSON string
    education: Optional[str] = None
    certifications: Optional[str] = None
    work_experience: Optional[str] = None
    location: Optional[str] = None
    personal_details: Optional[str] = None
    keywords: Optional[str] = None
    summary: Optional[str] = None
    resume_file_path: Optional[str] = None
    parsing_status: str
    parsed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ResumeMetadataUpdate(BaseModel):
    candidate_name: Optional[str] = None
    phone: Optional[str] = None
    current_designation: Optional[str] = None
    experience_years: Optional[float] = None
    skills: Optional[List[str]] = None
    location: Optional[str] = None
    annual_package: Optional[float] = None


class DuplicateCheckResponse(BaseModel):
    exists: bool
    candidate_email: Optional[str] = None
    candidate_name: Optional[str] = None
    current_designation: Optional[str] = None
    resume_file_path: Optional[str] = None
    parsed_at: Optional[datetime] = None
    # Which jobs this candidate is already linked to
    linked_job_ids: List[int] = []


class MatchedCandidateItem(BaseModel):
    resume_metadata_id: int
    candidate_email: str
    candidate_name: Optional[str] = None
    current_designation: Optional[str] = None
    experience_years: Optional[float] = None
    location: Optional[str] = None
    skills_match: List[str] = []       # skills that overlap with JD
    fit_score: int = 0                 # 0-100
    already_added: bool = False        # already a candidate for this job


class MatchCandidatesResponse(BaseModel):
    job_id: int
    total_in_database: int
    matched_count: int
    candidates: List[MatchedCandidateItem]


class AddMatchedCandidatesRequest(BaseModel):
    candidate_emails: List[str]


class AddMatchedCandidatesResponse(BaseModel):
    added: int
    skipped: int   # already existed for this job
    details: List[Dict[str, Any]] = []
