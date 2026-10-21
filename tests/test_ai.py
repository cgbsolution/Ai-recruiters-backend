import json
from unittest.mock import AsyncMock, patch

from app.services.llm_provider import LLMResult


def _llm_answers(response_text: str):
    """Stand in for the LLM provider: every AI call answers with this text."""
    return patch(
        "app.services.ai_service._ask_with_usage",
        AsyncMock(return_value=LLMResult(text=response_text)),
    )


def test_generate_jd(client):
    jd_json = json.dumps({
        "title": "Backend Developer",
        "summary": "A great role.",
        "experience_min": 2,
        "experience_max": 4,
        "responsibilities": ["Build APIs"],
        "skill_groups": [{"name": "Core", "skills": ["Python"], "criteria": 1, "weight": 2.0}],
        "special_instructions": "Probe API design.",
        "coding_questions_count": 1,
        "business_context": "No specific business context mentioned",
    })
    with _llm_answers(jd_json):
        resp = client.post("/ai/generate-jd", json={
            "title": "Backend Developer",
            "department": "Engineering",
            "experience_level": "2-4 Years",
            "job_type": "Full-time",
            "location": "Remote",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert "summary" in data
    assert "responsibilities" in data
    assert data["skill_groups"][0]["skills"] == ["Python"]


def test_generate_questions(client):
    q_json = '[{"text": "What is REST?", "category": "APIs", "difficulty": "intermediate"}]'
    with _llm_answers(q_json):
        resp = client.post("/ai/generate-questions", json={
            "job_title": "Backend Developer",
            "department": "Engineering",
            "skills": ["Python", "FastAPI"],
            "difficulty": "intermediate",
            "count": 1,
            "mode": "TRADITIONAL",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert "questions" in data
    assert len(data["questions"]) == 1


def test_evaluate_answer(client):
    eval_json = '{"score": 85, "feedback": "Good answer.", "passed": true}'
    with _llm_answers(eval_json):
        resp = client.post("/ai/evaluate-answer", json={
            "job_title": "Backend Developer",
            "question": "Explain async/await",
            "answer": "It allows non-blocking I/O in Python.",
            "scoring": "Standard Evaluation",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["score"] == 85
    assert data["passed"] is True


def test_suggest_questions(client):
    suggestions_json = '["What is a context manager?", "Explain GIL in Python."]'
    with _llm_answers(suggestions_json):
        resp = client.post("/ai/suggest-questions", json={
            "category": "Python",
            "difficulty": "intermediate",
            "count": 2,
        })
    assert resp.status_code == 200
    data = resp.json()
    assert "questions" in data
    assert len(data["questions"]) == 2
