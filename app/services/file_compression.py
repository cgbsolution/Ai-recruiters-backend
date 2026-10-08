"""
Shrink stored documents (resumes, job descriptions) without changing what
they say.

- PDF: oversized embedded images (photos, scanned logos) are scaled down to
  at most MAX_IMAGE_SIDE px and re-encoded as JPEG; then the file is rewritten
  with compressed object streams and re-compressed content (pikepdf/qpdf).
  Text, fonts and vector content are untouched, so text extraction is too.
- DOCX: pictures in word/media are scaled down the same way (keeping their
  format) and the package is re-zipped at maximum compression.
- Anything else (.doc, images, recordings) is left as it is.

A file is only replaced when the result is at least MIN_SAVING smaller AND
passes a check (same page count / same text), via an atomic rename — a failed
or unhelpful attempt leaves the original exactly as it was. compress_file
never raises.

Existing files: python -m app.services.file_compression [folder ...]
(defaults to the resume and job-description upload folders).
"""

import contextlib
import io
import logging
import os
import signal
import sys
import threading
import zipfile
from typing import Dict

logger = logging.getLogger(__name__)

MIN_SAVING = 0.05          # keep the original unless at least 5% smaller
MAX_IMAGE_SIDE = 1600      # px — a resume photo or logo never needs more
JPEG_QUALITY = 75
MIN_IMAGE_BYTES = 60_000   # small images are not worth re-encoding
_TMP_SUFFIX = ".compressing"
TIME_LIMIT_SECONDS = 120   # a file that takes longer is left as it is

logging.getLogger("fontTools").setLevel(logging.ERROR)


@contextlib.contextmanager
def _time_limit(seconds: int):
    """Abort the block after `seconds` (POSIX main thread only — that is where
    the worker processes run it; elsewhere there is no limit)."""
    usable = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    if not usable:
        yield
        return

    def _expired(signum, frame):
        raise TimeoutError(f"compression took over {seconds}s")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


# ── Images ───────────────────────────────────────────────────────────────────

def _shrink_raster(data: bytes, fmt: str) -> bytes:
    """Scale a JPEG/PNG down to MAX_IMAGE_SIDE and re-encode it in the same
    format. Returns the original bytes when that doesn't help."""
    if len(data) < MIN_IMAGE_BYTES:
        return data
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        img.load()
        w, h = img.size
        scale = min(1.0, MAX_IMAGE_SIDE / max(w, h))
        if scale < 1:
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        out = io.BytesIO()
        if fmt == "jpeg":
            img.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
        else:
            img.save(out, "PNG", optimize=True)
    new = out.getvalue()
    return new if len(new) < len(data) * 0.9 else data


# ── PDF ──────────────────────────────────────────────────────────────────────

def _shrink_pdf_image(raw) -> None:
    from PIL import Image
    from pikepdf import Name, PdfImage

    # Masks, transparency and decode arrays change how pixels are drawn;
    # re-encoding those safely is not worth it for a resume.
    for key in ("/ImageMask", "/SMask", "/Mask", "/Decode"):
        if key in raw:
            return
    if int(raw.get("/BitsPerComponent", 8)) != 8:
        return  # 1-bit scans (CCITT/JBIG2) are already tiny
    original = len(raw.read_raw_bytes())
    if original < MIN_IMAGE_BYTES:
        return
    pil = PdfImage(raw).as_pil_image()
    if pil.mode == "CMYK":
        return  # converting to RGB visibly shifts colours
    if pil.mode not in ("RGB", "L"):
        pil = pil.convert("RGB")
    w, h = pil.size
    scale = min(1.0, MAX_IMAGE_SIDE / max(w, h))
    if scale < 1:
        pil = pil.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    out = io.BytesIO()
    pil.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    data = out.getvalue()
    if len(data) >= original * 0.9:
        return
    raw.write(data, filter=Name.DCTDecode)
    raw.ColorSpace = Name.DeviceGray if pil.mode == "L" else Name.DeviceRGB
    raw.Width, raw.Height = pil.size
    raw.BitsPerComponent = 8
    if "/DecodeParms" in raw:
        del raw["/DecodeParms"]


