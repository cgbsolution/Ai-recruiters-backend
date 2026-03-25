JOB_PAYLOAD = {
    "title": "Senior Python Developer",
    "department": "Engineering",
    "location": "Remote",
    "job_type": "Full-time",
    "experience_level": "3-5 Years",
    "experience_min": 3,
    "experience_max": 5,
    "openings": 2,
    "emp_type": "Permanent",
    "status": "Open",
    "responsibilities": [{"description": "Build REST APIs"}],
    "skills": [{"name": "Backend", "skill": "Python", "skill_criteria": "All Mandatory"}],
    "interview_settings": {
        "code_editor_required": True,
        "resume_based": False,
        "virtual_avatar": False,
        "allow_mobile": True,
        "face_match": False,
        "webcam_required": True,
        "secondary_cam": False,
        "mode": "TRADITIONAL",
        "max_questions": 10,
        "auto_skip_timeout": 30,
        "special_instructions": "",
        "scoring": "Standard Evaluation",
        "manual_interview": False,
    },
    "manager_name": "Alice Smith",
    "manager_phone": "1234567890",
    "manager_email": "alice@example.com",
    "evaluation_scoring": "Standard Evaluation",
    "mix_of_questions": "MIX",
    "questions": [{"text": "Explain async/await", "category": "Python", "difficulty": "intermediate"}],
    "allow_to_contact": True,
    "upload_jd": "",
}


def test_create_job(client):
    resp = client.post("/jobs/", json=JOB_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Senior Python Developer"
    assert data["department"] == "Engineering"
    assert len(data["responsibilities"]) == 1
    assert len(data["skills"]) == 1
    assert data["interview_settings"] is not None


def test_list_jobs(client):
    client.post("/jobs/", json=JOB_PAYLOAD)
    resp = client.get("/jobs/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 1


def test_get_job(client):
    create_resp = client.post("/jobs/", json=JOB_PAYLOAD)
    job_id = create_resp.json()["id"]
    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == job_id


def test_update_job(client):
    create_resp = client.post("/jobs/", json=JOB_PAYLOAD)
    job_id = create_resp.json()["id"]
    updated = {**JOB_PAYLOAD, "title": "Lead Python Developer", "status": "Inactive"}
    resp = client.put(f"/jobs/{job_id}", json=updated)
    assert resp.status_code == 200
    assert resp.json()["title"] == "Lead Python Developer"
    assert resp.json()["status"] == "Inactive"


def test_delete_job(client):
    create_resp = client.post("/jobs/", json=JOB_PAYLOAD)
    job_id = create_resp.json()["id"]
    resp = client.delete(f"/jobs/{job_id}")
    assert resp.status_code == 204
    resp2 = client.get(f"/jobs/{job_id}")
    assert resp2.status_code == 404


def test_get_nonexistent_job(client):
    resp = client.get("/jobs/99999")
    assert resp.status_code == 404
