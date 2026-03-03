QUESTION_PAYLOAD = {
    "text": "What is a Python decorator?",
    "category": "Python",
    "difficulty": "intermediate",
}


def test_create_question(client):
    resp = client.post("/questions/", json=QUESTION_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["text"] == QUESTION_PAYLOAD["text"]
    assert data["category"] == "Python"
    assert data["difficulty"] == "intermediate"
    assert "id" in data


def test_list_questions(client):
    client.post("/questions/", json=QUESTION_PAYLOAD)
    resp = client.get("/questions/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 1


def test_update_question(client):
    create_resp = client.post("/questions/", json=QUESTION_PAYLOAD)
    q_id = create_resp.json()["id"]
    resp = client.put(f"/questions/{q_id}", json={"difficulty": "advanced"})
    assert resp.status_code == 200
    assert resp.json()["difficulty"] == "advanced"


def test_delete_question(client):
    create_resp = client.post("/questions/", json=QUESTION_PAYLOAD)
    q_id = create_resp.json()["id"]
    resp = client.delete(f"/questions/{q_id}")
    assert resp.status_code == 204


def test_delete_nonexistent_question(client):
    resp = client.delete("/questions/99999")
    assert resp.status_code == 404
