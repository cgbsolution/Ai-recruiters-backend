"""
Safety checks for uploaded resumes — every route that stores a resume uses
store_resume_upload(), so a file reaches the uploads folder only after:

1. Size: within MAX_UPLOAD_SIZE_MB, not empty.
2. Real type, from the bytes (never the name or the browser's word):
   PDF, DOCX, or — where allowed — legacy DOC.
3. Structure (the usual ways a document carries an attack):
   - PDF (opened with qpdf, so compressed object streams are seen too):
     JavaScript, launch / submit / import / remote-go-to actions, embedded
     or attached files, rich media, 3D, XFA forms; password-protected files
     (they can't be inspected).
   - DOCX: macros, ActiveX, embedded OLE objects, remote templates and other
     external parts, encrypted or unsafe zip entries, zip bombs.
   - DOC: VBA macros, OLE package objects, Equation Editor objects,
     encrypted documents.
4. Antivirus: ClamAV (clamd INSTREAM) when CLAMAV_HOST is set. Fail-closed:
   if the scanner can't be reached, the file is refused, not stored unscanned.

The file is written under a temporary ".incoming" name, which media serving
never serves, and moved into place only after every check passed. A refused
file is deleted at once. Logs carry the reason only — never the file name,
which often contains the candidate's name.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import struct
import zipfile
from typing import Optional

from app.config import settings

logger = logging.getLogger("app.file_safety")


class UnsafeFile(ValueError):
    """A resume we won't store. str(exc) is safe to show to the uploader."""

    def __init__(self, reason: str, status_code: int = 400):
        super().__init__(reason)
        self.reason, self.status_code = reason, status_code


_RESAVE = "Please save it as a plain PDF or Word file (for example, Print → Save as PDF) and upload it again."
SCAN_UNAVAILABLE = "We couldn't check this file for viruses right now. Please try again in a few minutes."

# ── PDF ──────────────────────────────────────────────────────────────────────

_MAX_PDF_OBJECTS = 200_000
_PDF_BAD_KEYS = {"/JS": "JavaScript", "/JavaScript": "JavaScript", "/EmbeddedFiles": "embedded files",
                 "/XFA": "an XFA form", "/RichMediaContent": "embedded media"}
_PDF_BAD_ACTIONS = {"/JavaScript": "JavaScript", "/Launch": "an action that launches programs",
                    "/SubmitForm": "a form that sends data", "/ImportData": "an import action",
                    "/GoToE": "a link into embedded files", "/GoToR": "a link to an outside file",
                    "/RichMediaExecute": "embedded media", "/Rendition": "embedded media"}
_PDF_BAD_TYPES = {"/EmbeddedFile": "embedded files"}
_PDF_BAD_SUBTYPES = {"/FileAttachment": "file attachments", "/RichMedia": "embedded media", "/3D": "3D content",
                     "/Screen": "embedded media"}


def _pdf_problem(root) -> Optional[str]:
    """What's dangerous inside one PDF object (walking its direct children;
    indirect ones are visited on their own)."""
    import pikepdf

    stack, steps = [root], 0
    while stack:
        steps += 1
        if steps > 50_000:
            return "an unusually complex structure"
        o = stack.pop()
        if isinstance(o, pikepdf.Array):
            stack.extend(x for x in o if not getattr(x, "is_indirect", True))
            continue
        d = o.stream_dict if isinstance(o, pikepdf.Stream) else o
        if not isinstance(d, pikepdf.Dictionary):
            continue
        for key in d.keys():
            if key in _PDF_BAD_KEYS:
                return _PDF_BAD_KEYS[key]
            value = d.get(key)
            if key == "/S" and str(value) in _PDF_BAD_ACTIONS:
                return _PDF_BAD_ACTIONS[str(value)]
            if key == "/Type" and str(value) in _PDF_BAD_TYPES:
                return _PDF_BAD_TYPES[str(value)]
            if key == "/Subtype" and str(value) in _PDF_BAD_SUBTYPES:
                return _PDF_BAD_SUBTYPES[str(value)]
            if isinstance(value, (pikepdf.Dictionary, pikepdf.Array)) and not value.is_indirect:
                stack.append(value)
    return None


