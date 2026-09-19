"""Create a local-only admin account used to capture booth screenshots.

Signups made through /auth/signup land with is_active=False (they wait on admin
approval), so there is no self-service way to get a usable login on a fresh local
database. This adds one activated admin without touching any existing user.

LOCAL DEV ONLY — refuses any DATABASE_URL that isn't on localhost, and asks for
the password at run time.

    python scripts/create_booth_capture_user.py
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.database import SessionLocal
from app.models.user import User, UserRole

EMAIL = "booth.capture@cgbindia.com"
# Shown in the app header and the dashboard greeting, so it lands on booth
# signage — use a plausible recruiter name, not "Booth Capture".
DISPLAY_NAME = "Priya Anand"


def main() -> None:
    # This creates an active ADMIN. The password used to be a literal in this
    # (public-repo, shipped-in-the-image) file and nothing stopped a run
    # against production, which would have left a known-password admin there.
    host = make_url(settings.DATABASE_URL).host or ""
    if host not in ("localhost", "127.0.0.1", "::1"):
        sys.exit(f"Refusing to run: DATABASE_URL points at '{host}', not a local database.")
    PASSWORD = getpass.getpass(f"Password for {EMAIL}: ")
    if len(PASSWORD) < 8:
        sys.exit("Password must be at least 8 characters.")

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == EMAIL).first()
        # Inherit the org of the current admin so the account sees the same jobs.
        admin = db.query(User).filter(User.role == UserRole.admin).first()
        org_id = admin.organization_id if admin else None

        if existing:
            existing.name = DISPLAY_NAME
            existing.password_hash = hash_password(PASSWORD)
            existing.is_active = True
            existing.is_email_verified = True
            db.commit()
            print(f"reset  {EMAIL} (org={existing.organization_id})")
            return

        user = User(
            name=DISPLAY_NAME,
            email=EMAIL,
            password_hash=hash_password(PASSWORD),
            role=UserRole.admin,
            is_active=True,
            is_email_verified=True,
            organization_id=org_id,
        )
        db.add(user)
        db.commit()
        print(f"created {EMAIL} (org={org_id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
