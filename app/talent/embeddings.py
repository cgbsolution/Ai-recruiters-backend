"""
Embeddings for Stage 2 of matching. Anthropic has no embeddings endpoint, so
the provider is chosen with EMBEDDING_PROVIDER:

    ollama   self-hosted at OLLAMA_BASE_URL, default bge-m3 (1024 dims, 8K context, multilingual) — no API key
    voyage   VOYAGE_API_KEY, default model voyage-3.5 (1024 dims) — Anthropic's recommended partner
    gemini   GEMINI_API_KEY, default gemini-embedding-001 at 1024 dims
    openai   OPENAI_API_KEY, default text-embedding-3-small at 1024 dims
    hash     deterministic local feature hashing — tests / dev only, no semantics
    ""       off: Stage 2 falls back to keyword similarity

Only REDACTED text is embedded (app.talent.redaction.embedding_text), so CV
PII never reaches the embedding provider. Every vector records the model that
made it; vectors from different models are never compared.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
import uuid
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import settings
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import CvVersion, TalentJobSettings
from app.talent import vectors
from app.talent.config import talent_config
from app.talent.redaction import embedding_text

logger = logging.getLogger("app.talent.embeddings")

_DEFAULT_MODELS = {"ollama": "bge-m3", "voyage": "voyage-3.5", "gemini": "gemini-embedding-001",
                   "openai": "text-embedding-3-small", "hash": "hash-1024"}
BATCH = 64


class EmbeddingsOff(RuntimeError):
    pass


def provider() -> str:
    return (talent_config.EMBEDDING_PROVIDER or "").strip().lower()


def model_name() -> str:
    p = provider()
    if not p:
        raise EmbeddingsOff("EMBEDDING_PROVIDER is not set")
    return talent_config.EMBEDDING_MODEL or _DEFAULT_MODELS.get(p, p)


def enabled() -> bool:
    return bool(provider())


def _hash_vectors(texts: List[str]) -> List[List[float]]:
    dim = int(talent_config.EMBEDDING_DIM)
    out = []
    for text in texts:
        v = [0.0] * dim
        tokens = re.findall(r"[a-z0-9+#.]{2,}", text.lower())
        for tok in tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]:
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            v[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / norm for x in v])
    return out


_ollama_checked: dict = {}  # (base, model) -> (monotonic time, problem)


def ollama_problem(max_age: float = 0) -> Optional[str]:
    """For the settings page: why Ollama embeddings would fail right now, or None.
    With ``max_age`` (seconds), a result that recent is reused instead of asking
    Ollama again, so the page doesn't wait on the network every time it loads."""
    import httpx

    base, model = talent_config.OLLAMA_BASE_URL.rstrip("/"), model_name()
    seen = _ollama_checked.get((base, model))
    if max_age and seen and time.monotonic() - seen[0] < max_age:
        return seen[1]
    try:
        names = [m.get("name", "") for m in httpx.get(f"{base}/api/tags", timeout=3).json().get("models", [])]
    except Exception:
        problem = f"Ollama is not reachable at {base}: profile embeddings will fail."
    else:
        wanted = model if ":" in model else f"{model}:latest"
        problem = (f"Ollama has no model {model!r}: run `ollama pull {model}` on the Ollama server."
                   if wanted not in names and model not in names else None)
    _ollama_checked[(base, model)] = (time.monotonic(), problem)
    return problem


def _ollama_vectors(texts: List[str], model: str, dim: int) -> List[List[float]]:
    import httpx

    base = talent_config.OLLAMA_BASE_URL.rstrip("/")
    try:
        resp = httpx.post(
            f"{base}/api/embed",
            json={"model": model, "input": texts, "truncate": True, "keep_alive": "15m",
                  "options": {"num_ctx": int(talent_config.OLLAMA_NUM_CTX)}},
            timeout=float(talent_config.OLLAMA_TIMEOUT_SECONDS),
        )
    except httpx.ConnectError as exc:
        raise RuntimeError(f"Ollama is not reachable at {base}") from exc
    if resp.status_code == 404:
        raise RuntimeError(f"Ollama has no model {model!r}: run `ollama pull {model}` on the Ollama server")
    resp.raise_for_status()
    vectors_out = resp.json()["embeddings"]
    if len(vectors_out) != len(texts):
        raise RuntimeError(f"Ollama returned {len(vectors_out)} vectors for {len(texts)} texts")
    if vectors_out and len(vectors_out[0]) != dim:
        # Storing them would break every comparison against the 1024-wide column.
        raise ValueError(f"Ollama model {model!r} makes {len(vectors_out[0])}-number vectors but EMBEDDING_DIM is "
                         f"{dim}: use a {dim}-dimension model such as bge-m3 or mxbai-embed-large")
    return vectors_out


