"""What the SMTP transport puts on the wire (found by the local end-to-end run:
messages went out with bare LF line endings, so a strict server saw one
endless line and refused them)."""
from datetime import datetime

from app.talent import templates, transports
from app.talent.consent import _SEAL, ContactPermit


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def ehlo(self):
        return 250, b"ok"

    def starttls(self):
        return 220, b"ok"

    def login(self, user, password):
        return 235, b"ok"

    def mail(self, sender):
        return 250, b"ok"

    def rcpt(self, to):
        return 250, b"ok"

    def data(self, payload):
        FakeSMTP.sent.append(payload)
        return 250, b"ok"

    def quit(self):
        pass


def _long_email():
    ctx = {"org_name": "Acme", "brand": templates.brand_context("Acme", color="#0a7c3e", signature="Priya\nTalent team"),
           "first_name": "Ravi", "job_title": "Senior Data Engineer", "job_location": "Pune",
           "role_summary": "Build streaming pipelines with Kafka and Spark — " * 20,
           "action_url": "https://app.test/t/invite/" + "x" * 43, "unsubscribe_url": "https://app.test/u",
           "my_data_url": "https://app.test/d"}
    subject, text, html = templates.render("invite", ctx)
    return transports.OutgoingEmail(subject=subject, text=text, html=html, from_name="Acme", message_id_hint="abc",
                                    unsubscribe_url="https://api.test/talent/public/unsubscribe/x")


def test_smtp_sends_crlf_lines_within_the_rfc_limit(monkeypatch):
    monkeypatch.setattr(transports.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(transports.settings, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(transports.settings, "SMTP_USER", "no-reply@acme.test")
    monkeypatch.setattr(transports.settings, "SMTP_PASS", "pw")
    FakeSMTP.sent.clear()
    permit = ContactPermit(1, "email", "jobs", "ravi@mail.test", datetime.utcnow(), _SEAL)
    transports.SmtpTransport().send(permit, _long_email())
    wire = FakeSMTP.sent[0]
    assert len(wire) > 2000  # long enough that one unbroken "line" would exceed 1000 bytes
    assert wire.count(b"\n") == wire.count(b"\r\n"), "bare LF on the wire"
    assert max(len(line) for line in wire.split(b"\r\n")) <= 998