def _check_pdf(path: str) -> None:
    import pikepdf

    try:
        pdf = pikepdf.open(path)
    except pikepdf.PasswordError:
        raise UnsafeFile("This PDF is password-protected, so we can't check it. "
                         "Please upload a copy without a password.")
    except Exception:
        raise UnsafeFile("This PDF is damaged and can't be read. " + _RESAVE)
    with pdf:
        for count, obj in enumerate(pdf.objects, 1):
            if count > _MAX_PDF_OBJECTS:
                raise UnsafeFile("This PDF is too complex to check. " + _RESAVE)
            problem = _pdf_problem(obj)
            if problem:
                raise UnsafeFile(f"This PDF contains {problem}, which we don't accept for safety. " + _RESAVE)


# ── DOCX ─────────────────────────────────────────────────────────────────────

_MAX_ZIP_ENTRIES = 2_000
_MAX_ZIP_TOTAL = 100 * 1024 * 1024
_MAX_ZIP_RATIO = 100
_MAX_PART_READ = 2 * 1024 * 1024
_RISKY_EXTERNAL_RELS = ("/attachedTemplate", "/oleObject", "/frame", "/subDocument", "/externalLinkPath",
                        "/package", "/control", "/image", "/altChunk")
_MACRO_EXTS = (".bin", ".docm", ".dotm", ".xlsm", ".xltm", ".pptm", ".potm", ".ppsm", ".exe", ".dll", ".js",
               ".vbs", ".ps1", ".bat", ".cmd", ".scr", ".hta", ".lnk")


def _read_part(z: zipfile.ZipFile, name: str) -> str:
    with z.open(name) as fh:
        return fh.read(_MAX_PART_READ).decode("utf-8", "replace")


def _check_docx(path: str) -> None:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise UnsafeFile("This Word file is damaged and can't be read. " + _RESAVE)
    with z:
        infos = z.infolist()
        if len(infos) > _MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > _MAX_ZIP_TOTAL:
            raise UnsafeFile("This Word file is unusually large inside, which we don't accept for safety. " + _RESAVE)
        names = []
        for i in infos:
            name = i.filename
            if i.flag_bits & 0x1:
                raise UnsafeFile("This Word file is encrypted, so we can't check it. "
                                 "Please upload a copy without a password.")
            if name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/"):
                raise UnsafeFile("This Word file has an invalid internal structure. " + _RESAVE)
            if i.file_size > 5 * 1024 * 1024 and i.file_size > _MAX_ZIP_RATIO * max(i.compress_size, 1):
                raise UnsafeFile("This Word file is unusually large inside, which we don't accept for safety. "
                                 + _RESAVE)
            names.append(name)
        lower = [n.lower() for n in names]
        if "[content_types].xml" not in lower:
            raise UnsafeFile("This isn't a valid Word document. " + _RESAVE)
        content_types = _read_part(z, names[lower.index("[content_types].xml")])
        if "wordprocessingml.document.main" not in content_types and "wordprocessingml.template.main" not in content_types:
            raise UnsafeFile("This isn't a valid Word document. " + _RESAVE)
        if "macroEnabled" in content_types or "vbaProject" in content_types or any(
                n.endswith(("vbaproject.bin", "vbadata.xml")) for n in lower):
            raise UnsafeFile("This Word file contains macros, which we don't accept for safety. " + _RESAVE)
        if any(n.startswith("word/activex/") for n in lower):
            raise UnsafeFile("This Word file contains ActiveX controls, which we don't accept for safety. " + _RESAVE)
        if any("/embeddings/" in n and n.endswith(_MACRO_EXTS) for n in lower):
            raise UnsafeFile("This Word file contains embedded objects, which we don't accept for safety. " + _RESAVE)
        for name, low in zip(names, lower):
            if not low.endswith(".rels"):
                continue
            for tag in re.findall(r"<Relationship\b[^>]*>", _read_part(z, name)):
                if not re.search(r"TargetMode\s*=\s*[\"']External[\"']", tag):
                    continue
                kind = re.search(r"Type\s*=\s*[\"']([^\"']+)", tag)
                if kind and kind.group(1).endswith(_RISKY_EXTERNAL_RELS):
                    raise UnsafeFile("This Word file loads content from the internet when opened, which we don't "
                                     "accept for safety. " + _RESAVE)


