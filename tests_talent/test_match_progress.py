"""Matching shows what it is doing (a recruiter clicked "Run matching" in
production and saw "Matching now…" forever: the Match switch was off or no
worker was running, and nothing said so)."""
import json
from datetime import datetime, timedelta

import inngest
from inngest.experimental import mocked

from app.agent import client as agent_client
from app.agent.match_job import match_job, match_job_on_edit
from app.models.talent import TalentJobSettings
from app.talent import heartbeat, match_loop
from tests_talent import fakes
from tests_talent.conftest import CapturingInngest


def _job(world, org):
    return world.job(org, title="Senior Data Engineer", skills=("Kafka", "Python"), experience_min=4,
                     experience_max=10, location="Pune")


def _settings(db, job_id) -> TalentJobSettings:
    db.expire_all()
    return db.query(TalentJobSettings).filter_by(job_id=job_id).one()


def test_run_matching_says_why_it_cannot_run(client, world, db, events):
    org = world.org()
    h = world.headers(world.user(org))
    job = _job(world, org)
    r = client.post(f"/talent/jobs/{job.id}/match", headers=h)
    assert r.status_code == 409 and "Loop B · Match is switched off" in r.json()["detail"]
    assert events == []  # nothing queued that could never start
    world.enable(org, "match")
    job.status = type(job.status)("Draft")
    db.commit()
    r = client.post(f"/talent/jobs/{job.id}/match", headers=h)
    assert r.status_code == 409 and "Only open jobs" in r.json()["detail"]


def test_a_request_the_worker_never_receives_is_reported(client, world, db, monkeypatch):
    org = world.org()
    world.enable(org, "match")
    h = world.headers(world.user(org))
    job = _job(world, org)
    monkeypatch.setattr(agent_client, "_sink", None)
    monkeypatch.setattr(agent_client, "_can_send", lambda: False)  # e.g. no Inngest event key
    r = client.post(f"/talent/jobs/{job.id}/match", headers=h)
    assert r.status_code == 503 and "background worker" in r.json()["detail"]
    js = _settings(db, job.id)
    assert js.match_status == "failed" and js.match_progress["stage"] == "failed"


def test_progress_moves_through_every_stage(world, db, monkeypatch):
    org = world.org()
    world.enable(org, "match")
    job = _job(world, org)
    for i in range(5):
        world.profile(org, email=f"p{i}@x.com", verification_status="fresh", skills='["Kafka", "Python"]',
                      work_experience=json.dumps([{"title": "Engineer", "company": f"C{i}"}]))
    assert match_loop.request(db, job.id) is None
    assert _settings(db, job.id).match_progress["stage"] == "queued"
    fakes.install(monkeypatch, fakes.rerank_responder)
    seen = []
    real = match_loop.chunk_scored

    def spy(db_, job_id, count):
        real(db_, job_id, count)
        seen.append(dict(_settings(db, job_id).match_progress))
    monkeypatch.setattr(match_loop, "chunk_scored", spy)
    result = mocked.trigger(match_job, inngest.Event(name="talent/match.requested", data={"job_id": job.id}),
                            CapturingInngest())
    assert result.error is None, result.error
    assert seen and seen[-1]["stage"] == "scoring" and seen[-1]["scored"] == seen[-1]["total"] == 5
    final = _settings(db, job.id)
    assert final.match_status == "done"
    assert final.match_progress["stage"] == "done" and final.match_progress["pool"] == 5
    assert final.match_progress["matches"] == 5


def test_a_queued_run_that_can_no_longer_go_ahead_says_why(world, db):
    org = world.org()
    world.enable(org, "match")
    job = _job(world, org)
    assert match_loop.request(db, job.id) is None
    world.enable(org)  # Match switched off while the run waited
    from app.talent.settings import get_or_create_settings
    row = get_or_create_settings(db, org.id)
    row.flags = {**(row.flags or {}), "match": False}
    db.commit()
    assert match_loop.start(db, job.id) == {"skip": "loop_off"}
    js = _settings(db, job.id)
    assert js.match_status == "skipped" and "switched off" in js.match_error


def test_job_edits_settle_but_the_button_does_not_wait():
    assert match_job_on_edit._opts.debounce is not None
    assert getattr(match_job._opts, "debounce", None) is None
    assert [t.event for t in match_job._triggers] == ["talent/match.requested"]


def test_job_edit_queues_a_run_through_the_same_checks(world, db):
    org = world.org()
    job = _job(world, org)
    result = mocked.trigger(match_job_on_edit, inngest.Event(name="talent/job.updated", data={"job_id": job.id}),
                            CapturingInngest())
    assert result.output == {"skip": "loop_off"}
    world.enable(org, "match")
    result = mocked.trigger(match_job_on_edit, inngest.Event(name="talent/job.updated", data={"job_id": job.id}),
                            CapturingInngest())
    assert result.output == {"requested": True}
    assert _settings(db, job.id).match_status == "queued"


def test_worker_heartbeat_and_warnings(client, world, db):
    org = world.org()
    world.enable(org, "match")
    h = world.headers(world.user(org))
    warnings = client.get("/talent/settings", headers=h).json()["warnings"]
    assert any("worker has never checked in" in w for w in warnings)
    job = _job(world, org)
    match_loop.request(db, job.id, now=datetime.utcnow() - timedelta(minutes=10))
    detail = client.get(f"/talent/jobs/{job.id}", headers=h).json()
    assert detail["worker"]["ok"] is False and "never checked in" in detail["stuck_hint"]
    heartbeat.beat(db)
    assert not any("worker" in w for w in client.get("/talent/settings", headers=h).json()["warnings"])
    assert client.get(f"/talent/jobs/{job.id}", headers=h).json()["worker"]["ok"] is True
    heartbeat.beat(db, now=datetime.utcnow() - timedelta(hours=1))
    assert any("last checked in" in w for w in client.get("/talent/settings", headers=h).json()["warnings"])
