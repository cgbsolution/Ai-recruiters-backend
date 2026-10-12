"""A newer resume replaces a profile's file: the profile keeps its id, every job
candidate linked to it follows the new file, and the old file goes only when
nothing uses it (app.services.resume_links)."""
import pytest

from app import media
from app.models.candidate import Candidate
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import CvVersion
from app.routers.file_manager import _resume_in_use
from app.services import resume_links
from app.talent import cv


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "UPLOAD_ROOT", str(tmp_path))
    (tmp_path / "resumes").mkdir()

    def make(name):
        (tmp_path / "resumes" / name).write_bytes(b"%PDF-1.4 test")
        return f"uploads/resumes/{name}"

    make.dir = tmp_path / "resumes"
    return make


def _candidate(db, job, profile=None, path=None, email="c@x.com"):
    c = Candidate(name="Asha Rao", email=email, job_id=job.id, resume_path=path,
                  resume_metadata_id=profile.id if profile else None)
    db.add(c)
    db.commit()
    return c.id


def test_linked_job_candidates_follow_the_new_resume(world, db, files):
    org = world.org()
    old, new, other = files("old.pdf"), files("new.pdf"), files("other.pdf")
    profile = world.profile(org, email="asha@x.com", resume_file_path=old)
    pid = profile.id
    in_job_a = _candidate(db, world.job(org, title="A"), profile, old)
    in_job_b = _candidate(db, world.job(org, title="B"), profile, old)
    unrelated = _candidate(db, world.job(org, title="C"), None, other, email="o@x.com")

    profile.resume_file_path = new
    db.commit()
    db.expire_all()

    assert db.get(ResumeMetadata, pid).resume_file_path == new  # same profile id
    assert db.get(Candidate, in_job_a).resume_path == new
    assert db.get(Candidate, in_job_b).resume_path == new
    assert db.get(Candidate, unrelated).resume_path == other
    assert not (files.dir / "old.pdf").exists()  # nothing uses it any more
    assert (files.dir / "new.pdf").exists() and (files.dir / "other.pdf").exists()


def test_old_file_stays_while_another_record_uses_it(world, db, files):
    org = world.org()
    old, new = files("old.pdf"), files("new.pdf")
    profile = world.profile(org, email="asha@x.com", resume_file_path=old)
    _candidate(db, world.job(org), None, old)  # an unlinked application of the same file
    profile.resume_file_path = new
    db.commit()
    assert (files.dir / "old.pdf").exists()


def test_rollback_deletes_nothing(world, db, files):
    org = world.org()
    old, new = files("old.pdf"), files("new.pdf")
    profile = world.profile(org, email="asha@x.com", resume_file_path=old)
    profile.resume_file_path = new
    db.flush()
    db.rollback()
    assert (files.dir / "old.pdf").exists()


def test_talent_agent_keeps_history_and_matches_on_the_new_resume(world, db, files):
    org = world.org()
    old, new = files("old.pdf"), files("new.pdf")
    profile = world.profile(org, email="asha@x.com", resume_file_path=old, skills='["Excel"]')
    cv.ensure_baseline(db, profile)
    db.commit()

    profile.resume_file_path = new
    profile.skills = '["Excel", "Power BI"]'
    db.commit()

    versions = db.query(CvVersion).filter_by(profile_id=profile.id).order_by(CvVersion.version_no).all()
    assert [(v.version_no, v.is_current, v.source) for v in versions] == [
        (1, False, "backfill"), (2, True, "resume_upload")]
    assert versions[1].parsed["skills"] == ["Excel", "Power BI"]
    assert (files.dir / "old.pdf").exists()  # kept as version 1 until its retention date


def test_the_agents_own_refresh_upload_is_not_recorded_twice(world, db, files):
    org = world.org()
    old, new = files("old.pdf"), files("new.pdf")
    profile = world.profile(org, email="asha@x.com", resume_file_path=old)
    cv.ensure_baseline(db, profile)
    version = cv.new_version(db, profile, source="refresh_upload", file_path=new)
    db.commit()
    cv.make_current(db, version)  # what cv_pipeline.apply_parse does…
    profile.resume_file_path = new  # …together with pointing the profile at the new file
    db.commit()
    assert db.query(CvVersion).filter_by(profile_id=profile.id).count() == 2


def test_find_profile_ignores_letter_case_and_shared_inboxes(world, db):
    org, other_org = world.org(), world.org("Other")
    asha = world.profile(org, email="Asha.Rao@Mail.com")
    world.profile(other_org, email="asha.rao@mail.com")
    hr = world.profile(org, email="hr@agency.com")
    assert resume_links.find_profile(db, org.id, "asha.rao@MAIL.com").id == asha.id
    assert resume_links.find_profile(db, org.id, "hr@agency.com") is None
    assert resume_links.find_profile(db, org.id, "hr@agency.com", include_shared_inboxes=True).id == hr.id
    assert resume_links.find_profile(db, org.id, "pending_1@resume.local") is None


def test_repair_points_broken_job_links_at_the_profiles_file(world, db, files):
    org = world.org()
    current = files("current.pdf")
    profile = world.profile(org, email="asha@x.com", resume_file_path=current)
    broken = _candidate(db, world.job(org), profile, "uploads/resumes/deleted-long-ago.pdf")

    assert resume_links.repair_links(db)["would_fix"] == 1
    assert db.get(Candidate, broken).resume_path.endswith("deleted-long-ago.pdf")  # dry run changes nothing
    assert resume_links.repair_links(db, apply=True)["fixed"] == 1
    db.expire_all()
    assert db.get(Candidate, broken).resume_path == current


def test_file_manager_refuses_to_delete_a_resume_in_use(world, db, files):
    org = world.org()
    used, loose = files("used.pdf"), files("loose.pdf")
    world.profile(org, email="asha@x.com", resume_file_path=used)
    assert _resume_in_use(db, "resumes", str(files.dir), str(files.dir / "used.pdf"))
    assert not _resume_in_use(db, "resumes", str(files.dir), str(files.dir / "loose.pdf"))
    assert not _resume_in_use(db, "photos", str(files.dir), str(files.dir / "used.pdf"))
