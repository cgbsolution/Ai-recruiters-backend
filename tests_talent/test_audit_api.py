"""GET /talent/audit: several actions / actors at once, text search, a time
window, and per-option counts for the filter menus."""
from datetime import datetime, timedelta

from app.talent.audit import audit


def _seed(db, org_id, other_org_id):
    old = datetime.utcnow() - timedelta(days=40)
    rows = [
        ("agent", "match.completed", "job", "29"),
        ("agent", "match.completed", "job", "30"),
        ("user:2", "match.requested", "job", "29"),
        ("user:2", "settings.updated", "organization", str(org_id)),
        ("candidate", "refresh.confirmed", "cv_version", "abc123"),
    ]
    for actor, action, st, sid in rows:
        audit(db, actor=actor, action=action, subject_type=st, subject_id=sid, organization_id=org_id)
    audit(db, actor="agent", action="match.completed", subject_type="job", subject_id="99", organization_id=other_org_id)
    db.commit()
    entry = audit(db, actor="agent", action="retention.deleted", subject_type="profile", subject_id="7",
                  organization_id=org_id)
    db.flush()
    entry.at = old
    db.commit()


def test_audit_filters_search_window_and_counts(client, world, db):
    org, other = world.org(), world.org("Other")
    _seed(db, org.id, other.id)
    h = world.headers(world.user(org))
    get = lambda qs="": client.get(f"/talent/audit{qs}", headers=h).json()  # noqa: E731

    everything = get()
    assert everything["total"] == 6  # never another organization's rows
    assert everything["action_counts"]["match.completed"] == 2
    assert everything["actor_counts"] == {"agent": 3, "user:2": 2, "candidate": 1}

    two = get("?action=match&action=settings")  # prefixes, OR-ed
    assert two["total"] == 4
    # Each menu is counted under the other filters: actions ignore the action pick…
    assert two["action_counts"]["refresh.confirmed"] == 1
    # …actors honour it.
    assert two["actor_counts"] == {"agent": 2, "user:2": 2}

    both = get("?action=match&actor=user:2&actor=candidate")
    assert both["total"] == 1 and both["items"][0]["action"] == "match.requested"
    assert both["actor_counts"]["candidate"] == 0  # a picked option stays listed

    assert get("?q=abc1")["total"] == 1  # subject id
    assert get("?q=SETTINGS")["total"] == 1  # case-insensitive, action
    since = (datetime.utcnow() - timedelta(days=30)).isoformat()
    recent = get(f"?since={since}")
    assert recent["total"] == 5 and "retention.deleted" not in recent["action_counts"]

    page = get("?page=2&page_size=4")
    assert page["total"] == 6 and len(page["items"]) == 2
    assert get("?action=match.requested")["items"][0]["subject_id"] == "29"  # old single-value form still works
