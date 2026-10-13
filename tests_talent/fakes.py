"""A fake Anthropic client: records every request, answers with a responder."""
import json
import re
from types import SimpleNamespace


class FakeMessages:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    async def create(self, **params):
        self.calls.append(params)
        data = self.responder(params)
        if data == "REFUSE":
            return SimpleNamespace(content=[], stop_reason="refusal", usage=SimpleNamespace(input_tokens=10, output_tokens=0),
                                   model=params["model"], _request_id="req_test")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(data))], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200), model=params["model"], _request_id="req_test",
        )


class FakeClaude:
    def __init__(self, responder):
        self.beta = SimpleNamespace(messages=FakeMessages(responder))

    @property
    def calls(self):
        return self.beta.messages.calls


def install(monkeypatch, responder) -> FakeClaude:
    from app.talent import claude

    fake = FakeClaude(responder)
    monkeypatch.setattr(claude.settings, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(claude, "_client", fake)
    monkeypatch.setattr(claude, "account_problem", lambda max_age=600: None)  # never call Anthropic from tests
    return fake


def user_text(params) -> str:
    content = params["messages"][0]["content"]
    return content if isinstance(content, str) else json.dumps(content)


def candidate_payload(params) -> dict:
    m = re.search(r"<untrusted_candidate>\n(.*)\n</untrusted_candidate>", user_text(params), re.S)
    return json.loads(m.group(1)) if m else {}


def job_payload(params) -> dict:
    m = re.search(r"<untrusted_job>\n(.*)\n</untrusted_job>", user_text(params), re.S)
    return json.loads(m.group(1)) if m else {}


def rerank_responder(params) -> dict:
    """Scores by how many job skills appear in the candidate's skills, and
    quotes real evidence from the payload."""
    cand, job = candidate_payload(params), job_payload(params)
    skills = [s["name"] for s in cand.get("skills", [])]
    lower = {s.lower() for s in skills}
    must = []
    for req in job.get("must_haves", []):
        hit = next((s for s in skills if s.lower() in req.lower()), None)
        must.append({"requirement": req, "met": "yes" if hit else "no", "evidence": hit or ""})
    coverage = sum(m["met"] == "yes" for m in must) / max(len(must), 1)
    base = int(40 + 50 * coverage)
    return {
        "score": base, "dimension_scores": {"must_have_coverage": int(100 * coverage), "relevant_experience": base,
                                            "skills_depth": base, "domain_fit": 60, "trajectory": 55},
        "must_haves": must, "gaps": [m["requirement"] for m in must if m["met"] != "yes"],
        "strengths": [{"strength": f"Uses {s}", "evidence": s} for s in skills[:2]],
        "risk_flags": [] if "kafka" in lower else [{"flag": "Short tenures", "evidence": ""}],
    }


def pdf_bytes() -> bytes:
    """A small real PDF (uploads are opened and inspected before they're stored)."""
    import io

    import pikepdf

    buf = io.BytesIO()
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(buf)
    return buf.getvalue()
