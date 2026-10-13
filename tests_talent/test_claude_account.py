"""When the Anthropic account can't be used at all (no credit, bad key), the
job page and Settings say so, and a match run stops calling Claude."""
import json

import anthropic
import httpx
import inngest
from inngest.experimental import mocked

from app.agent.match_job import match_job
from app.talent import claude, match_loop
from tests_talent import fakes
from tests_talent.conftest import CapturingInngest
from tests_talent.test_match_progress import _job, _settings

NO_CREDIT = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing."


def _bad_request(message):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(message, response=httpx.Response(400, request=req),
                                     body={"error": {"type": "invalid_request_error", "message": message}})


def test_account_problems_are_named():
    assert "out of credit" in claude.account_problem_from(_bad_request(NO_CREDIT))
    assert claude.account_problem_from(_bad_request("max_tokens: too large")) is None
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    auth = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=req), body=None)
    assert "invalid or revoked" in claude.account_problem_from(auth)


def test_match_run_without_credit_says_why_and_stops_calling(world, db, monkeypatch):
    org = world.org()
    world.enable(org, "match")
    for i in range(12):
        world.profile(org, email=f"p{i}@x.com", verification_status="fresh", skills='["Kafka", "Python"]',
                      work_experience=json.dumps([{"title": "Engineer", "company": f"C{i}"}]))
    job = _job(world, org)

    def no_credit(params):
        raise _bad_request(NO_CREDIT)

    fake = fakes.install(monkeypatch, no_credit)
    match_loop.request(db, job.id)
    mocked.trigger(match_job, inngest.Event(name="talent/match.requested", data={"job_id": job.id}), CapturingInngest())
    js = _settings(db, job.id)
    assert js.match_status == "done"  # ranked without AI scores, not stuck
    assert "out of credit" in js.match_progress["ai_error"]
    # The first failing chunk stops the rest of the run from calling Claude.
    assert len(fake.calls) <= 10

    # A new run starts clean (credit may have been added meanwhile).
    match_loop.request(db, job.id)
    match_loop.start(db, job.id)
    assert _settings(db, job.id).match_progress.get("ai_error") is None


def test_settings_warns_when_the_account_cannot_be_used(client, world, monkeypatch):
    h = world.headers(world.user(world.org()))
    monkeypatch.setattr(claude.settings, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(claude, "account_problem", lambda max_age=600: "The Anthropic account is out of credit")
    assert "The Anthropic account is out of credit" in client.get("/talent/settings", headers=h).json()["warnings"]


def test_a_request_during_a_run_keeps_its_progress_on_screen(world, db):
    org = world.org()
    world.enable(org, "match")
    job = _job(world, org)
    match_loop.request(db, job.id)
    match_loop.start(db, job.id)
    before = dict(_settings(db, job.id).match_progress)
    assert match_loop.request(db, job.id) is None  # e.g. the job was edited mid-run
    js = _settings(db, job.id)
    assert js.match_status == "running" and js.match_progress["stage"] == before["stage"]
    assert js.match_progress["rerun_requested"] is True
