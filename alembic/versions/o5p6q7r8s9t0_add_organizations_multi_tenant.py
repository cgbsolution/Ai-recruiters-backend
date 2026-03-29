"""Add organizations table and organization_id to core tables for multi-tenancy

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9
Create Date: 2026-03-28 16:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "o5p6q7r8s9t0"
down_revision = "n4o5p6q7r8s9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1. Create organizations table
    if "organizations" not in existing_tables:
        op.create_table(
            "organizations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("domain", sa.String(255), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_organizations_domain", "organizations", ["domain"], unique=True)
        op.create_index("ix_organizations_id", "organizations", ["id"])

    # 2. Insert a default organization for existing data
    op.execute(
        """
        INSERT INTO organizations (name, domain, is_active, created_at)
        SELECT 'Default Organization', 'default.local', true, NOW()
        WHERE NOT EXISTS (SELECT 1 FROM organizations WHERE domain = 'default.local')
        """
    )

    # 3. Add organization_id columns (nullable first for safe backfill)
    tables_needing_org_id = ["users", "jobs", "questions", "resume_metadata", "ai_credit_usage"]
    for table in tables_needing_org_id:
        if table in existing_tables:
            existing_columns = [c["name"] for c in inspector.get_columns(table)]
            if "organization_id" not in existing_columns:
                op.add_column(table, sa.Column("organization_id", sa.Integer(), nullable=True))
                op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])

    # 4. Backfill existing rows with the default organization
    op.execute(
        """
        UPDATE users SET organization_id = (SELECT id FROM organizations WHERE domain = 'default.local' LIMIT 1)
        WHERE organization_id IS NULL AND role != 'super_admin'
        """
    )
    for table in ["jobs", "questions", "resume_metadata", "ai_credit_usage"]:
        op.execute(
            f"""
            UPDATE {table} SET organization_id = (SELECT id FROM organizations WHERE domain = 'default.local' LIMIT 1)
            WHERE organization_id IS NULL
            """
        )

    # 5. Add foreign key constraints
    for table in tables_needing_org_id:
        if table in existing_tables:
            try:
                op.create_foreign_key(
                    f"fk_{table}_organization_id",
                    table, "organizations",
                    ["organization_id"], ["id"],
                )
            except Exception:
                pass  # FK may already exist


def downgrade() -> None:
    tables = ["users", "jobs", "questions", "resume_metadata", "ai_credit_usage"]
    for table in tables:
        try:
            op.drop_constraint(f"fk_{table}_organization_id", table, type_="foreignkey")
        except Exception:
            pass
        try:
            op.drop_index(f"ix_{table}_organization_id", table_name=table)
        except Exception:
            pass
        try:
            op.drop_column(table, "organization_id")
        except Exception:
            pass

    op.drop_index("ix_organizations_domain", table_name="organizations")
    op.drop_index("ix_organizations_id", table_name="organizations")
    op.drop_table("organizations")
