"""Move data that has no organization into one organization, and link that
organization's users.

A super admin with no organization stores everything it creates with
organization_id NULL. Since the September 2026 hardening, admins and
recruiters only see their own organization's rows, so they get an empty
dashboard. This does the same as Organizations -> "Assign to organization"
for when the UI is not deployed yet.

Run it where the production DATABASE_URL resolves (inside the backend
container). It only REPORTS unless --apply is given; --apply runs in one
transaction.

    python scripts/fix_org_assignment.py --domain cgbindia.com
    python scripts/fix_org_assignment.py --domain cgbindia.com --apply
    python scripts/fix_org_assignment.py --domain cgbindia.com --apply --include-super-admins

--include-super-admins also links super admins on the domain, so jobs they
create from now on belong to the organization (they then see only that
organization's jobs and candidates; managing orgs and users is unchanged).
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.config import settings  # noqa: E402  (not app.main: that runs migrations)

ORG_SCOPED_TABLES = ("jobs", "questions", "ai_credit_usage")


def show(cur, title, sql, params=None):
    cur.execute(sql, params)
    rows = cur.fetchall()
    print(f"\n== {title}")
    print("   " + " | ".join(d.name for d in cur.description))
    for r in rows:
        print("   " + " | ".join("" if v is None else str(v) for v in r))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True, help="organization email domain, e.g. cgbindia.com")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: report only)")
    parser.add_argument("--include-super-admins", action="store_true")
    args = parser.parse_args()
    domain = args.domain.strip().lower().lstrip("@")

    url = make_url(settings.DATABASE_URL)
    print(f"database: host={url.host} db={url.database}  mode={'APPLY' if args.apply else 'report only'}")
    conninfo = url.set(drivername="postgresql").render_as_string(hide_password=False)

    with psycopg.connect(conninfo, connect_timeout=15) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, is_active FROM organizations WHERE lower(domain) = %s", (domain,))
        org = cur.fetchone()
        if not org:
            sys.exit(f"No organization with domain '{domain}'.")
        org_id, org_name, org_active = org
        print(f"target organization: {org_name} (id={org_id}, active={org_active})")
        if not org_active:
            sys.exit("That organization is inactive — activate it first.")

        show(cur, "organizations", "SELECT id, name, domain, is_active, total_jobs_used FROM organizations ORDER BY id")
        show(cur, "users", "SELECT id, email, role, organization_id, is_active FROM users ORDER BY id")
        for t in ORG_SCOPED_TABLES + ("resume_metadata",):
            show(cur, f"{t} by organization_id",
                 f"SELECT organization_id, count(*) FROM {t} GROUP BY 1 ORDER BY 1 NULLS FIRST")
        show(cur, "candidates by job organization",
             "SELECT j.organization_id, count(*) FROM candidates c JOIN jobs j ON j.id = c.job_id GROUP BY 1 ORDER BY 1 NULLS FIRST")

        if not args.apply:
            print("\nReport only. Re-run with --apply to move the org-less rows and link the users.")
            return

        moved = {}
        for t in ORG_SCOPED_TABLES:
            cur.execute(f"UPDATE {t} SET organization_id = %s WHERE organization_id IS NULL", (org_id,))
            moved[t] = cur.rowcount

        # Resumes are unique per (email, organization): skip emails the org
        # already has and move one row per email.
        cur.execute(
            """
            UPDATE resume_metadata SET organization_id = %(org)s
            WHERE id IN (
                SELECT min(id) FROM resume_metadata
                WHERE organization_id IS NULL
                  AND candidate_email NOT IN (SELECT candidate_email FROM resume_metadata WHERE organization_id = %(org)s)
                GROUP BY candidate_email
            )
            """,
            {"org": org_id},
        )
        moved["resume_metadata"] = cur.rowcount

        roles = "('admin', 'recruiter', 'super_admin')" if args.include_super_admins else "('admin', 'recruiter')"
        cur.execute(
            f"""
            UPDATE users SET organization_id = %s, updated_at = now()
            WHERE lower(email) LIKE %s
              AND role::text IN {roles}
              AND organization_id IS DISTINCT FROM %s
            RETURNING email, role
            """,
            (org_id, "%@" + domain, org_id),
        )
        linked = cur.fetchall()
        moved["users"] = len(linked)

        cur.execute(
            "UPDATE organizations SET total_jobs_used = coalesce(total_jobs_used, 0) + %s, updated_at = now() WHERE id = %s",
            (moved["jobs"], org_id),
        )
        conn.commit()

        print(f"\nAPPLIED to {org_name}: {moved}")
        for email, role in linked:
            print(f"   linked {email} ({role})")
        show(cur, "jobs by organization_id (after)",
             "SELECT organization_id, count(*) FROM jobs GROUP BY 1 ORDER BY 1 NULLS FIRST")


if __name__ == "__main__":
    main()
