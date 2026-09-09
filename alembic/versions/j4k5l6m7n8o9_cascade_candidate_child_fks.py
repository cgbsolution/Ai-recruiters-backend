"""Repair ON DELETE rules on foreign keys that reference candidates

DELETE /candidates/{id} was failing in production with::

    ForeignKeyViolation: update or delete on table "candidates" violates
    foreign key constraint "email_logs_candidate_id_fkey" on table "email_logs"

`email_logs` is created in two places: the raw DDL list in `app.main` (which
declares ON DELETE CASCADE) and `Base.metadata.create_all()` (which used the
model, and the model had no `ondelete=`). Databases that took the create_all()
path got a NO ACTION constraint, and `CREATE TABLE IF NOT EXISTS` can never
repair it — so every candidate who had ever been emailed became undeletable.

The model is fixed for new databases; this migration repairs existing ones. It
covers every FK pointing at `candidates`, not just `email_logs`, because the
same create_all()-before-the-annotation history applies to all of them and
fixing one at a time just moves the violation to the next table.

Revision ID: j4k5l6m7n8o9
Revises: i3j4k5l6m7n8
Create Date: 2026-09-09 09:00:00.000000
"""
from app.migration_helpers import has_table, set_fk_ondelete

# revision identifiers
revision = "j4k5l6m7n8o9"
down_revision = "i3j4k5l6m7n8"
branch_labels = None
depends_on = None


# (table, column, ON DELETE rule) — mirrors the ForeignKey definitions in
# app/models/. interview_sessions is SET NULL on purpose: the session row
# (transcript, evaluation, recording) outlives the candidate record.
_CANDIDATE_CHILD_FKS = [
    ("email_logs", "candidate_id", "CASCADE"),
    ("evaluations", "candidate_id", "CASCADE"),
    ("meeting_schedules", "candidate_id", "CASCADE"),
    ("interview_status_logs", "candidate_id", "CASCADE"),
    ("observer_tokens", "candidate_id", "CASCADE"),
    ("interview_sessions", "candidate_id", "SET NULL"),
]


def upgrade() -> None:
    if not has_table("candidates"):
        return
    for table, column, rule in _CANDIDATE_CHILD_FKS:
        if has_table(table):
            set_fk_ondelete(table, column, "candidates", rule)


def downgrade() -> None:
    # No-op by design: these rules now match the models, and restoring NO
    # ACTION would reintroduce the ForeignKeyViolation this migration fixes.
    pass
