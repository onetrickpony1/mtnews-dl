"""Sequential HTTPS access to subscribe.mt with explicit response classification.

Redirects are never followed automatically: every hop is checked so that a
login redirect is reported as an authentication problem and cookies are never
sent to another host. HTTP 200 alone is never treated as a PDF.
"""

import email.utils
import http.client
import os
import http.cookiejar
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from ... import __version__

HOST = "subscribe.mt"
BASE = "https://subscribe.mt/newspapers/"
DAILY_URL = BASE + "daily/"
USER_AGENT = f"talkmt-dl/{__version__} (personal subscriber archive)"
MAX_PDF_BYTES = 600 * 1024 * 1024
MAX_HTML_BYTES = 8 * 1024 * 1024
LOGIN_MARKERS = ("mepr_loginform", "mepr-unauthorized", "mepr_unauthorized", 'name="mepr_username"')


class TalkError(Exception):
    """Base class for classified failures."""


class AuthRequired(TalkError):
    pass


class SessionExpired(AuthRequired):
    pass


class NotEntitled(TalkError):
    """Logged in, but this file is refused (subscription does not cover it)."""


class IssueMissing(TalkError):
    pass


class Forbidden(TalkError):
    pass


class BadResponse(TalkError):
    pass


class Temporary(TalkError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimited(Temporary):
    pass


class ServerError(Temporary):
    pass


class NetworkError(Temporary):
    pass


def is_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "https" and parsed.hostname == HOST and port in (None, 443)
            and parsed.username is None and parsed.password is None)


def is_login_url(url: str) -> bool:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return (parsed.path.rstrip("/").endswith("/login")
            or "mepr_unauthorized" in query.get("action", [])
            or "wp-login.php" in parsed.path)


def looks_like_login(html: str) -> bool:
    return any(marker in html for marker in LOGIN_MARKERS)


def parse_retry_after(value):
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


@dataclass
class Result:
    url: str
    kind: str  # "pdf" or "html"
    text: str = ""
    size: int = 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError carrying the 3xx response