def _font_file(font):
    """(FontFile2 stream, kind) of an embedded TrueType font we know how to
    subset: a Type0/Identity-H CID font (2-byte glyph ids) or a simple
    TrueType font with a standard encoding (1-byte codes). Else (None, None)."""
    try:
        if font.get("/Subtype") == "/Type0":
            if font.get("/Encoding") != "/Identity-H":
                return None, None
            desc = font.DescendantFonts[0]
            if desc.get("/Subtype") != "/CIDFontType2" or desc.get("/CIDToGIDMap", "/Identity") != "/Identity":
                return None, None
            return desc.FontDescriptor.get("/FontFile2"), "cid"
        if font.get("/Subtype") == "/TrueType":
            enc = font.get("/Encoding")
            if enc not in ("/WinAnsiEncoding", "/MacRomanEncoding"):
                return None, None  # symbolic fonts / custom encodings: left as they are
            return font.FontDescriptor.get("/FontFile2"), enc
    except Exception:
        pass
    return None, None


def _collect_font_usage(pdf) -> dict:
    """Glyphs actually drawn, per embedded font file: {objgen: [stream, gids, unicodes]}.
    Walks page content, form XObjects and annotation appearances."""
    import pikepdf

    usage: dict = {}

    def record(font, data: bytes) -> None:
        stream, kind = _font_file(font)
        if stream is None:
            return
        entry = usage.setdefault(stream.objgen, [stream, set(), set()])
        if kind == "cid":
            entry[1].update(int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data) - 1, 2))
        else:
            codec = "cp1252" if kind == "/WinAnsiEncoding" else "mac_roman"
            entry[2].update(ord(c) for c in data.decode(codec, errors="ignore"))
            entry[2].update(data)  # also keep the raw codes' glyphs

    def scan(owner, resources, depth: int = 0) -> None:
        if depth > 8 or resources is None:
            return
        fonts = resources.get("/Font") or {}
        xobjects = resources.get("/XObject") or {}
        current = None
        for operands, op in pikepdf.parse_content_stream(owner):
            op = str(op)
            if op == "Tf" and operands:
                current = fonts.get(operands[0])
            elif op in ("Tj", "'", '"', "TJ") and current is not None and operands:
                items = operands[-1] if op != "TJ" else list(operands[0])
                for item in items if isinstance(items, list) else [items]:
                    if isinstance(item, pikepdf.String):
                        record(current, bytes(item))
            elif op == "Do" and operands:
                xobj = xobjects.get(operands[0])
                if xobj is not None and xobj.get("/Subtype") == "/Form":
                    scan(xobj, xobj.get("/Resources", resources), depth + 1)

    for page in pdf.pages:
        scan(page, page.obj.get("/Resources"))
        for annot in page.obj.get("/Annots") or []:
            for appearance in (annot.get("/AP") or {}).values():
                streams = appearance.values() if isinstance(appearance, pikepdf.Dictionary) else [appearance]
                for stream in streams:
                    if isinstance(stream, pikepdf.Stream):
                        scan(stream, stream.get("/Resources"))
    return usage


def _subset_fonts(pdf) -> bool:
    """Cut embedded TrueType fonts down to the glyphs the document draws.
    Word/Naukri exports often embed whole fonts (~150 KB each, four or five per
    resume) for a few hundred characters. Glyph ids are kept (retain_gids), so
    the text still maps to the same glyphs. Returns True if any font changed."""
    from fontTools import subset
    from fontTools.ttLib import TTFont

    changed = False
    for stream, gids, unicodes in _collect_font_usage(pdf).values():
        try:
            original = stream.read_bytes()
            font = TTFont(io.BytesIO(original))
            options = subset.Options()
            options.retain_gids = True       # text in the PDF refers to glyph ids
            options.notdef_outline = True
            options.hinting = True           # identical rasterisation
            options.layout_features = []    # a PDF never shapes text
            options.name_IDs = ["*"]
            options.ignore_missing_glyphs = True
            options.ignore_missing_unicodes = True
            subsetter = subset.Subsetter(options)
            subsetter.populate(gids=sorted(gids | {0}), unicodes=sorted(unicodes))
            subsetter.subset(font)
            out = io.BytesIO()
            font.save(out)
            data = out.getvalue()
        except Exception:
            continue  # this font stays whole
        if len(data) < len(original) * 0.8:
            stream.write(data)
            stream.Length1 = len(data)
            changed = True
    return changed


def _renders_identically(a_bytes: bytes, b_bytes: bytes, max_pages: int = 12) -> bool:
    """Render both PDFs and require every page to be pixel-identical."""
    import pypdfium2 as pdfium
    from PIL import ImageChops

    a, b = pdfium.PdfDocument(a_bytes), pdfium.PdfDocument(b_bytes)
    try:
        if len(a) != len(b) or len(a) > max_pages:
            return False
        for i in range(len(a)):
            img_a = a[i].render(scale=1.5, grayscale=True).to_pil()
            img_b = b[i].render(scale=1.5, grayscale=True).to_pil()
            if img_a.size != img_b.size or ImageChops.difference(img_a, img_b).getbbox() is not None:
                return False
        return True
    finally:
        a.close()
        b.close()


