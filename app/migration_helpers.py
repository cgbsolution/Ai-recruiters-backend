"""Idempotent DDL helpers for Alembic migrations.

Why this exists
---------------
This deployment creates schema from three places:

1. ``startup.py`` — raw ``ADD COLUMN IF NOT EXISTS`` / ``CREATE TABLE IF NOT
   EXISTS`` statements run before the server boots.
2. ``app.main`` — an import-time ``alembic upgrade head`` with a
   ``Base.metadata.create_all()`` fallback plus its own raw ``ALTER TABLE``
   list.
3. Alembic itself.

The result is databases whose *schema* is far ahead of their
``alembic_version`` row. A bare ``op.add_column()`` on such a database raises
``DuplicateColumn`` and aborts the whole upgrade — which is exactly what took
the container down (``alembic upgrade head`` sits in the Docker CMD chain, so
uvicorn never starts).

Every migration therefore asks the database what it already has before it
creates anything. These helpers do that check, so migrations stay declarative.

Notes
-----
* A **fresh** ``Inspector`` is built per call on purpose: SQLAlchemy caches
  reflection results on the inspector, so a long-lived one goes stale as soon
  as the migration issues its first ``CREATE``.
* Everything degrades gracefully on non-PostgreSQL dialects (the test suite
  uses SQLite via ``create_all``), so no ``IF NOT EXISTS`` SQL is hardcoded.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import sqlalchemy as sa
from alembic import op


# ── Introspection ────────────────────────────────────────────────────────────

def _inspector() -> sa.Inspector:
    """A fresh inspector — never cache one across DDL statements."""
    return sa.inspect(op.get_bind())


def has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def has_column(table: str, column: str) -> bool:
    if not has_table(table):
        return False
    return column in {c["name"] for c in _inspector().get_columns(table)}


def has_index(table: str, index_name: str) -> bool:
    if not has_table(table):
        return False
    return index_name in {i["name"] for i in _inspector().get_indexes(table)}


def has_unique_constraint(table: str, name: str) -> bool:
    if not has_table(table):
        return False
    insp = _inspector()
    if name in {u["name"] for u in insp.get_unique_constraints(table)}:
        return True
    # A unique constraint is also backed by an index of the same name.
    return name in {i["name"] for i in insp.get_indexes(table)}


def has_foreign_key(table: str, name: str) -> bool:
    if not has_table(table):
        return False
    return name in {fk["name"] for fk in _inspector().get_foreign_keys(table)}


def has_enum_type(type_name: str) -> bool:
    """True if a PostgreSQL enum type of this name exists (False elsewhere)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    return bool(
        bind.execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = :n"), {"n": type_name}
        ).scalar()
    )


def enum_labels(type_name: str) -> set[str]:
    """Current labels of a PostgreSQL enum type (empty set if absent)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return set()
    rows = bind.execute(
        sa.text(
            "SELECT e.enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :n"
        ),
        {"n": type_name},
    ).fetchall()
    return {r[0] for r in rows}


# ── Guarded DDL ──────────────────────────────────────────────────────────────

def add_column_if_missing(table: str, column: sa.Column) -> bool:
    """``op.add_column`` that skips columns the database already has."""
    if not has_table(table) or has_column(table, column.name):
        return False
    op.add_column(table, column)
    return True


def create_table_if_missing(table: str, *columns, **kwargs) -> bool:
    if has_table(table):
        return False
    op.create_table(table, *columns, **kwargs)
    return True


def create_index_if_missing(
    name: str, table: str, columns: Sequence[str], unique: bool = False, **kwargs
) -> bool:
    if not has_table(table) or has_index(table, name):
        return False
    op.create_index(name, table, list(columns), unique=unique, **kwargs)
    return True


def create_unique_constraint_if_missing(
    name: str, table: str, columns: Sequence[str]
) -> bool:
    if not has_table(table) or has_unique_constraint(table, name):
        return False
    op.create_unique_constraint(name, table, list(columns))
    return True


def create_foreign_key_if_missing(
    name: str,
    source_table: str,
    referent_table: str,
    local_cols: Sequence[str],
    remote_cols: Sequence[str],
    **kwargs,
) -> bool:
    if not has_table(source_table) or not has_table(referent_table):
        return False
    if has_foreign_key(source_table, name):
        return False
    op.create_foreign_key(
        name, source_table, referent_table, list(local_cols), list(remote_cols), **kwargs
    )
    return True


def _zero_value_for(sa_type):
    """A benign non-NULL value for a column type we must fill in."""
    if isinstance(sa_type, sa.Boolean):
        return False
    if isinstance(sa_type, (sa.Integer, sa.Numeric, sa.Float)):
        return 0
    if isinstance(sa_type, (sa.String, sa.Text)):
        return ""
    if isinstance(sa_type, (sa.DateTime, sa.Date)):
        return sa.func.now()
    return None


def insert_row_if_missing(table: str, values: dict, unique_column: str) -> bool:
    """Insert one row unless a row with the same `unique_column` value exists.

    Any column the caller does not supply that is NOT NULL *and* has no
    database-level default is filled with a zero value for its type.

    That matters because several models declare `Column(..., default=0,
    nullable=False)`. SQLAlchemy's `default=` is applied in Python on ORM
    inserts and emits NO `DEFAULT` clause in the DDL — so a database built by
    `create_all()` ends up with a NOT NULL column that a plain INSERT cannot
    satisfy, and a migration inserting a seed row dies with NotNullViolation
    on a column it has never heard of.
    """
    if not has_table(table):
        return False

    columns = {c["name"]: c for c in _inspector().get_columns(table)}
    payload = dict(values)

    for name, col in columns.items():
        if name in payload or col["nullable"] or col.get("default") is not None:
            continue
        if col.get("autoincrement") or col.get("primary_key"):
            continue
        filler = _zero_value_for(col["type"])
        if filler is not None:
            payload[name] = filler

    cols = ", ".join(payload)
    selects = ", ".join(
        str(v) if isinstance(v, sa.sql.functions.Function) else f":{k}"
        for k, v in payload.items()
    )
    binds = {
        k: v for k, v in payload.items()
        if not isinstance(v, sa.sql.functions.Function)
    }

    op.get_bind().execute(
        sa.text(
            f"INSERT INTO {table} ({cols}) SELECT {selects} "
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {unique_column} = :__uq)"
        ),
        {**binds, "__uq": values[unique_column]},
    )
    return True


def enum_type(name: str, values: Iterable[str]):
    """Return a column type for an enum, creating the PG type if needed.

    On PostgreSQL the type is created up front with ``checkfirst`` and the
    returned type has ``create_type=False``, so a guarded ``create_table``
    never tries to ``CREATE TYPE`` a name that already exists (which is how a
    half-applied migration turns into a hard failure). Other dialects get a
    plain ``sa.Enum``.
    """
    values = list(values)
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return sa.Enum(*values, name=name)

    from sqlalchemy.dialects import postgresql

    postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)
    return postgresql.ENUM(*values, name=name, create_type=False)
