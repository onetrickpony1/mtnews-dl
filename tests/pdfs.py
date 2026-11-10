"""Small real PDFs for tests (pypdfium2 builds them; no fixtures on disk)."""

from pathlib import Path

from tests.talkmt.helpers import pdf_bytes  # a 1-page PDF with a line of text


def blank_pdf(path, pages=1, width=100, height=100):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(width, height)
    with Path(path).open("wb") as f:
        doc.save(f)
    doc.close()
    return Path(path)


def text_pdf(path, text):
    Path(path).write_bytes(pdf_bytes(text))
    return Path(path)


def page_count(path):
    from mtnews_dl.common.pdfcheck import page_count as count
    return count(path)