# ── DOC (legacy) ─────────────────────────────────────────────────────────────

_DOC_MARKERS = {
    "_VBA_PROJECT": "macros",
    "Ole10Native": "embedded program packages",
    "Equation Native": "Equation Editor objects",
    "EncryptedPackage": "encryption",
}


def _check_doc(path: str) -> None:
    with open(path, "rb") as fh:
        data = fh.read()
    # OLE stream names are stored in UTF-16; these name the risky parts.
    for marker, what in _DOC_MARKERS.items():
        if marker.encode("utf-16-le") in data:
            raise UnsafeFile(f"This Word file contains {what}, which we don't accept for safety. " + _RESAVE)


# ── Antivirus ────────────────────────────────────────────────────────────────

def antivirus_configured() -> bool:
    return bool((settings.CLAMAV_HOST or "").strip())


def _clamav_scan(path: str) -> None:
    if not antivirus_configured():
        return
    reply = b""
    try:
        with socket.create_connection((settings.CLAMAV_HOST.strip(), int(settings.CLAMAV_PORT)),
                                      timeout=float(settings.CLAMAV_TIMEOUT_SECONDS)) as sock:
            sock.sendall(b"zINSTREAM\0")
            with open(path, "rb") as fh:
                while chunk := fh.read(64 * 1024):
                    sock.sendall(struct.pack("!L", len(chunk)) + chunk)
            sock.sendall(struct.pack("!L", 0))
            while not reply.endswith(b"\0"):
                part = sock.recv(4096)
                if not part:
                    break
                reply += part
    except OSError as exc:
        logger.warning("Virus scanner unavailable (%s): upload refused", type(exc).__name__)
        raise UnsafeFile(SCAN_UNAVAILABLE, status_code=503)
    answer = reply.rstrip(b"\0").decode("utf-8", "replace").strip()
    if answer.endswith(" OK"):
        return
    if answer.endswith(" FOUND"):
        logger.warning("Virus scanner flagged an upload: %s", answer.split(":", 1)[-1].strip()[:120])
        raise UnsafeFile("Our virus scanner flagged this file, so it has been deleted.")
    logger.warning("Virus scanner error: %s", answer[:120])
    raise UnsafeFile(SCAN_UNAVAILABLE, status_code=503)


# ── Entry points ─────────────────────────────────────────────────────────────

def check_resume_file(path: str, *, allow_doc: bool = True) -> str:
    """Raise UnsafeFile unless the stored file is a safe PDF / DOCX / DOC.
    Returns its kind: "pdf" | "docx" | "doc"."""
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(b"%PDF"):
        kind, check = "pdf", _check_pdf
    elif head.startswith(b"PK\x03\x04"):
        kind, check = "docx", _check_docx
    elif head.startswith(b"\xd0\xcf\x11\xe0"):
        if not allow_doc:
            raise UnsafeFile("Please upload a PDF or DOCX file (older .doc files aren't accepted here).")
        kind, check = "doc", _check_doc
    else:
        raise UnsafeFile("Only PDF, DOC or DOCX files are accepted.")
    check(path)
    _clamav_scan(path)
    return kind


def store_resume_upload(upload, final_path: str, *, allow_doc: bool = True) -> str:
    """Stream an uploaded resume (a Starlette UploadFile or any object with a
    binary .file) to `final_path` — only if it passes every check. Returns the
    SHA-256 of its bytes; raises UnsafeFile (nothing is left on disk)."""
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if os.path.exists(final_path):
        raise FileExistsError(final_path)
    tmp = f"{final_path}.incoming"
    digest, total = hashlib.sha256(), 0
    try:
        with open(tmp, "xb") as out:
            while chunk := upload.file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise UnsafeFile(f"The file is larger than {settings.MAX_UPLOAD_SIZE_MB} MB.", status_code=413)
                digest.update(chunk)
                out.write(chunk)
        if total == 0:
            raise UnsafeFile("The file is empty.")
        check_resume_file(tmp, allow_doc=allow_doc)
        os.replace(tmp, final_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return digest.hexdigest()
