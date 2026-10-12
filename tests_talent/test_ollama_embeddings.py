"""EMBEDDING_PROVIDER=ollama: self-hosted embeddings for the talent pool (no API key, no data leaves the server)."""
import httpx
import pytest

from app.models.talent import CvVersion
from app.talent import backfill, cv, embeddings, matching


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code, self._payload = status_code, payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)


@pytest.fixture
def ollama(monkeypatch):
    """Route embeddings to a fake Ollama that answers with the hash vectors (so similarity still means something)."""
    monkeypatch.setattr(embeddings.talent_config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(embeddings.talent_config, "EMBEDDING_MODEL", "")
    calls = []

    def post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _Resp(payload={"embeddings": embeddings._hash_vectors(json["input"])})

    monkeypatch.setattr(httpx, "post", post)
    return calls


def test_ollama_request_shape(ollama):
    vecs = embeddings.embed_texts(["Skills: Kafka, Python"], "document")
    assert len(vecs) == 1 and len(vecs[0]) == 1024
    call = ollama[0]
    assert call["url"] == "http://localhost:11434/api/embed"
    assert call["json"]["model"] == "bge-m3"
    assert call["json"]["input"] == ["Skills: Kafka, Python"]
    assert call["json"]["truncate"] is True
    assert call["json"]["options"]["num_ctx"] == 4096


def test_pool_is_embedded_and_ranked_through_ollama_without_pii(ollama, world, db):
    org = world.org()
    job = world.job(org, title="Senior Data Engineer", skills=("Kafka", "Python"), experience_min=4,
                    experience_max=10, location="Pune")
    kafka = world.profile(org, email="kafka.person@x.com", candidate_name="Ravi Menon", verification_status="fresh",
                          skills='["Kafka", "Python", "Spark"]', current_designation="Senior Data Engineer")
    web = world.profile(org, email="web.person@x.com", candidate_name="Meera Iyer", verification_status="fresh",
                        skills='["Python", "Django", "CSS"]', current_designation="Web Developer")
    for p in (kafka, web):
        cv.ensure_baseline(db, p)
    db.commit()
    assert backfill.embed_chunk(db, org.id) == 2
    ranked = matching.stage2(db, job, matching.stage1(db, job, matching.requirements(db, job)))
    assert [r["profile_id"] for r in ranked] == [kafka.id, web.id]
    assert db.query(CvVersion).filter(CvVersion.embedding_model == "bge-m3").count() == 2
    sent = " ".join(t for c in ollama for t in c["json"]["input"])
    for pii in ("Ravi", "Menon", "Meera", "Iyer", "kafka.person@x.com", "web.person@x.com"):
        assert pii not in sent


def test_wrong_vector_size_is_refused(monkeypatch):
    monkeypatch.setattr(embeddings.talent_config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(embeddings.talent_config, "EMBEDDING_MODEL", "nomic-embed-text")
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: _Resp(payload={"embeddings": [[0.1] * 768]}))
    with pytest.raises(ValueError, match="768-number vectors"):
        embeddings.embed_texts(["text"])


def test_missing_model_and_unreachable_server_say_what_to_do(monkeypatch):
    monkeypatch.setattr(embeddings.talent_config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(embeddings.talent_config, "EMBEDDING_MODEL", "")
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: _Resp(404, {"error": "not found"}))
    with pytest.raises(RuntimeError, match="ollama pull bge-m3"):
        embeddings.embed_texts(["text"])

    def refuse(url, json=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refuse)
    with pytest.raises(RuntimeError, match="not reachable"):
        embeddings.embed_texts(["text"])


def test_settings_warning_for_ollama(monkeypatch):
    monkeypatch.setattr(embeddings.talent_config, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(embeddings.talent_config, "EMBEDDING_MODEL", "")
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp(payload={"models": [{"name": "bge-m3:latest"}]}))
    assert embeddings.ollama_problem() is None
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp(payload={"models": [{"name": "qwen2.5:7b"}]}))
    assert "ollama pull bge-m3" in embeddings.ollama_problem()

    def down(url, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    assert "not reachable" in embeddings.ollama_problem()
