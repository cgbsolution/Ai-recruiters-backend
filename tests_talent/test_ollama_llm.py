"""The Talent Agent on Ollama (TALENT_LLM_PROVIDER=ollama, the product default):
schema-constrained calls, the errors that matter, and a full match run scored
by a (mocked) Ollama server — no Anthropic call anywhere."""
import json

import httpx
import inngest
import pytest
from inngest.experimental import mocked

from app.agent.match_job import match_job
from app.models.ai_credit_usage import AiCreditUsage
from app.talent import claude, llm, match_loop, ollama_chat
from app.talent.config import TalentConfig, talent_config
from tests_talent import fakes
from tests_talent.conftest import CapturingInngest
from tests_talent.test_match_progress import _job, _settings


def test_ollama_is_the_default_provider():
    assert TalentConfig.model_fields["TALENT_LLM_PROVIDER"].default == "ollama"


@pytest.fixture
def ollama(monkeypatch):
    """Switch the agent to Ollama and answer its /api/chat calls with `respond(body)`."""
    monkeypatch.setattr(talent_config, "TALENT_LLM_PROVIDER", "ollama")
    monkeypatch.setattr(talent_config, "TALENT_OLLAMA_MODEL", "qwen2.5:7b-instruct")
    monkeypatch.setattr(claude.settings, "ANTHROPIC_API_KEY", "")  # Claude can't be reached at all
    calls = []
    state = {"respond": lambda body: {}}

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        out = state["respond"](body)
        if isinstance(out, httpx.Response):
            return out
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(out)},
                                         "done_reason": "stop", "prompt_eval_count": 600, "eval_count": 300})

    monkeypatch.setattr(ollama_chat, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return {"calls": calls, "state": state}


async def _call(**kw):
    return await llm.structured(feature="talent_rerank", system="SYSTEM", user="<untrusted_candidate>\n{}\n</untrusted_candidate>",
                                schema={"type": "object", "properties": {"score": {"type": "integer"}}},
                                model="claude-opus-5-5", effort="medium", prompt_version="v1", max_tokens=500, **kw)


@pytest.mark.asyncio
async def test_a_call_is_schema_constrained_deterministic_and_free(ollama, db):
    ollama["state"]["respond"] = lambda body: {"score": 81}
    result = await _call(organization_id=None)
    body = ollama["calls"][0]
    assert body["model"] == "qwen2.5:7b-instruct"  # not the Claude model the call site names
    assert body["format"]["properties"]["score"]["type"] == "integer"
    assert body["options"]["temperature"] == 0 and body["options"]["num_predict"] == 500
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert result.data == {"score": 81} and result.model == "qwen2.5:7b-instruct" and not result.refused
    row = db.query(AiCreditUsage).filter_by(feature="talent_rerank").order_by(AiCreditUsage.id.desc()).first()
    assert row.provider == "ollama" and row.estimated_cost == 0 and row.output_tokens == 300


@pytest.mark.asyncio
async def test_errors_are_classified(ollama):
    ollama["state"]["respond"] = lambda body: httpx.Response(404, json={"error": "model not found"})
    with pytest.raises(ollama_chat.OllamaModelMissing) as missing:
        await _call()
    assert "ollama pull qwen2.5:7b-instruct" in llm.account_problem_from(missing.value)
    assert not llm.is_transient(missing.value)

    def down(body):
        raise httpx.ConnectError("refused")

    ollama["state"]["respond"] = down
    with pytest.raises(ollama_chat.OllamaTransient) as transient:
        await _call()
    assert llm.is_transient(transient.value) and llm.account_problem_from(transient.value) is None

    ollama["state"]["respond"] = lambda body: httpx.Response(200, json={"message": {"content": "Sure! Here is"}})
    with pytest.raises(ValueError):
        await _call()


def test_a_match_run_is_scored_by_ollama_in_small_steps(world, db, ollama, monkeypatch):
    org = world.org()
    world.enable(org, "match")
    for i in range(5):
        world.profile(org, email=f"o{i}@x.com", verification_status="fresh", skills='["Kafka", "Python"]',
                      work_experience=json.dumps([{"title": "Engineer", "company": f"C{i}"}]))
    job = _job(world, org)
    # The Claude fakes' scorer reads the user turn; Ollama sends it as the second message.
    ollama["state"]["respond"] = lambda body: fakes.rerank_responder({"messages": [body["messages"][1]]})
    chunks = []
    real = match_loop.chunk_scored
    monkeypatch.setattr(match_loop, "chunk_scored", lambda db, job_id, n: (chunks.append(n), real(db, job_id, n))[1])

    match_loop.request(db, job.id)
    result = mocked.trigger(match_job, inngest.Event(name="talent/match.requested", data={"job_id": job.id}),
                            CapturingInngest())
    assert result.error is None, result.error
    js = _settings(db, job.id)
    assert js.match_status == "done" and not js.match_progress.get("ai_error")
    assert len(ollama["calls"]) == 5 and chunks == [2, 2, 1]  # TALENT_OLLAMA_RERANK_CHUNK = 2
    from app.models.talent import JobMatch
    scored = db.query(JobMatch).filter_by(job_id=job.id).all()
    assert all(m.ai_score is not None and m.model == "qwen2.5:7b-instruct" for m in scored)


def test_ollama_sizes_for_a_cpu(monkeypatch):
    monkeypatch.setattr(talent_config, "TALENT_LLM_PROVIDER", "ollama")
    assert (llm.rerank_pool(), llm.rerank_chunk(), llm.rerank_concurrency()) == (20, 2, 1)
    assert llm.followups_enabled() is False  # the candidate would wait on a CPU model
    monkeypatch.setattr(talent_config, "TALENT_OLLAMA_FOLLOWUPS", True)
    assert llm.followups_enabled() is True
    monkeypatch.setattr(talent_config, "TALENT_LLM_PROVIDER", "anthropic")
    assert (llm.rerank_pool(), llm.rerank_chunk()) == (200, 10)


def test_settings_show_the_ollama_model_and_its_problems(client, world, monkeypatch):
    monkeypatch.setattr(talent_config, "TALENT_LLM_PROVIDER", "ollama")
    monkeypatch.setattr(ollama_chat, "problem", lambda max_age=60: "Ollama has no model 'qwen2.5:7b-instruct'")
    out = client.get("/talent/settings", headers=world.headers(world.user(world.org()))).json()
    assert out["ai_provider"] == "ollama" and out["models"]["rerank"] == talent_config.TALENT_OLLAMA_MODEL
    assert "Ollama has no model 'qwen2.5:7b-instruct'" in out["warnings"]
    assert not any("ANTHROPIC" in w or "Anthropic" in w for w in out["warnings"])