def _compress_pdf(src: str, dst: str) -> bool:
    import pikepdf

    with open(src, "rb") as fh:
        original = fh.read()
    pdf = pikepdf.open(io.BytesIO(original))
    try:
        if pdf.is_encrypted:
            return False  # rewriting would drop the author's protection
        pages = len(pdf.pages)

        # 1. Fonts — kept only if every page still renders pixel-for-pixel the same.
        if "/AcroForm" not in pdf.Root and _subset_fonts(pdf):
            trial = io.BytesIO()
            pdf.save(trial)
            if not _renders_identically(original, trial.getvalue()):
                pdf.close()
                pdf = pikepdf.open(io.BytesIO(original))  # back to the untouched fonts

        # 2. Oversized images.
        seen = set()
        for page in pdf.pages:
            for _, raw in page.images.items():
                if raw.objgen in seen:
                    continue
                seen.add(raw.objgen)
                try:
                    _shrink_pdf_image(raw)
                except Exception:
                    continue  # leave any image we can't handle exactly as it is

        pdf.remove_unreferenced_resources()
        pdf.save(
            dst,
            compress_streams=True,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
    finally:
        pdf.close()
    with pikepdf.open(dst) as check:
        return len(check.pages) == pages


# ── DOCX ─────────────────────────────────────────────────────────────────────

def _docx_text(path: str) -> str:
    from docx import Document

    return "\n".join(p.text for p in Document(path).paragraphs)


def _compress_docx(src: str, dst: str) -> bool:
    from app.services.resume_service import check_docx_is_safe

    check_docx_is_safe(src)  # bounded size before anything is inflated
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            name = info.filename.lower()
            if name.startswith("word/media/"):
                if name.endswith((".jpg", ".jpeg")):
                    data = _shrink_raster(data, "jpeg")
                elif name.endswith(".png"):
                    data = _shrink_raster(data, "png")
            entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            entry.external_attr = info.external_attr
            entry.compress_type = zipfile.ZIP_DEFLATED
            zout.writestr(entry, data, compresslevel=9)
    return _docx_text(dst) == _docx_text(src)


# ── Entry points ─────────────────────────────────────────────────────────────

def compress_file(path: str) -> Dict[str, int]:
    """Compress `path` in place when that saves space. Returns the sizes
    before and after (equal when the file was left alone). Never raises."""
    ext = os.path.splitext(path)[1].lower()
    try:
        before = os.path.getsize(path)
    except OSError:
        return {"before": 0, "after": 0}
    result = {"before": before, "after": before}
    if ext not in (".pdf", ".docx"):
        return result
    tmp = path + _TMP_SUFFIX
    try:
        with _time_limit(TIME_LIMIT_SECONDS):
            ok = _compress_pdf(path, tmp) if ext == ".pdf" else _compress_docx(path, tmp)
        if ok:
            after = os.path.getsize(tmp)
            if after <= before * (1 - MIN_SAVING):
                os.replace(tmp, path)  # atomic: readers see the old or the new file, never half
                result["after"] = after
    except Exception as exc:
        logger.info("Left %s uncompressed: %s", os.path.basename(path), exc)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return result


def schedule_compression(path: str) -> None:
    """Compress a just-saved upload in a background worker process (fire and
    forget), keeping the CPU work off the API's event loop."""
    from app.services.extraction_pool import submit_background

    submit_background(compress_file, path)


def _main(folders) -> None:
    from app.media import UPLOAD_ROOT

    folders = folders or [os.path.join(UPLOAD_ROOT, "resumes"), os.path.join(UPLOAD_ROOT, "jd")]
    before = after = changed = files = 0
    for folder in folders:
        for entry in sorted(os.scandir(folder), key=lambda e: e.name) if os.path.isdir(folder) else []:
            if not entry.is_file() or entry.name.endswith(_TMP_SUFFIX):
                if entry.name.endswith(_TMP_SUFFIX):
                    os.remove(entry.path)  # left by an interrupted run
                continue
            files += 1
            r = compress_file(entry.path)
            before += r["before"]
            after += r["after"]
            changed += r["after"] < r["before"]
            if files % 200 == 0:
                print(f"  {files} files checked, {(before - after) / 1048576:.1f} MB saved so far", flush=True)
    mb = 1048576
    saved = before - after
    pct = (saved / before * 100) if before else 0
    print(f"{files} files checked, {changed} compressed: {before / mb:.1f} MB -> {after / mb:.1f} MB "
          f"({saved / mb:.1f} MB, {pct:.0f}% saved)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    _main(sys.argv[1:])
