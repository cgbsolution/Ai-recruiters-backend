"""Uploaded resumes are stored only when safe (app.services.file_safety)."""
import hashlib
import io
import socket
import threading
import zipfile

import pikepdf
import pytest
from docx import Document

from app.services import file_safety
from app.services.file_safety import UnsafeFile, check_resume_file, store_resume_upload

# ── Builders ─────────────────────────────────────────────────────────────────


def _pdf(path, tweak=None, **save):
    pdf = pikepdf.new()
    pdf.add_blank_page()
    if tweak:
        tweak(pdf)
    pdf.save(path, **save)
    return str(path)


def _docx(path, extra=None, rels=None):
    buf = io.BytesIO()
    Document().save(buf)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if rels and item.filename == "word/_rels/document.xml.rels":
                data = data.replace(b"</Relationships>", rels.encode() + b"</Relationships>")
            out.writestr(item, data)
        for name, data in (extra or {}).items():
            out.writestr(name, data)
    return str(path)


def _refused(path, match, **kw):
    with pytest.raises(UnsafeFile, match=match):
        check_resume_file(path, **kw)


# ── PDF ──────────────────────────────────────────────────────────────────────


def test_plain_pdf_with_links_is_accepted(tmp_path):
    def link(pdf):
        pdf.pages[0].Annots = pdf.make_indirect(pikepdf.Array([pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=[0, 0, 10, 10],
            A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String("https://linkedin.com/in/x")))]))
    assert check_resume_file(_pdf(tmp_path / "cv.pdf", link)) == "pdf"


def test_pdf_javascript_is_refused(tmp_path):
    def js(pdf):
        pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)"))
    _refused(_pdf(tmp_path / "js.pdf", js), "JavaScript")


def test_pdf_javascript_hidden_in_a_compressed_object_stream_is_refused(tmp_path):
    def js(pdf):
        pdf.Root.OpenAction = pdf.make_indirect(
            pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)")))
    path = _pdf(tmp_path / "hidden.pdf", js, object_stream_mode=pikepdf.ObjectStreamMode.generate,
                compress_streams=True)
    assert b"/JavaScript" not in open(path, "rb").read()  # a text search would miss it
    _refused(path, "JavaScript")


def test_pdf_launch_action_and_embedded_file_are_refused(tmp_path):
    def launch(pdf):
        pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.Launch, F=pikepdf.String("cmd.exe"))

    def attach(pdf):
        pdf.attachments["payload.exe"] = pikepdf.AttachedFileSpec(pdf, b"MZ\x90\x00 not really")
    _refused(_pdf(tmp_path / "launch.pdf", launch), "launches programs")
    _refused(_pdf(tmp_path / "attach.pdf", attach), "embedded files")


def test_password_protected_and_broken_pdfs_are_refused(tmp_path):
    _refused(_pdf(tmp_path / "locked.pdf", encryption=pikepdf.Encryption(user="pw", owner="pw")), "password")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 50)
    _refused(str(broken), "damaged")


# ── Word ─────────────────────────────────────────────────────────────────────


def test_plain_docx_with_hyperlink_is_accepted(tmp_path):
    rel = ('<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
           'hyperlink" Target="https://github.com/x" TargetMode="External"/>')
    assert check_resume_file(_docx(tmp_path / "cv.docx", rels=rel)) == "docx"


def test_docx_macros_activex_and_embedded_programs_are_refused(tmp_path):
    _refused(_docx(tmp_path / "m.docx", extra={"word/vbaProject.bin": b"\x00" * 64}), "macros")
    _refused(_docx(tmp_path / "a.docx", extra={"word/activeX/activeX1.xml": b"<x/>"}), "ActiveX")
    _refused(_docx(tmp_path / "e.docx", extra={"word/embeddings/oleObject1.bin": b"\x00" * 64}), "embedded objects")


def test_docx_remote_template_is_refused(tmp_path):
    rel = ('<Relationship Id="rId98" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
           'attachedTemplate" Target="http://evil.example/t.dotm" TargetMode="External"/>')
    _refused(_docx(tmp_path / "t.docx", rels=rel), "loads content from the internet")


