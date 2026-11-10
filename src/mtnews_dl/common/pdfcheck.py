"""PDF structure checks, printed-date detection and merging, shared by every source.

Text and rendering use pypdfium2 (bundled PDFium wheels for Windows, macOS and Linux), so
no Poppler install is needed. Tesseract is optional and only used when the text layer has
no single printed date (see ocr.py).

Date logic is the one from talkmt-dl 0.1.0, itself adapted from mttd-dl 0.8.1: dates in the
top 28% of page 1 (the masthead) outrank dates in stories; English and Maltese month
names are recognised; OCR runs region by region when the text gives no date or several.
"""

import calendar
import datetime as dt
import hashlib
import os
import re
import unicodedata
from pathlib import Path

from . import ocr

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({"jannar": 1, "frar": 2, "marzu": 3, "april": 4, "mejju": 5,
               "gunju": 6, "lulju": 7, "awwissu": 8, "settembru": 9,
               "ottubru": 10, "novembru": 11, "dicembru": 12})
# 4: pypdfium2 text extraction instead of Poppler (older rows are not re-inspected for this).
DATE_CHECK_VERSION = 4
_NAMES = "|".join(MONTHS)
DATE_RE = re.compile(r"\b(" + _NAMES + r")\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\b", re.I)
DMY_RE = re.compile(
    r"(?<!No\.\s)(?<!No\s)(?<!Nru\.\s)\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:(?:ta\s*['’]?|t\s*['’])\s*)?("
    + _NAMES + r")\s*,?\s*(\d{4})\b", re.I)

MASTHEAD = 0.28  # top fraction of page 1 treated as the masthead

# OCR regions of page 1 as (x0, y0, x1, y1) fractions from the top-left, dpi, Tesseract psm.
# Corner crops catch small masthead dates that whole-strip OCR misses (it-Torca).
OCR_REGIONS = (
    ("top-right corner", (0.55, 0.0, 1.0, 0.12), 300, "6"),
    ("top-left corner", (0.0, 0.0, 0.45, 0.12), 300, "6"),
    ("masthead strip", (0.0, 0.0, 1.0, MASTHEAD), 200, "3"),
    ("whole page", (0.0, 0.0, 1.0, 1.0), 150, "3"),
)


def _pdfium():
    import pypdfium2
    return pypdfium2


