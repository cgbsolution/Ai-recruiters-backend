from unittest.mock import MagicMock, patch


def _mock_claude(response_text: str):
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=response_text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg
    return mock_client


def test_generate_jd(client):
    jd_json = '{"summary": "A great role.", "responsibilities": ["Build APIs"], "skills": ["Python"]}'
    with patch("app.services.ai_service._client", return_value=_mock_claude(jd_json)):
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
    assert "skills" in data


def test_generate_questions(client):
    q_json = '[{"text": "What is REST?", "category": "APIs", "difficulty": "intermediate"}]'
    with patch("app.services.ai_service._client", return_value=_mock_claude(q_json)):
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
    with patch("app.services.ai_service._client", return_value=_mock_claude(eval_json)):
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
    with patch("app.services.ai_service._client", return_value=_mock_claude(suggestions_json)):
        resp = client.post("/ai/suggest-questions", json={
            "category": "Python",
            "difficulty": "intermediate",
            "count": 2,
        })
    assert resp.status_code == 200
    data = resp.json()
    assert "questions" in data
    assert len(data["questions"]) == 2