def embed_texts(texts: List[str], kind: str = "document") -> List[List[float]]:
    """kind: document (profiles) | query (jobs) — some providers embed them differently."""
    p, model, dim = provider(), model_name(), int(talent_config.EMBEDDING_DIM)
    if not texts:
        return []
    if p == "hash":
        return _hash_vectors(texts)
    if p == "ollama":
        return _ollama_vectors(texts, model, dim)
    if p == "voyage":
        import httpx

        resp = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {talent_config.VOYAGE_API_KEY}"},
            json={"input": texts, "model": model, "input_type": kind, "output_dimension": dim},
            timeout=60,
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]
    if p == "gemini":
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        result = client.models.embed_content(
            model=model, contents=texts,
            config=types.EmbedContentConfig(
                output_dimensionality=dim,
                task_type="RETRIEVAL_DOCUMENT" if kind == "document" else "RETRIEVAL_QUERY"),
        )
        return [list(e.values) for e in result.embeddings]
    if p == "openai":
        from openai import OpenAI

        result = OpenAI(api_key=settings.OPENAI_API_KEY).embeddings.create(model=model, input=texts, dimensions=dim)
        return [d.embedding for d in sorted(result.data, key=lambda d: d.index)]
    raise EmbeddingsOff(f"unknown EMBEDDING_PROVIDER {p!r}")


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _store(db: Session, table: str, key_column: str, key, vector: List[float], model: str, text_hash: str) -> None:
    conn = db.connection()
    db.execute(
        sa.text(
            f"UPDATE {table} SET embedding = {vectors.cast_sql(conn, table)}, embedding_model = :model, "
            f"embedded_text_hash = :h, embedded_at = :at WHERE {key_column} = :key"
        ),
        {"embedding": vectors.to_param(conn, table, vector), "model": model, "h": text_hash,
         "at": datetime.utcnow(), "key": key},
    )


def embed_versions(db: Session, version_ids: List[str]) -> dict:
    """Embed parsed CV versions (skipping ones already embedded from the same text by the same model)."""
    if not enabled():
        return {"status": "off"}
    model = model_name()
    rows = []
    for vid in version_ids:
        version = db.get(CvVersion, uuid.UUID(str(vid)))
        if version is None or version.parse_status != "parsed" or not version.parsed:
            continue
        profile = db.get(ResumeMetadata, version.profile_id)
        text = embedding_text(version.parsed, profile.candidate_name if profile else None)
        if not text.strip():
            # Nothing to embed: record that this model looked, so backfills move on.
            version.embedding_model, version.embedded_text_hash = model, "empty"
            continue
        h = _text_hash(text)
        if version.embedding_model == model and version.embedded_text_hash == h:
            continue
        rows.append((version.id, text, h))
    db.commit()
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        vecs = embed_texts([t for _, t, _ in chunk], "document")
        for (vid, _, h), vec in zip(chunk, vecs):
            _store(db, "cv_versions", "id", vid, vec, model, h)
            done += 1
        db.commit()
    return {"status": "ok", "embedded": done, "model": model}


def embed_version_sync(db: Session, version_id: str) -> dict:
    try:
        return embed_versions(db, [version_id])
    except EmbeddingsOff:
        return {"status": "off"}


def job_text(db: Session, job) -> str:
    """The job side of Stage 2: title, must-haves, skills, responsibilities."""
    from app.models.job_metadata import JobMetadata
    from app.talent.settings import get_job_settings

    js = get_job_settings(db, job.id, job.organization_id)
    jm = db.query(JobMetadata).filter(JobMetadata.job_id == job.id).first()
    lines = [f"Role: {job.title}", f"Department: {job.department}"]
    if job.experience_min is not None or job.experience_max is not None:
        lines.append(f"Experience: {job.experience_min or 0}-{job.experience_max or ''} years")
    if js.must_haves:
        lines.append("Must have: " + "; ".join(js.must_haves))
    skills = [s.skill or s.name for s in job.skills if (s.skill or s.name)]
    if skills:
        lines.append("Skills: " + ", ".join(skills))
    if js.nice_to_haves:
        lines.append("Nice to have: " + "; ".join(js.nice_to_haves))
    for r in job.responsibilities[:15]:
        lines.append(r.description)
    if jm and jm.responsibilities_summary:
        lines.append(jm.responsibilities_summary)
    return "\n".join(lines)[:8000]


def embed_job(db: Session, job) -> Optional[List[float]]:
    """Embed the job if its text changed; returns the vector (None when off)."""
    if not enabled():
        return None
    from app.talent.settings import get_job_settings

    js = get_job_settings(db, job.id, job.organization_id)
    text = job_text(db, job)
    h, model = _text_hash(text), model_name()
    if js.embedding_model == model and js.embedded_text_hash == h:
        row = db.execute(sa.text("SELECT embedding FROM talent_job_settings WHERE job_id = :j"), {"j": job.id}).first()
        if row and row[0] is not None:
            return vectors.from_db(row[0])
    vec = embed_texts([text], "query")[0]
    db.commit()  # make sure the settings row exists before the raw UPDATE
    _store(db, "talent_job_settings", "job_id", job.id, vec, model, h)
    db.commit()
    return vec


def backfill_candidates(db: Session, organization_id: Optional[int], limit: int = 1000) -> List[str]:
    """Current versions with no vector from the configured model (for the backfill run)."""
    if not enabled():
        return []
    model = model_name()
    q = (
        db.query(CvVersion.id)
        .join(ResumeMetadata, ResumeMetadata.id == CvVersion.profile_id)
        .filter(CvVersion.is_current.is_(True), CvVersion.parse_status == "parsed",
                sa.or_(CvVersion.embedding_model.is_(None), CvVersion.embedding_model != model))
    )
    if organization_id is not None:
        q = q.filter(ResumeMetadata.organization_id == organization_id)
    return [str(v) for (v,) in q.order_by(CvVersion.received_at.desc()).limit(limit)]
