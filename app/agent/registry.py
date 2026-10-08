"""Every Inngest function the Talent Agent serves."""

from app.agent.cv_backfill import cv_backfill
from app.agent.interview_conduct import interview_conduct, interview_prepare
from app.agent.interview_reminders import interview_reminders
from app.agent.interview_schedule import interview_schedule
from app.agent.interview_score import interview_score, shortlist_build
from app.agent.invite_plan import invite_plan
from app.agent.invite_send import invite_send
from app.agent.match_job import match_job
from app.agent.refresh_dead_letter import refresh_dead_letter
from app.agent.refresh_reengage import refresh_reengage
from app.agent.refresh_respond import refresh_respond
from app.agent.refresh_sweep import refresh_sweep
from app.agent.refresh_touch import refresh_touch

FUNCTIONS = [
    # Loop A — refresh
    refresh_sweep,
    refresh_touch,
    refresh_respond,
    refresh_reengage,
    refresh_dead_letter,
    # Loop B — match
    match_job,
    cv_backfill,
    # Loop C — rank and invite
    invite_plan,
    invite_send,
    # Loop D — schedule, interview, score
    interview_schedule,
    interview_reminders,
    interview_prepare,
    interview_conduct,
    interview_score,
    shortlist_build,
]
