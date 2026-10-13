"""Phase 2 — Loop B: stage filters, embeddings, Claude rerank, redaction,
needs-refresh, ratings and precision@10, backfill."""
import json
import random
import re
import uuid
from datetime import datetime, timedelta

import inngest
from inngest.experimental import mocked

from app.models import ResumeMetadata
from app.models.talent import AuditLog, CampaignMember, CvVersion, JobMatch, TalentJobSettings
from app.talent import backfill, cv, embeddings, match_loop, matching, redaction, rerank
from tests_talent import fakes
from tests_talent.conftest import CapturingInngest

FIRST = ["Rahul", "Priyanka", "Mohammed", "Gurpreet", "Anjali", "Thomas", "Lakshmi", "Farhan", "Deepika", "Sandeep"]
LAST = ["Sharma", "Iyer", "Qureshi", "Dhillon", "Banerjee", "Mathew", "Venkatesan", "Shaikh", "Kulkarni", "Yadav"]
RELIGIONS = ["Hindu", "Muslim", "Christian", "Sikh", "Jain", "Buddhist"]
CATEGORIES = ["OBC", "SC/ST", "General category", "EWS category"]
MARITAL = ["Married", "Unmarried", "Divorced", "Widowed"]
GENDERS = ["Male", "Female"]


def _profile_with_protected_data(world, org, i, rng):
    first, last = rng.choice(FIRST), rng.choice(LAST)
    father = f"{rng.choice(FIRST)} {last}"
    dob = f"{rng.randint(1,28):02d}/{rng.randint(1,12):02d}/{rng.randint(1970,2001)}"
    gender, religion, cat, marital = rng.choice(GENDERS), rng.choice(RELIGIONS), rng.choice(CATEGORIES), rng.choice(MARITAL)
    street = f"{rng.randint(10, 999)}, {rng.choice(['MG Road', 'Linking Road', 'Anna Salai', 'Park Street'])}"
    email = f"{first.lower()}.{last.lower()}{i}@mail.example"
    phone = f"98{rng.randint(10000000, 99999999)}"
    pronoun = "She" if gender == "Female" else "He"
    work = [
        {"title": "Data Engineer", "company": "Northwind Analytics", "start_date": "2019-04", "end_date": None,
         "duration": "Apr 2019 - Present",
         "description": f"{first} {last} built Kafka pipelines in Python. {pronoun} led a team of 4. "
                        f"Personal details: DOB: {dob}; Gender: {gender}; Religion: {religion}; Category: {cat}; "
                        f"Marital Status: {marital}; Nationality: Indian; Father's Name: {father}; "
                        f"Address: {street}, Pune; email {email}, phone +91 {phone}."},
        {"title": "Software Engineer", "company": "Contoso Systems", "start_date": "2015-06", "end_date": "2019-03",
         "duration": "Jun 2015 - Mar 2019", "description": f"Mr. {last} wrote ETL jobs. {pronoun} is {marital.lower()}."},
    ]
    meta = world.profile(
        org, email=email, phone=phone, candidate_name=f"{first} {last}", created_at=datetime.utcnow() - timedelta(days=30),
        current_designation="Data Engineer", experience_years=8, location=f"{street}, Pune",
        skills=json.dumps(["Python", "Kafka", "SQL"]), work_experience=json.dumps(work),
        education=json.dumps([f"B.Tech Computer Science, IIT Bombay ({rng.randint(1990, 2015)})"]),
        summary=f"{first} {last}, {rng.randint(25, 50)} years old {gender.lower()} {religion} engineer, {marital}. "
                f"Born on {dob}. Indian national. Lives at {street}.",
        personal_details=json.dumps({"dob": dob, "gender": gender, "religion": religion, "caste": cat,
                                     "marital_status": marital, "nationality": "Indian", "photo": "photo.jpg"}),
        verification_status="fresh", email_status="valid",
    )
    protected = [first, last, father, dob, email, phone, phone[-6:], street, religion, cat, marital, gender,
                 "photo.jpg", "Nationality", "Indian national", "IIT Bombay"]
    return meta, protected


def _job(world, org, **settings):
    job = world.job(org, title="Senior Data Engineer", skills=("Kafka", "Python"), experience_min=4, experience_max=10,
                    location="Pune")
    if settings:
        js = TalentJobSettings(job_id=job.id, **settings)
        world.db.merge(js)
        world.db.commit()
    return job


