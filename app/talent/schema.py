"""
Database objects the models can't express: the embedding columns
(app.talent.vectors) and the audit trigger that logs every deletion of a
talent-pool profile.

The trigger is the guarantee behind "no row is deleted without a log entry":
it fires for every path — the Resume Database delete / bulk delete / clear-all
endpoints, bulk-upload de-duplication, Phase 0 merges, retention, erasure and
raw SQL alike. Code that knows who is deleting sets the actor first:

    set_audit_actor(db, "user:12")   # SET LOCAL, so it ends with the transaction

Called by the Alembic migration, the agent worker at startup and the tests.
"""

import sqlalchemy as sa

from app.talent.vectors import ensure_embedding_columns

_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION talent_audit_profile_delete() RETURNS trigger AS $$
BEGIN
  INSERT INTO audit_log (at, organization_id, profile_id, actor, action, subject_type, subject_id, detail)
  VALUES (
    timezone('utc', now()),
    OLD.organization_id,
    OLD.id,
    COALESCE(NULLIF(current_setting('talent.actor', true), ''), 'app'),
    'profile.deleted',
    'profile',
    OLD.id::text,
    jsonb_build_object(
      'source', OLD.source,
      'reason', NULLIF(current_setting('talent.delete_reason', true), '')
    )
  );
  RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_talent_audit_profile_delete') THEN
    CREATE TRIGGER trg_talent_audit_profile_delete
      AFTER DELETE ON resume_metadata
      FOR EACH ROW EXECUTE FUNCTION talent_audit_profile_delete();
  END IF;
END $$;
"""


def ensure_db_objects(conn) -> str:
    """Idempotent. Returns the vector storage mode."""
    mode = ensure_embedding_columns(conn)
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text(_TRIGGER_FN))
        conn.execute(sa.text(_TRIGGER))
    return mode


def drop_db_objects(conn) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("DROP TRIGGER IF EXISTS trg_talent_audit_profile_delete ON resume_metadata"))
        conn.execute(sa.text("DROP FUNCTION IF EXISTS talent_audit_profile_delete()"))


def set_audit_actor(db, actor: str, reason: str = "") -> None:
    """Name who is deleting for the rest of this transaction (Postgres only)."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    db.execute(sa.text("SELECT set_config('talent.actor', :a, true)"), {"a": actor[:80]})
    db.execute(sa.text("SELECT set_config('talent.delete_reason', :r, true)"), {"r": reason[:120]})