def test_zip_bombs_traversal_and_fake_word_files_are_refused(tmp_path):
    _refused(_docx(tmp_path / "bomb.docx", extra={"word/media/huge.bin.txt": b"\x00" * (6 * 1024 * 1024)}),
             "unusually large")
    _refused(_docx(tmp_path / "trav.docx", extra={"../../evil.txt": b"x"}), "invalid internal structure")
    with zipfile.ZipFile(tmp_path / "notword.docx", "w") as z:
        z.writestr("hello.txt", "hi")
    _refused(str(tmp_path / "notword.docx"), "valid Word document")


def test_legacy_doc_rules(tmp_path):
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
    clean, macro, package = tmp_path / "c.doc", tmp_path / "m.doc", tmp_path / "p.doc"
    clean.write_bytes(ole + "Experience: Excel macros, SQL".encode("utf-16-le"))  # the skill, not a macro
    macro.write_bytes(ole + "_VBA_PROJECT".encode("utf-16-le"))
    package.write_bytes(ole + "\x01Ole10Native".encode("utf-16-le"))
    assert check_resume_file(str(clean)) == "doc"
    _refused(str(macro), "macros")
    _refused(str(package), "embedded program packages")
    _refused(str(clean), "PDF or DOCX", allow_doc=False)


def test_other_file_types_are_refused(tmp_path):
    exe = tmp_path / "cv.pdf"
    exe.write_bytes(b"MZ\x90\x00" + b"\x00" * 100)
    _refused(str(exe), "Only PDF, DOC or DOCX")


# ── Antivirus ────────────────────────────────────────────────────────────────


@pytest.fixture
def clamd(monkeypatch):
    """A fake clamd: answers INSTREAM with whatever `state["answer"]` says."""
    state = {"answer": b"stream: OK\0", "received": 0}
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while not data.endswith(b"\0\0\0\0"):
                    part = conn.recv(65536)
                    if not part:
                        break
                    data += part
                state["received"] += 1
                conn.sendall(state["answer"])

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(file_safety.settings, "CLAMAV_HOST", "127.0.0.1")
    monkeypatch.setattr(file_safety.settings, "CLAMAV_PORT", server.getsockname()[1])
    yield state
    server.close()


def test_clean_scan_passes_and_a_virus_is_refused(tmp_path, clamd):
    path = _pdf(tmp_path / "cv.pdf")
    assert check_resume_file(path) == "pdf" and clamd["received"] == 1
    clamd["answer"] = b"stream: Eicar-Test-Signature FOUND\0"
    _refused(path, "virus scanner flagged")


def test_unreachable_scanner_refuses_the_file(tmp_path, monkeypatch):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    monkeypatch.setattr(file_safety.settings, "CLAMAV_HOST", "127.0.0.1")
    monkeypatch.setattr(file_safety.settings, "CLAMAV_PORT", port)
    with pytest.raises(UnsafeFile, match="couldn't check this file for viruses") as exc:
        check_resume_file(_pdf(tmp_path / "cv.pdf"))
    assert exc.value.status_code == 503


# ── Storing ──────────────────────────────────────────────────────────────────


class _Upload:
    def __init__(self, data):
        self.file = io.BytesIO(data)


def test_store_moves_a_safe_file_into_place(tmp_path):
    data = open(_pdf(tmp_path / "src.pdf"), "rb").read()
    final = tmp_path / "stored.pdf"
    assert store_resume_upload(_Upload(data), str(final)) == hashlib.sha256(data).hexdigest()
    assert final.read_bytes() == data
    assert not list(tmp_path.glob("*.incoming"))


def test_store_leaves_nothing_behind_for_unsafe_empty_or_oversized_files(tmp_path, monkeypatch):
    def js(pdf):
        pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("x"))
    bad = open(_pdf(tmp_path / "bad.pdf", js), "rb").read()
    with pytest.raises(UnsafeFile):
        store_resume_upload(_Upload(bad), str(tmp_path / "a.pdf"))
    with pytest.raises(UnsafeFile, match="empty"):
        store_resume_upload(_Upload(b""), str(tmp_path / "b.pdf"))
    monkeypatch.setattr(file_safety.settings, "MAX_UPLOAD_SIZE_MB", 1)
    with pytest.raises(UnsafeFile, match="larger than 1 MB") as exc:
        store_resume_upload(_Upload(b"%PDF" + b"0" * (2 * 1024 * 1024)), str(tmp_path / "c.pdf"))
    assert exc.value.status_code == 413
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.pdf"]