def _run_loop_b(job_id, client=None):
    from app.agent.match_job import match_job

    client = client or CapturingInngest()
    res = mocked.trigger(match_job, inngest.Event(name="talent/match.requested", data={"job_id": job_id}), client)
    assert res.status is mocked.Status.COMPLETED, res.error
    return res.output, client


# ── Redaction ───────────────────────────────────────────────────────────────

def test_protected_attributes_never_reach_100_ranking_prompts(world, db, monkeypatch):
    rng = random.Random(42)
    org = world.org()
    world.enable(org, "match")
    sampled = [_profile_with_protected_data(world, org, i, rng) for i in range(100)]
    job = _job(world, org)
    fake = fakes.install(monkeypatch, fakes.rerank_responder)

    out, _ = _run_loop_b(job.id)
    assert out["reranked"] == 100 and len(fake.calls) == 100

    by_profile = {}
    for params in fake.calls:
        cand = fakes.candidate_payload(params)
        text = params["system"][0]["text"] + "\n" + fakes.user_text(params)
        by_profile[json.dumps(cand, sort_keys=True)] = text
        assert set(cand) <= {"current_role", "current_employer", "industry", "total_experience_years", "skills",
                             "work_history", "education", "certifications", "summary"}
    leaks = []
    for meta, protected in sampled:
        match = db.query(JobMatch).filter_by(job_id=job.id, profile_id=meta.id).one()
        assert set(match.fields_sent) <= set(redaction.ALLOWED_FIELDS)
        # Check this profile's own prompt: find it through the redacted payload it produced.
        version = cv.current_version(db, meta.id)
        payload, _, _ = redaction.build_rank_input(version.parsed, meta.candidate_name)
        prompt = by_profile[json.dumps(payload, sort_keys=True)]
        candidate_part = prompt.split("<untrusted_candidate>", 1)[1]
        for value in protected:
            pattern = rf"\b{re.escape(value)}\b" if re.match(r"^\w", value) else re.escape(value)
            if re.search(pattern, candidate_part, flags=0 if value in ("Male", "Female") else re.I):
                leaks.append((meta.id, value))
        assert not re.search(r"\b(he|she|him|her|his|hers)\b", candidate_part, re.I), candidate_part[:300]
    assert leaks == [], leaks[:20]

    # The work that matters survived redaction.
    some = fakes.candidate_payload(fake.calls[0])
    assert any("Kafka pipelines in Python" in w.get("description", "") for w in some["work_history"])
    assert {"name": "Kafka"} in [{"name": s["name"]} for s in some["skills"]]
    # Every score is audited with the fields that were sent.
    logs = db.query(AuditLog).filter_by(action="match.scored").all()
    assert len(logs) == 100 and all(l.detail["fields_sent"] for l in logs)
    assert all(l.detail["prompt_version"] and l.detail["model"] for l in logs)


def test_redaction_keeps_job_relevant_text():
    payload, fields, counts = redaction.build_rank_input({
        "current_designation": "Will Smith — Platform Lead", "skills": [{"name": "Go", "years": 4}],
        "work_experience": [{"title": "Lead", "company": "Acme", "start_date": "2020-01",
                             "description": "Will Smith will own the migration. He mentored 5 engineers. Grace periods for SLAs."}],
        "education": ["MBA, Harvard (2010)"],
    }, "Will Smith")
    desc = payload["work_history"][0]["description"]
    assert "will own the migration" in desc and "Grace periods" in desc
    assert "Smith" not in json.dumps(payload) and "Harvard" not in json.dumps(payload) and "2010" not in json.dumps(payload)
    assert "they mentored" in desc
    assert payload["education"] == [{"degree_and_field": "MBA"}]
    assert counts["name"] >= 2


# ── Stage 1 / Stage 2 ───────────────────────────────────────────────────────

