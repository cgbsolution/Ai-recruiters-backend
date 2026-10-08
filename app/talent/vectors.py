"""
Embedding storage that works with or without pgvector.

With the `vector` extension the embedding columns are `vector(N)` and Stage 2
of matching orders by `<=>` (cosine distance) inside Postgres. Without it they
are `double precision[]` and cosine similarity is computed with numpy over the
Stage-1-filtered set, so a database without the extension (the local dev
Postgres, possibly production) still matches — just in Python.

The columns are not mapped in app.models.talent; everything goes through here.

    python -m app.talent.vectors status    # which mode, how many vectors
    python -m app.talent.vectors upgrade   # after installing pgvector: convert the columns
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import sqlalchemy as sa

from app.talent.config import talent_config

logger = logging.getLogger("app.talent.vectors")

EMBEDDING_TABLES = ("cv_versions", "talent_job_settings")
_mode_cache: Dict[str, str] = {}


def _dialect(conn) -> str:
    return conn.dialect.name


def try_create_extension(conn) -> bool:
    """CREATE EXTENSION vector when the server ships it. Never raises; uses a
    savepoint so a permissions error doesn't abort the caller's transaction."""
    if _dialect(conn) != "postgresql":
        return False
    available = conn.execute(sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).scalar()
    if not available:
        return False
    try:
        with conn.begin_nested():
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        return True
    except Exception as exc:  # e.g. not a superuser and the extension isn't trusted
        logger.warning("pgvector is available but could not be enabled (%s); using float arrays", exc)
        return False


def vector_mode(conn) -> str:
    """'pgvector' when the extension is installed in this database, else 'array'."""
    if _dialect(conn) != "postgresql":
        return "array"
    installed = conn.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar()
    return "pgvector" if installed else "array"


def column_type(conn, table: str) -> Optional[str]:
    """'pgvector' | 'array' | None (column missing) for an embedding column."""
    row = conn.execute(
        sa.text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = 'embedding'"
        ),
        {"t": table},
    ).first()
    if row is None:
        return None
    return "pgvector" if row[0] == "vector" else "array"


def ensure_embedding_columns(conn) -> str:
    """Add the embedding columns if they are missing. Returns the mode."""
    if _dialect(conn) != "postgresql":
        return "array"
    try_create_extension(conn)
    mode = vector_mode(conn)
    sql_type = f"vector({int(talent_config.EMBEDDING_DIM)})" if mode == "pgvector" else "double precision[]"
    for table in EMBEDDING_TABLES:
        exists = conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": table}).scalar()
        if exists and column_type(conn, table) is None:
            conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN embedding {sql_type}"))
    return mode


def storage_mode(conn, table: str = "cv_versions") -> str:
    """How this table stores vectors right now (cached per process)."""
    key = f"{id(conn.engine)}:{table}"
    if key not in _mode_cache:
        _mode_cache[key] = column_type(conn, table) or "array"
    return _mode_cache[key]


def reset_cache() -> None:
    _mode_cache.clear()


def to_param(conn, table: str, vector: Sequence[float]):
    """Bind value for an embedding column: pgvector takes its text form."""
    values = [float(v) for v in vector]
    if storage_mode(conn, table) == "pgvector":
        return "[" + ",".join(repr(v) for v in values) + "]"
    return values


def cast_sql(conn, table: str, param: str = ":embedding") -> str:
    if storage_mode(conn, table) == "pgvector":
        return f"CAST({param} AS vector)"
    return f"CAST({param} AS double precision[])"


def from_db(value) -> Optional[List[float]]:
    """Read either storage form back into a list of floats."""
    if value is None:
        return None
    if isinstance(value, str):  # pgvector text form "[1,2,3]"
        return [float(x) for x in value.strip("[]").split(",") if x.strip()]
    if hasattr(value, "tolist"):
        return [float(x) for x in value.tolist()]
    return [float(x) for x in value]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def nearest_profiles(
    conn, query: Sequence[float], profile_ids: Iterable[int], limit: int, model: str
) -> List[Tuple[int, float]]:
    """(profile_id, cosine similarity) for the current CV versions of
    `profile_ids`, best first. Only vectors made by `model` are compared —
    vectors from different embedding models are not comparable."""
    ids = list(profile_ids)
    if not ids:
        return []
    if storage_mode(conn, "cv_versions") == "pgvector":
        rows = conn.execute(
            sa.text(
                "SELECT profile_id, 1 - (embedding <=> CAST(:q AS vector)) AS sim "
                "FROM cv_versions WHERE is_current AND embedding IS NOT NULL "
                "AND embedding_model = :model AND profile_id = ANY(:ids) "
                "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :limit"
            ),
            {"q": to_param(conn, "cv_versions", query), "ids": ids, "limit": limit, "model": model},
        ).all()
        return [(int(r[0]), float(r[1])) for r in rows]

    import numpy as np

    q = np.asarray(query, dtype=np.float32)
    qn = np.linalg.norm(q) or 1.0
    scored: List[Tuple[int, float]] = []
    # Chunked so 40K x 1024 floats never sit in memory at once.
    for start in range(0, len(ids), 2000):
        chunk = ids[start:start + 2000]
        rows = conn.execute(
            sa.text(
                "SELECT profile_id, embedding FROM cv_versions WHERE is_current AND embedding IS NOT NULL "
                "AND embedding_model = :model AND profile_id = ANY(:ids)"
            ),
            {"ids": chunk, "model": model},
        ).all()
        if not rows:
            continue
        mat = np.asarray([from_db(r[1]) for r in rows], dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        sims = (mat @ q) / (norms * qn)
        scored.extend((int(r[0]), float(s)) for r, s in zip(rows, sims))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:limit]


def upgrade_to_pgvector(conn) -> bool:
    """Convert float-array columns to vector(N) once the extension exists."""
    if not try_create_extension(conn) or vector_mode(conn) != "pgvector":
        return False
    dim = int(talent_config.EMBEDDING_DIM)
    for table in EMBEDDING_TABLES:
        if column_type(conn, table) == "array":
            conn.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({dim}) USING embedding::vector"))
    reset_cache()
    return True


if __name__ == "__main__":  # pragma: no cover - operator CLI
    import sys

    from app.database import engine

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    with engine.begin() as c:
        if cmd == "upgrade":
            print("converted" if upgrade_to_pgvector(c) else "pgvector not available — nothing changed")
        mode = vector_mode(c)
        counts = {t: c.execute(sa.text(f"SELECT count(embedding) FROM {t}")).scalar() for t in EMBEDDING_TABLES}
        print({"extension": mode, "columns": {t: column_type(c, t) for t in EMBEDDING_TABLES}, "vectors": counts})
