"""Claude request shapes the API actually accepts (found by the local end-to-end
run: the CV parse schema had 22 nullable fields, over the structured-output
limit of 16, so every real CV parse was refused with a 400)."""
import asyncio

import anthropic
import httpx2 as httpx
import pytest

from app.talent import parsing, prompts
from tests_talent import fakes

STRUCTURED_OUTPUT_UNION_LIMIT = 16
SCHEMAS = {name: getattr(prompts, name) for name in dir(prompts) if name.endswith("_SCHEMA")}


def _union_params(schema) -> int:
    count = 0
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "anyOf" in node or isinstance(node.get("type"), list):
                count += 1
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return count


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_schema_fits_the_structured_output_union_limit(name):
    assert _union_params(SCHEMAS[name]) <= STRUCTURED_OUTPUT_UNION_LIMIT


def _cv_reply(**over):
    reply = {
        "identity": {"name": "", "email": "", "phone": "", "location": ""},
        "current_role": "Data Engineer", "current_employer": "", "industry": "",
        "work_history": [{"title": "Data Engineer", "company": "Acme", "start": "2021-01", "end": "",
                          "is_current": True, "summary": ""}],
        "skills": [{"name": "Kafka", "years": None, "last_used_year": None}],
        "education": [{"degree": "B.Tech", "field": "", "institution": "", "year": None}],
        "certifications": ["", "AWS SAA"], "summary": "",
        "stated": {"current_ctc_lpa": None, "expected_ctc_lpa": None, "notice_period_days": None},
        "field_confidence": {k: 0.9 for k in ("identity", "current_role", "work_history", "skills", "education",
                                              "certifications", "industry")},
        "changes_summary": [],
    }
    reply.update(over)
    return reply


def test_blank_text_from_the_model_becomes_none(monkeypatch):
    fakes.install(monkeypatch, lambda params: _cv_reply())
    result = asyncio.run(parsing.parse_cv("Data Engineer at Acme since 2021. Kafka. B.Tech. AWS SAA."))
    parsed = result.parsed
    assert parsed["current_designation"] == "Data Engineer"
    assert parsed["current_company"] is None and parsed["industry"] is None
    assert parsed["work_experience"][0]["end_date"] is None and parsed["work_experience"][0]["description"] is None
    assert parsed["education"] == ["B.Tech"]
    assert parsed["certifications"] == ["AWS SAA"]


def test_a_request_the_api_refuses_falls_back_to_the_regex_parse(monkeypatch):
    def refuse(params):
        raise anthropic.BadRequestError(
            "schema too complex", body=None,
            response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")))

    fakes.install(monkeypatch, refuse)
    assert asyncio.run(parsing.parse_cv("Data Engineer at Acme since 2021.")) is None
