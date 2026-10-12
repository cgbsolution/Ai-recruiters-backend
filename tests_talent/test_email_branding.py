"""Profile-update emails every 3 months, branded per organization."""
from datetime import datetime

from app.models.talent import TalentSettings
from app.talent import campaigns, settings as talent_settings, templates, transports
from app.talent import outbox as outbox_module


def _ctx(**brand):
    return {
        "org_name": "Acme", "brand": templates.brand_context("Acme", **brand), "first_name": "Asha",
        "profile_role": "Data Engineer", "profile_skills": ["Python", "Kafka"], "profile_updated": "July 2026",
        "action_url": "https://app.test/t/refresh/abc", "unsubscribe_url": "https://app.test/u", "my_data_url": "https://app.test/d",
    }


def test_new_organizations_are_asked_every_three_months(db):
    row = talent_settings.get_settings(db, None)
    assert row.refresh_after_days == 90
    assert talent_settings.freshness_window(row).days == 90


def test_update_email_is_personal_branded_and_shows_what_we_hold():
    subject, text, html = templates.render("refresh_touch_1", _ctx(
        name="Acme Talent", logo_url="https://cdn.acme.test/logo.png", color="#0a7c3e",
        signature="Priya\nTalent team, Acme", message="We hire for data roles across Pune and Bengaluru."))
    assert subject == "Asha, is your profile with Acme Talent still up to date?"
    assert '<img src="https://cdn.acme.test/logo.png"' in html
    assert "background:#0a7c3e" in html and "Update my profile" in html
    assert "Priya<br>Talent team, Acme" in html
    assert "We hire for data roles across Pune and Bengaluru." in html  # the organization's own words
    for shown in ("Data Engineer", "Python, Kafka", "July 2026"):
        assert shown in html and shown in text
    assert "https://app.test/t/refresh/abc" in text and "Priya" in text


def test_reminders_are_shorter_and_the_last_one_offers_a_way_out():
    _, _, second = templates.render("refresh_touch_2", _ctx())
    subject, _, third = templates.render("refresh_touch_3", _ctx())
    assert "Quick reminder" in templates.render("refresh_touch_2", _ctx())[0]
    assert subject == "Last reminder: keep your Acme profile current"
    assert "Not looking at the moment?" in third and "What we have on file" not in third
    assert "What we have on file" in second


def test_unsafe_or_broken_branding_never_reaches_an_email():
    subject, _, html = templates.render("refresh_touch_1", _ctx(
        name="<b>Evil</b>\r\nBcc: x@y.z {org}", logo_url="javascript:alert(1)", color="red;background:url(x)"))
    assert "<b>Evil</b>" not in html and "&lt;b&gt;Evil&lt;/b&gt;" in html
    assert "\n" not in subject and "\r" not in subject and "{org}" in subject  # treated as text, not a template
    assert "javascript:" not in html and "<img" not in html
    assert f"background:{templates.DEFAULT_COLOR}" in html and "url(x)" not in html
    _, _, plain_http = templates.render("refresh_touch_1", _ctx(logo_url="http://cdn.acme.test/logo.png"))
    assert "<img" not in plain_http


def test_button_text_stays_readable_on_light_brand_colours():
    _, _, light = templates.render("refresh_touch_1", _ctx(color="#ffd60a"))
    _, _, dark = templates.render("refresh_touch_1", _ctx(color="#1d3557"))
    assert "background:#ffd60a;color:#101828" in light
    assert "background:#1d3557;color:#ffffff" in dark


def test_sent_email_uses_the_organizations_branding_and_a_real_from_address(world, db, outbox, monkeypatch):
    monkeypatch.setattr(transports.settings, "EMAIL_FROM", "Anytimehire.ai")  # a display name, as configured
    monkeypatch.setattr(transports.settings, "SMTP_USER", "no-reply@cgbindia.com")
    org = world.org("Acme")
    world.enable(org, "refresh")
    db.query(TalentSettings).filter_by(organization_id=org.id).update(
        {"brand_name": "Acme Careers", "brand_color": "#0a7c3e"})
    db.commit()
    p = world.profile(org, email="asha@acme.com", email_status="valid", candidate_name="Asha Rao",
                      current_designation="Data Engineer", skills='["Python", "Kafka", "Spark"]',
                      last_verified_at=datetime(2026, 7, 1))
    world.consent(p)
    campaign = campaigns.get_or_create_campaign(db, org.id, "refresh", "test", "Test")
    result = outbox_module.send_touch(db, campaign_id=campaign.id, profile_id=p.id, touch_no=1, kind="refresh_touch_1")
    assert result.status == "sent"
    msg = outbox[0]  # the fixture: messages the memory transport recorded
    assert msg["headers"]["From"] == "Acme Careers <no-reply@cgbindia.com>"
    assert msg["subject"] == "Asha, is your profile with Acme Careers still up to date?"
    assert "Python, Kafka, Spark" in msg["html"] and "July 2026" in msg["html"]
    assert "background:#0a7c3e" in msg["html"]


def test_sender_address_reads_email_from_only_when_it_is_an_address(monkeypatch):
    monkeypatch.setattr(transports.settings, "SMTP_USER", "smtp@corp.test")
    monkeypatch.setattr(transports.settings, "EMAIL_FROM", "Anytimehire.ai")
    assert transports.sender_address() == "smtp@corp.test"
    assert transports.sender_domain() == "corp.test"
    monkeypatch.setattr(transports.settings, "EMAIL_FROM", "Careers <careers@acme.test>")
    assert transports.sender_address() == "careers@acme.test"


def test_branding_settings_api_validates_and_previews(client, world, db):
    org = world.org("Acme")
    headers = world.headers(world.user(org))
    bad_logo = client.put("/talent/settings", json={"brand_logo_url": "http://x.test/l.png"}, headers=headers)
    bad_color = client.put("/talent/settings", json={"brand_color": "green"}, headers=headers)
    assert bad_logo.status_code == 400 and bad_color.status_code == 400
    ok = client.put("/talent/settings", headers=headers, json={
        "brand_name": "Acme Careers", "brand_logo_url": "https://cdn.acme.test/l.png", "brand_color": "#0A7C3E",
        "email_signature": "Priya\nTalent team", "refresh_message": "We hire across India.", "refresh_after_days": 90})
    assert ok.status_code == 200, ok.text
    branding = ok.json()["branding"]
    assert branding["brand_color"] == "#0a7c3e" and branding["email_signature"] == "Priya\nTalent team"
    preview = client.get("/talent/settings/email-preview?touch=1", headers=headers).json()
    assert preview["from_name"] == "Acme Careers" and preview["refresh_after_days"] == 90
    assert "We hire across India." in preview["html"] and "https://cdn.acme.test/l.png" in preview["html"]
    cleared = client.put("/talent/settings", json={"brand_name": "", "brand_color": ""}, headers=headers).json()
    assert cleared["branding"]["brand_name"] is None and cleared["branding"]["default_name"] == "Acme"