def test_stage1_filters_only_on_what_the_profile_contradicts(world, db):
    org = world.org()
    js = dict(budget_max_ctc=20.0, max_notice_days=60, relocation_ok=True, locations=["Pune"])
    job = _job(world, org, **js)
    base = dict(skills='["Python", "Kafka"]', verification_status="fresh")
    ok = world.profile(org, email="ok@x.com", experience_years=6, location="Pune", expected_ctc=18, notice_period_days=30, **base)
    unknowns = world.profile(org, email="unk@x.com", **base)  # nothing known: kept, lower score
    too_junior = world.profile(org, email="jr@x.com", experience_years=1, **base)
    too_long_notice = world.profile(org, email="n@x.com", notice_period_days=90, **base)
    too_expensive = world.profile(org, email="c@x.com", expected_ctc=40, **base)
    elsewhere = world.profile(org, email="e@x.com", location="Chennai", **base)
    relocates = world.profile(org, email="r@x.com", location="Chennai", open_to_relocate=True, **base)
    not_looking = world.profile(org, email="nl@x.com", job_search_status="not_looking", **base)
    unreachable = world.profile(org, email="u@x.com", **{**base, "verification_status": "unreachable"})
    no_skills = world.profile(org, email="ns@x.com", skills='["Excel"]', verification_status="fresh")
    other_org = world.profile(world.org("Other"), email="o@x.com", **base)

    req = matching.requirements(db, job)
    ids = {r["profile_id"]: r for r in matching.stage1(db, job, req)}
    assert set(ids) == {ok.id, unknowns.id, relocates.id}
    assert ids[ok.id]["stage1_score"] > ids[unknowns.id]["stage1_score"]
    for excluded in (too_junior, too_long_notice, too_expensive, elsewhere, not_looking, unreachable, no_skills, other_org):
        assert excluded.id not in ids

    # Short skill names match whole words only ("Go" must not match "good").
    job2 = world.job(org, title="Go dev", skills=("Go",), location="Remote")
    world.profile(org, email="good@x.com", raw_text="A good engineer", skills='["Java"]', verification_status="fresh")
    gopher = world.profile(org, email="go@x.com", skills='["Go"]', verification_status="fresh")
    found = {r["profile_id"] for r in matching.stage1(db, job2, matching.requirements(db, job2))}
    assert found == {gopher.id}


def test_stage2_vectors_order_the_pool(world, db):
    org = world.org()
    job = _job(world, org)
    kafka = world.profile(org, email="k@x.com", verification_status="fresh",
                          skills='["Kafka", "Python", "Spark"]', current_designation="Senior Data Engineer")
    web = world.profile(org, email="w@x.com", verification_status="fresh",
                        skills='["Python", "Django", "CSS"]', current_designation="Web Developer")
    for p in (kafka, web):
        cv.ensure_baseline(db, p)
    db.commit()
    ids = backfill.embed_chunk(db, org.id)
    assert ids == 2
    pool = matching.stage1(db, job, matching.requirements(db, job))
    ranked = matching.stage2(db, job, pool)
    assert [r["profile_id"] for r in ranked] == [kafka.id, web.id]
    assert ranked[0]["vector_score"] > ranked[1]["vector_score"]
    model = embeddings.model_name()
    assert db.query(CvVersion).filter(CvVersion.embedding_model == model).count() == 2


# ── Stage 3 ─────────────────────────────────────────────────────────────────

def test_finalize_downgrades_untraceable_evidence_and_computes_the_score():
    payload = {"skills": [{"name": "Kafka"}], "work_history": [{"title": "Data Engineer", "description": "Built Kafka pipelines"}]}
    data = {
        "score": 95, "dimension_scores": {"must_have_coverage": 100, "relevant_experience": 80, "skills_depth": 70,
                                          "domain_fit": 60, "trajectory": 50},
        "must_haves": [
            {"requirement": "Kafka", "met": "yes", "evidence": "Built Kafka pipelines"},
            {"requirement": "Kubernetes", "met": "yes", "evidence": "Ran Kubernetes clusters"},  # invented
            {"requirement": "Leadership", "met": "unclear", "evidence": ""},
        ],
        "strengths": [{"strength": "Streaming", "evidence": "Built Kafka pipelines"}], "gaps": [], "risk_flags": [],
    }
    out = rerank.finalize(data, payload, ["Kafka", "Kubernetes", "Leadership"])
    mh = {m["requirement"]: m for m in out["must_haves"]}
    assert mh["Kafka"]["met"] == "yes" and mh["Kafka"]["evidence_verified"]
    assert mh["Kubernetes"]["met"] == "unclear" and not mh["Kubernetes"]["evidence_verified"]
    assert out["dimension_scores"]["must_have_coverage"] == 67  # (1 + 0.5 + 0.5) / 3
    assert out["ai_score"] == round(0.4 * 67 + 0.25 * 80 + 0.15 * 70 + 0.1 * 60 + 0.1 * 50)
    assert out["model_score"] == 95 and out["evidence"] == {"verified_quotes": 1, "unverified_quotes": 1}