class Session:
    def __init__(self, jar=None, *, timeout=45):
        self.jar = jar if jar is not None else http.cookiejar.CookieJar()
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect())

    def _open(self, url):
        """Return (status, headers, stream). Tests replace this method."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"})
        try:
            response = self.opener.open(request, timeout=self.timeout)
            return response.status, response.headers, response
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc

    def get(self, url, *, dest=None, stop=None, expect_pdf=False, probe=False) -> Result:
        """probe=True: confirm a PDF is served (first bytes only), then close without downloading."""
        current = url
        for _hop in range(6):
            if not is_allowed(current):
                raise BadResponse("Refusing a URL outside https://subscribe.mt/.")
            try:
                status, headers, stream = self._open(current)
            except (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError,
                    ConnectionError) as exc:
                raise NetworkError(f"Network error: {getattr(exc, 'reason', exc)!s}") from None
            try:
                if status in (301, 302, 303, 307, 308):
                    location = headers.get("Location")
                    if not location:
                        raise BadResponse(f"HTTP {status} without a Location header.")
                    nxt = urljoin(current, location)
                    if is_login_url(nxt):
                        raise AuthRequired("The server redirected to the login page.")
                    if not is_allowed(nxt):
                        raise BadResponse("The server redirected outside subscribe.mt; stopped.")
                    current = nxt
                    continue
                retry_after = parse_retry_after(headers.get("Retry-After"))
                if status == 429:
                    raise RateLimited("HTTP 429 Too Many Requests.", retry_after)
                if status >= 500:
                    raise ServerError(f"HTTP {status} server error.", retry_after)
                if status == 401:
                    raise AuthRequired("HTTP 401 Unauthorized.")
                if status == 403:
                    body, _cut = self._read_text(stream)
                    if looks_like_login(body):
                        raise AuthRequired("HTTP 403 with a login form.")
                    raise Forbidden("HTTP 403 Forbidden (possibly a firewall/Cloudflare block).")
                if status in (404, 410):
                    raise IssueMissing(f"HTTP {status}: no such file.")
                if status != 200:
                    raise BadResponse(f"Unexpected HTTP {status}.")
                if is_login_url(current):
                    raise AuthRequired("Landed on the login page.")
                return self._body(current, headers, stream, dest, stop, expect_pdf or probe, probe)
            finally:
                try:
                    stream.close()
                except Exception:
                    pass
        raise BadResponse("Too many redirects.")

    @staticmethod
    def _read_text(stream, limit=MAX_HTML_BYTES, first=b""):
        """Return (text, cut_short). A dropped connection keeps whatever arrived."""
        data = bytearray(first)
        cut = False
        while len(data) < limit:
            try:
                chunk = stream.read(min(1024 * 1024, limit - len(data)))
            except http.client.IncompleteRead as exc:
                data.extend(exc.partial or b"")
                cut = True
                break
            except (http.client.HTTPException, socket.timeout, TimeoutError, OSError):
                cut = True
                break
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data).decode("utf-8", "replace"), cut

    def _body(self, url, headers, stream, dest, stop, expect_pdf, probe=False):
        first = b""
        while len(first) < 8:
            try:
                chunk = stream.read(8 - len(first))
            except http.client.IncompleteRead as exc:
                first += exc.partial or b""
                break
            except (http.client.HTTPException, socket.timeout, TimeoutError, OSError) as exc:
                raise NetworkError(f"Connection dropped: {exc!s}") from None
            if not chunk:
                break
            first += chunk
        if first.startswith(b"%PDF-"):
            if probe:
                length = headers.get("Content-Length")
                return Result(url=url, kind="pdf", size=int(length) if length and length.isdigit() else 0)
            if not (expect_pdf and dest):
                raise BadResponse("Received a PDF where a web page was expected.")
            return self._save_pdf(url, headers, stream, first, Path(dest), stop)
        text, cut = self._read_text(stream, first=first)
        # Even a cut-short page is conclusive if its start shows the login form or the
        # site's "not found" listing (both appear near the top of the page).
        if looks_like_login(text):
            raise AuthRequired("HTTP 200 returned a login/unauthorized page, not the file.")
        if expect_pdf:
            if re.search(r"<body[^>]*class=\"[^\"]*\b(?:blog|error404|search-no-results)\b", text):
                raise IssueMissing("HTTP 200 returned the site's page listing, not a PDF: file not found.")
            if cut:
                raise NetworkError("Connection dropped before the response could be identified.")
            raise BadResponse("HTTP 200 but the response is not a PDF.")
        if cut:
            raise NetworkError("Connection dropped while reading the page.")
        return Result(url=url, kind="html", text=text, size=len(text))

    @staticmethod
    def _save_pdf(url, headers, stream, first, dest, stop):
        expected = headers.get("Content-Length")
        expected = int(expected) if expected and expected.isdigit() else None
        total = len(first)
        with dest.open("wb") as out:
            out.write(first)
            while True:
                if stop is not None and stop.is_set():
                    raise InterruptedError("Stopped by user.")
                try:
                    chunk = stream.read(1024 * 1024)
                except (http.client.HTTPException, socket.timeout, TimeoutError, ConnectionError,
                        OSError) as exc:
                    raise NetworkError(f"Download interrupted: {exc!s}"[:200]) from None
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise BadResponse("Response exceeded the size limit.")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if expected is not None and total != expected:
            raise NetworkError(f"Download truncated ({total} of {expected} bytes).")
        return Result(url=url, kind="pdf", size=total)


def with_retries(call, *, retries, stop, emit, max_wait=900):
    """Bounded retries for temporary failures only; honours Retry-After."""
    attempt = 0
    while True:
        try:
            return call()
        except Temporary as exc:
            if attempt >= retries:
                raise
            wait = exc.retry_after if exc.retry_after is not None else min(300, 10 * 2 ** attempt)
            if wait > max_wait:
                raise RateLimited(f"{exc} Server asked to wait {wait:.0f}s; stopping instead.",
                                  wait) from None
            attempt += 1
            emit(f"  {exc} Waiting {wait:.0f}s before retry {attempt}/{retries}…")
            if stop is not None and stop.wait(wait):
                raise InterruptedError("Stopped by user.") from None