def sha(path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fold(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def dates_in(text: str) -> list:
    text = " ".join(fold(text).split())
    found = set()
    for pattern, order in ((DATE_RE, (2, 0, 1)), (DMY_RE, (2, 1, 0))):
        for match in pattern.finditer(text):
            values = match.groups()
            try:
                found.add(dt.date(int(values[order[0]]), MONTHS[values[order[1]].lower()],
                                  int(values[order[2]])).isoformat())
            except ValueError:
                pass
    return sorted(found)


def classify(expected, found) -> str:
    if found == [expected]:
        return "verified"
    if found and expected not in found:
        return "wrong_date"
    return "uncertain"


def _open(path):
    pdfium = _pdfium()
    try:
        return pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        raise ValueError(f"PDF cannot be read: {exc}") from None


def check_structure(path) -> int:
    """Signature, trailer and readable page count; raises ValueError otherwise."""
    path = Path(path)
    with path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            raise ValueError("Not a PDF (possibly a sign-in or error page).")
        f.seek(max(0, path.stat().st_size - 4096))
        if b"%%EOF" not in f.read():
            raise ValueError("PDF has no end-of-file marker; the download is probably truncated.")
    doc = _open(path)
    try:
        pages = len(doc)
    finally:
        doc.close()
    if pages < 1:
        raise ValueError("PDF has no readable pages.")
    return pages


def page_count(path) -> int:
    doc = _open(path)
    try:
        return len(doc)
    finally:
        doc.close()


def front_text(path):
    """(whole page-1 text, masthead-region text) from the PDF's text layer."""
    doc = _open(path)
    try:
        page = doc[0]
        width, height = page.get_size()
        textpage = page.get_textpage()
        try:
            whole = textpage.get_text_range()
            top = textpage.get_text_bounded(left=0, bottom=height * (1 - MASTHEAD),
                                            right=width, top=height)
        finally:
            textpage.close()
            page.close()
        return whole, top
    finally:
        doc.close()


def document_text(path, max_pages=200) -> str:
    doc = _open(path)
    try:
        parts = []
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            textpage = page.get_textpage()
            parts.append(textpage.get_text_range())
            textpage.close()
            page.close()
        return "\n".join(parts)
    finally:
        doc.close()


def render_region(path, box, dpi, out_png):
    """Render part of page 1 (box = fractions from the top-left) to a PNG file."""
    doc = _open(path)
    try:
        page = doc[0]
        width, height = page.get_size()
        x0, y0, x1, y1 = box
        # crop = points removed from (left, bottom, right, top)
        crop = (width * x0, height * (1 - y1), width * (1 - x1), height * y0)
        bitmap = page.render(scale=dpi / 72, crop=crop)
        bitmap.to_pil().save(out_png)
        page.close()
    finally:
        doc.close()


def inspect_pdf(path, expected) -> dict:
    """Structure check, then printed-date check against `expected` (YYYY-MM-DD or None)."""
    path = Path(path)
    pages = check_structure(path)
    if expected is None:
        return dict(date_check_version=DATE_CHECK_VERSION, status="uncertain", detected_dates=[],
                    method="PDF structure checked; no expected date to compare",
                    pages=pages, sha256=sha(path), front_text="")
    try:
        text, top = front_text(path)
    except ValueError:
        text, top = "", ""
    found = dates_in(text)
    method = "embedded text (whole front page)"
    masthead = dates_in(top)
    if masthead:  # Masthead dates outrank dates mentioned in stories.
        found, method = masthead, "embedded text (top 28% masthead region)"
    if len(found) != 1:
        # Several dates usually mean hidden template layers (seen in it-Torca) or adverts;
        # OCR sees only what is visibly printed. Small regions are tried first.
        engine = ocr.engine()
        if engine is None:
            method += "; OCR unavailable (Tesseract not installed: see `mtnews-dl doctor`)"
        else:
            for label, box, dpi, psm in OCR_REGIONS:
                try:
                    result = engine.read_region(path, box, dpi, psm)
                except (OSError, ValueError, ocr.OcrError):
                    method += "; OCR failed"
                    break
                text = f"OCR ({label}):\n{result}\n\n" + text  # keep OCR ahead of truncation
                ocr_found = dates_in(result)
                if len(ocr_found) == 1 or (ocr_found and not found and label == "whole page"):
                    previous = f"; embedded text had {', '.join(found)}" if found else ""
                    found, method = ocr_found, f"OCR {label} ({engine.langs}){previous}"
                    break
            else:
                if not found:
                    method = f"text + OCR ({engine.langs}); no date found"
    return dict(date_check_version=DATE_CHECK_VERSION, status=classify(expected, found),
                detected_dates=found, method=method, pages=pages, sha256=sha(path),
                front_text=text[:20000])


def inspect_safe(path, expected) -> dict:
    """inspect_pdf that never raises: damaged files become `uncertain`."""
    try:
        return inspect_pdf(path, expected)
    except (ValueError, OSError) as exc:
        return dict(date_check_version=DATE_CHECK_VERSION, status="uncertain", detected_dates=[],
                    method="PDF inspection failed", pages=0, sha256=sha(path), front_text=str(exc))


def merge(sources, dest) -> int:
    """Join PDFs in order into dest (atomically). Returns the page count written."""
    pdfium = _pdfium()
    dest = Path(dest)
    out = pdfium.PdfDocument.new()
    try:
        expected = 0
        for source in sources:
            doc = _open(source)
            try:
                out.import_pages(doc)
                expected += len(doc)
            finally:
                doc.close()
        temp = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("wb") as f:
            out.save(f)
            f.flush()
            os.fsync(f.fileno())
    finally:
        out.close()
    if page_count(temp) != expected:
        temp.unlink(missing_ok=True)
        raise ValueError("Merged PDF page count mismatch.")
    os.replace(temp, dest)
    return expected