def test_loop_b_end_to_end_is_idempotent_and_flags_stale_matches(world, db, monkeypatch):
    org = world.org()
    world.enable(org, "match")
    job = _job(world, org, top_k=3)
    strong_stale = world.profile(org, email="s@x.com", verification_status="stale", skills='["Kafka", "Python"]',
                                 experience_years=6, work_experience=json.dumps([{"title": "Data Engineer", "company": "A",
                                                                                  "description": "Kafka and Python"}]))
    strong_fresh = world.profile(org, email="f@x.com", verification_status="fresh", skills='["Kafka", "Python"]',
                                 experience_years=6, work_experience=json.dumps([{"title": "Data Engineer", "company": "B"}]))
    weak = world.profile(org, email="w@x.com", verification_status="fresh", skills='["Python"]', experience_years=6,
                         work_experience=json.dumps([{"title": "Analyst", "company": "C"}]))
    fake = fakes.install(monkeypatch, fakes.rerank_responder)

    out, client = _run_loop_b(job.id)
    assert out["reranked"] == 3 and len(fake.calls) == 3
    rows = match_loop.ordered_matches(db, job.id)
    assert [p.id for _, p in rows][-1] == weak.id
    by_profile = {m.profile_id: m for m, _ in rows}
    assert by_profile[strong_stale.id].status == "needs_refresh"
    assert by_profile[strong_fresh.id].status == "ranked"
    assert by_profile[strong_fresh.id].ai_score > by_profile[weak.id].ai_score
    assert db.query(CampaignMember).filter_by(profile_id=strong_stale.id).one().priority == match_loop.REFRESH_PRIORITY
    assert [e.name for e in client.sent] == ["talent/match.completed", "talent/refresh.sweep.requested"]
    assert db.get(TalentJobSettings, job.id).match_status == "done"

    _run_loop_b(job.id)
    assert len(fake.calls) == 3  # nothing changed: nothing re-scored
    weak_db = db.get(ResumeMetadata, weak.id)
    weak_db.skills = json.dumps(["Python", "Kafka"])
    db.commit()
    v = cv.current_version(db, weak.id)
    v.parsed = {**v.parsed, "skills": [{"name": "Python"}, {"name": "Kafka"}]}
    db.commit()
    _run_loop_b(job.id)
    assert len(fake.calls) == 4  # only the changed profile


def test_refusals_and_missing_ai_never_produce_a_score(world, db, monkeypatch):
    org = world.org()
    world.enable(org, "match")
    job = _job(world, org)
    p = world.profile(org, email="r@x.com", verification_status="fresh", skills='["Kafka", "Python"]',
                      work_experience=json.dumps([{"title": "Engineer", "company": "X"}]))
    fakes.install(monkeypatch, lambda params: "REFUSE")
    _run_loop_b(job.id)
    m = db.query(JobMatch).filter_by(profile_id=p.id).one()
    assert m.ai_score is None and m.status_reason == "model_declined" and m.status == "ranked"
    assert db.query(AuditLog).filter_by(action="match.score_unavailable").count() == 1


def test_loop_switch_and_job_hook(world, db, events):
    from app.talent import hooks

    org = world.org()
    job = _job(world, org)
    out, _ = _run_loop_b(job.id)
    assert out == {"skip": "loop_off"}
    hooks.job_changed(job.id)
    assert events == []
    world.enable(org, "match")
    hooks.job_changed(job.id)
    assert events == [("talent/job.updated", {"job_id": job.id})]


# ── API, ratings, precision@10 ──────────────────────────────────────────────

