"""
Export all resume_metadata from the LOCAL database to a JSON file.

Usage:
    python export_resumes.py

Output: resume_export.json (in current directory)
"""

import json
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from app.database import SessionLocal
from app.models.resume_metadata import ResumeMetadata


def export():
    db = SessionLocal()
    try:
        resumes = db.query(ResumeMetadata).filter(
            ~ResumeMetadata.candidate_email.like("%@pending.local"),
            ResumeMetadata.parsing_status == "completed",
        ).all()

        records = []
        for r in resumes:
            records.append({
                "candidate_email": r.candidate_email,
                "candidate_name": r.candidate_name,
                "phone": r.phone,
                "current_designation": r.current_designation,
                "experience_years": r.experience_years,
                "skills": r.skills,
                "education": r.education,
                "certifications": r.certifications,
                "work_experience": r.work_experience,
                "location": r.location,
                "personal_details": r.personal_details,
                "keywords": r.keywords,
                "summary": r.summary,
                "raw_text": r.raw_text,
                "resume_file_path": r.resume_file_path,
                "parsing_status": r.parsing_status,
                "parsed_at": r.parsed_at.isoformat() if r.parsed_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })

        out_path = os.path.join(os.path.dirname(__file__), "resume_export.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False)

        print(f"Exported {len(records)} resumes to {out_path}")
        print(f"File size: {os.path.getsize(out_path) / (1024*1024):.1f} MB")
    finally:
        db.close()


if __name__ == "__main__":
    export()
