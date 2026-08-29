"""Seed fictional demo jobs + candidates for booth screenshots.

Why this exists
---------------
The local `ai_recruiters` database contains a handful of REAL job applicants
(real names, real personal email addresses and phone numbers). Those must never
appear on printed trade-show signage, and there are too few of them to fill a
pipeline view anyway — every one sits at status "Applied".

This adds entirely fictional records, themed for a real-estate technology event,
spread across every pipeline stage so the Candidates and Job Openings screens
photograph well.

Everything written here is tagged with the reserved `@example.com` email domain, so it
is trivially reversible:

    python scripts/seed_booth_demo_data.py --revert

No existing row is modified or deleted. LOCAL DEV DATABASE ONLY.

    python scripts/seed_booth_demo_data.py
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.models.candidate import Candidate, CandidateStatus
from app.models.job import Job, JobStatus
from app.models.user import User, UserRole

# RFC 2606 reserved domain: reads naturally at print size and can never be
# delivered to a real inbox. Also the marker used by --revert.
TAG = "@example.com"

# (title, department, location, exp_level, exp_min, exp_max, openings)
JOBS = [
    ("Leasing Manager — Commercial", "Leasing", "Gurugram", "Mid", 4, 8, 2),
    ("Site Engineer — Residential Towers", "Projects", "Noida", "Mid", 3, 6, 3),
    ("Facilities Manager", "Facilities", "Bengaluru", "Senior", 6, 10, 1),
    ("Sales Manager — Luxury Residential", "Sales", "Mumbai", "Senior", 5, 9, 2),
]

# (name, role, city, job_index, status, stage, interview_status)
CANDIDATES = [
    ("Ananya Deshpande", "Leasing Manager", "Gurugram", 0, CandidateStatus.hired, "Offer-Made", "Completed"),
    ("Rohit Malhotra", "Leasing Manager", "Gurugram", 0, CandidateStatus.offered, "Offer-Made", "Completed"),
    ("Sneha Iyer", "Leasing Manager", "Delhi", 0, CandidateStatus.interview, "Panel Round", "Scheduled"),
    ("Vikram Chauhan", "Leasing Manager", "Gurugram", 0, CandidateStatus.applied, "Candidates", "New"),
    ("Farhan Qureshi", "Site Engineer", "Noida", 1, CandidateStatus.selected, "Panel Round", "Completed"),
    ("Meera Raghavan", "Site Engineer", "Ghaziabad", 1, CandidateStatus.interview, "AI Round", "Scheduled"),
    ("Arjun Sethi", "Site Engineer", "Noida", 1, CandidateStatus.applied, "Candidates", "New"),
    ("Kavya Nambiar", "Site Engineer", "Faridabad", 1, CandidateStatus.rejected, "Rejected", "Completed"),
    ("Imran Shaikh", "Facilities Manager", "Bengaluru", 2, CandidateStatus.interview, "Panel Round", "Scheduled"),
    ("Divya Menon", "Facilities Manager", "Bengaluru", 2, CandidateStatus.applied, "Candidates", "New"),
    ("Sandeep Kulkarni", "Facilities Manager", "Mysuru", 2, CandidateStatus.rejected, "Rejected", "Completed"),
    ("Nikhil Bhatia", "Sales Manager", "Mumbai", 3, CandidateStatus.hired, "Offer-Made", "Completed"),
    ("Priyanka Joshi", "Sales Manager", "Pune", 3, CandidateStatus.offered, "Offer-Made", "Completed"),
    ("Rahul Vaidya", "Sales Manager", "Mumbai", 3, CandidateStatus.interview, "AI Round", "Scheduled"),
    ("Tanvi Kapoor", "Sales Manager", "Thane", 3, CandidateStatus.applied, "Candidates", "New"),
    ("Aditya Rane", "Sales Manager", "Mumbai", 3, CandidateStatus.applied, "Candidates", "New"),
]


def slug(name: str) -> str:
    return name.lower().replace(" ", ".")


def revert(db) -> None:
    cands = db.query(Candidate).filter(Candidate.email.like(f"%{TAG}")).all()
    for c in cands:
        db.delete(c)
    jobs = db.query(Job).filter(Job.manager_email.like(f"%{TAG}")).all()
    for j in jobs:
        db.delete(j)
    db.commit()
    print(f"removed {len(cands)} demo candidates and {len(jobs)} demo jobs")


def seed(db) -> None:
    admin = db.query(User).filter(User.role == UserRole.admin).first()
    org_id = admin.organization_id if admin else None
    now = datetime.utcnow()

    if db.query(Job).filter(Job.manager_email.like(f"%{TAG}")).count():
        print("demo data already present — run with --revert first")
        return

    jobs = []
    for i, (title, dept, loc, lvl, lo, hi, openings) in enumerate(JOBS):
        job = Job(
            title=title,
            department=dept,
            location=loc,
            job_type="Full-time",
            emp_type="Full-time",
            experience_level=lvl,
            experience_min=lo,
            experience_max=hi,
            openings=openings,
            status=JobStatus.open,
            organization_id=org_id,
            manager_name="Priya Anand",
            manager_email=f"priya.anand{TAG}",
            created_at=now - timedelta(days=18 + i * 6),
        )
        db.add(job)
        jobs.append(job)
    db.flush()

    per_job_apps = {j.id: 0 for j in jobs}
    per_job_ivs = {j.id: 0 for j in jobs}

    for i, (name, role, city, ji, status, stage, iv_status) in enumerate(CANDIDATES):
        job = jobs[ji]
        db.add(
            Candidate(
                name=name,
                role=role,
                email=f"{slug(name)}{TAG}",
                # Reserved-for-documentation range — never a real subscriber.
                phone=f"+91 900000{i:04d}"[:20],
                location=city,
                status=status,
                job_id=job.id,
                candidate_stage=stage,
                interview_status=iv_status,
                applied_date=now - timedelta(days=15 - (i % 14)),
                # score deliberately left NULL: the brief excludes Fit Score /
                # AI Round Score from booth signage, and a null score renders no chip.
            )
        )
        per_job_apps[job.id] += 1
        if iv_status in ("Scheduled", "Completed"):
            per_job_ivs[job.id] += 1

    for job in jobs:
        job.applications = per_job_apps[job.id]
        job.interviews = per_job_ivs[job.id]
        job.candidates = per_job_apps[job.id]

    db.commit()
    print(f"seeded {len(jobs)} demo jobs and {len(CANDIDATES)} demo candidates (org={org_id})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revert", action="store_true", help="delete everything this script created")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        revert(db) if args.revert else seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