def test_job_api_settings_matches_and_precision(client, world, db, monkeypatch, events):
    org = world.org()
    world.enable(org, "match")
    admin = world.user(org)
    from app.models.user import UserRole

    recruiter = world.user(org, UserRole.recruiter)
    job = _job(world, org)
    for i in range(12):
        world.profile(org, email=f"p{i}@x.com", verification_status="fresh",
                      skills='["Kafka", "Python"]' if i % 2 else '["Python"]',
                      work_experience=json.dumps([{"title": "Engineer", "company": f"C{i}"}]))
    h, hr = world.headers(admin), world.headers(recruiter)
    r = client.put(f"/talent/jobs/{job.id}/settings", json={"autonomy_mode": "autopilot"}, headers=hr)
    assert r.status_code == 403  # recruiters can't switch a job to autopilot
    r = client.put(f"/talent/jobs/{job.id}/settings",
                   json={"must_haves": ["Kafka in production", "  "], "top_k": 10, "budget_max_ctc": 30}, headers=h)
    assert r.status_code == 200 and r.json()["must_haves"] == ["Kafka in production"]
    assert client.put(f"/talent/jobs/{job.id}/settings", json={"question_count": 3}, headers=h).status_code == 400
    assert client.post(f"/talent/jobs/{job.id}/match", headers=hr).json() == {"queued": True}
    assert ("talent/match.requested", {"job_id": job.id}) in events

    fakes.install(monkeypatch, fakes.rerank_responder)
    _run_loop_b(job.id)
    items = client.get(f"/talent/jobs/{job.id}/matches", headers=hr).json()["items"]
    assert len(items) == 12 and items[0]["rank"] == 1 and items[0]["must_haves"]
    assert items[0]["profile"]["name"]  # recruiters see who it is; the model never did
    for item in items[:10]:
        rel = "Kafka" in json.dumps(item["strengths"])
        assert client.post(f"/talent/matches/{item['id']}/rating", json={"relevant": rel}, headers=hr).status_code == 200
    rep = client.get("/talent/reports/precision", headers=h).json()
    assert rep["jobs_fully_rated"] == 1
    top10 = items[:10]
    expected = sum("Kafka" in json.dumps(i["strengths"]) for i in top10) / 10
    assert rep["mean_precision_at_10"] == round(expected, 3)
    other = world.user(world.org("Other"))
    assert client.get(f"/talent/jobs/{job.id}/matches", headers=world.headers(other)).status_code == 404


# ── Backfill ────────────────────────────────────────────────────────────────

def test_backfill_embeds_only_and_never_reparses_with_claude(client, world, db, events):
    org = world.org()
    admin = world.user(org)
    world.profile(org, email="bf@x.com", raw_text="Data Engineer at Northwind since 2019-04. Kafka, Python. " * 10)
    assert backfill.ensure_baselines(db, org.id) == 1
    h = world.headers(admin)
    assert client.get("/talent/backfill/estimate", headers=h).json() == {"embeddings_missing": 1}
    r = client.post("/talent/backfill", json={"kind": "parse"}, headers=h)
    assert r.status_code == 400 and "no Claude re-parse" in r.json()["detail"]
    assert not events
    assert client.get("/talent/backfill/runs", headers=h).status_code == 404
    assert client.post("/talent/backfill", json={}, headers=h).json() == {"queued": True}
    assert events == [("talent/backfill.requested", {"organization_id": org.id, "kind": "embed", "limit": 50000,
                                                     "actor": f"user:{admin.id}"})]
    assert backfill.embed_chunk(db, org.id) == 1
    assert backfill.status(db, org.id) == {"embeddings_missing": 0}


def test_new_uploads_are_embedded_on_schedule_for_orgs_with_match_on(world, db):
    from app.agent.cv_backfill import cv_backfill

    on, off = world.org("On"), world.org("Off")
    world.enable(on, "match")
    new = world.profile(on, email="new@x.com", raw_text="Data Engineer. Kafka, Python, Spark. " * 10)
    other = world.profile(off, email="other@x.com", raw_text="Data Engineer. Kafka, Python, Spark. " * 10)
    assert {type(t).__name__ for t in cv_backfill._triggers} == {"TriggerCron", "TriggerEvent"}

    result = mocked.trigger(cv_backfill, inngest.Event(name="inngest/scheduled.timer", data={}), CapturingInngest())
    assert result.output == {"baselines": 1, "embedded": 1}
    db.expire_all()
    assert cv.current_version(db, new.id).embedding_model == embeddings.model_name()
    assert cv.current_version(db, other.id) is None  # Match is off there: left alone

    # Embed profiles (one organization) still works whatever its switches.
    result = mocked.trigger(cv_backfill, inngest.Event(name="talent/backfill.requested",
                                                       data={"organization_id": off.id, "kind": "embed"}),
                            CapturingInngest())
    assert result.output == {"baselines": 1, "embedded": 1}


def test_total_experience_counts_overlaps_once():
    from datetime import date

    from app.talent.parsing import total_experience_years

    roles = [{"start": "2015-01", "end": "2018-12"}, {"start": "2018-06", "end": "2020-12"},
             {"start": "2022-01", "end": None}]
    assert total_experience_years(roles, today=date(2025, 12, 15)) == 10.0  # 72 + 48 months
