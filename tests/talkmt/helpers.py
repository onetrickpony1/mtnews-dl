import io
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from mtnews_dl.sources.talkmt.net import Session

LOGIN_HTML = b'<html><body class="page"><form id="mepr_loginform" class="mepr-form"></form></body></html>'
BLOG_HTML = b'<html><body class="blog wp-theme-Divi"><h1>Latest</h1></body></html>'


def pdf_bytes(text="It-Tlieta, 3 ta' Novembru 2026", eof=True):
    stream = f"BT /F1 16 Tf 50 780 Td ({text}) Tj ET".encode("latin-1")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{n} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n".encode())
    if eof:
        data.extend(b"%%EOF\n")
    return bytes(data)


class FakeStream(io.BytesIO):
    def __init__(self, data, on_read=None):
        super().__init__(data)
        self.on_read = on_read

    def read(self, n=-1):
        if self.on_read:
            self.on_read()
        return super().read(n)


def listing_html(*slugs, extra=""):
    links = "".join(f'<a href="https://subscribe.mt/newspapers/mp-files/{s}/">{t}</a>' for s, t in slugs)
    return f'<html><body class="page logged-in"><a href="/newspapers/logout/">Logout</a>{links}{extra}</body></html>'.encode()


class Router:
    """Maps URL -> response(s). A list is consumed in order; the last item repeats."""

    def __init__(self, routes=None, fallback=None):
        self.routes = dict(routes or {})
        self.fallback = fallback  # response for any other URL (e.g. the generic HTML page)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.routes and self.fallback is not None:
            return self.fallback[0], self.fallback[1], FakeStream(self.fallback[2])
        if url not in self.routes:
            raise AssertionError(f"unexpected request: {url}")
        spec = self.routes[url]
        if isinstance(spec, list):
            spec = spec.pop(0) if len(spec) > 1 else spec[0]
        if isinstance(spec, Exception):
            raise spec
        status, headers, body = spec
        stream = body if isinstance(body, io.IOBase) else FakeStream(body)
        return status, headers, stream


def session_with(router):
    session = Session()
    session._open = router
    return session


class TempConfig:
    """Isolated TALKMT_DL_CONFIG for a test."""

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config"
        self.patch = patch.dict(os.environ, {"TALKMT_DL_CONFIG": str(self.path)})
        self.patch.start()
        return self.path

    def __exit__(self, *exc):
        self.patch.stop()
        self.temp.cleanup()


def no_wait_event():
    """A stop event whose wait() returns immediately (no real sleeping in tests)."""
    event = threading.Event()
    event.wait = lambda timeout=None: event.is_set()
    return event
