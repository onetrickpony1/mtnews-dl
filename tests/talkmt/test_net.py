import http.cookiejar
import threading
import unittest
import urllib.request
from email.message import Message
from pathlib import Path
import tempfile

from mtnews_dl.sources.talkmt import net
from mtnews_dl.sources.talkmt.net import (AuthRequired, BadResponse, IssueMissing, NetworkError, RateLimited,
                           ServerError, with_retries)
from tests.talkmt.helpers import BLOG_HTML, LOGIN_HTML, FakeStream, Router, pdf_bytes, session_with

URL = "https://subscribe.mt/newspapers/mp-files/l-orizzont-2026-11-03.pdf/"
LOGIN = ("https://subscribe.mt/newspapers/login/?action=mepr_unauthorized&mepr-unauth-page=10403"
         "&redirect_to=%2Fnewspapers%2Fmp-files%2Fl-orizzont-2026-11-03.pdf%2F")


def headers(**values):
    msg = Message()
    for k, v in values.items():
        msg[k.replace("_", "-")] = v
    return msg


class ResponseClassification(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name) / "x.part"

    def get(self, routes, url=URL):
        router = Router(routes)
        return session_with(router).get(url, dest=self.dest, expect_pdf=True), router

    def test_redirect_to_login_is_auth_failure_and_login_page_is_not_fetched(self):
        router = Router({URL: (302, headers(Location=LOGIN), b"")})
        with self.assertRaises(AuthRequired):
            session_with(router).get(URL, dest=self.dest, expect_pdf=True)
        self.assertEqual(router.calls, [URL])
        self.assertFalse(self.dest.exists())

    def test_http_200_login_page_is_never_a_pdf(self):
        with self.assertRaises(AuthRequired):
            self.get({URL: (200, headers(), LOGIN_HTML)})
        self.assertFalse(self.dest.exists())

    def test_http_200_site_listing_means_missing(self):
        with self.assertRaises(IssueMissing):
            self.get({URL: (200, headers(), BLOG_HTML)})

    def test_http_200_other_html_is_malformed(self):
        with self.assertRaises(BadResponse):
            self.get({URL: (200, headers(), b"<html><body class='page'>hello</body></html>")})

    def test_404_missing_and_401_auth(self):
        with self.assertRaises(IssueMissing):
            self.get({URL: (404, headers(), b"")})
        with self.assertRaises(AuthRequired):
            self.get({URL: (401, headers(), b"")})

    def test_pdf_saved_and_content_length_checked(self):
        data = pdf_bytes()
        result, _ = self.get({URL: (200, headers(Content_Length=str(len(data))), data)})
        self.assertEqual(result.kind, "pdf")
        self.assertEqual(self.dest.read_bytes(), data)
        with self.assertRaises(NetworkError):
            self.get({URL: (200, headers(Content_Length=str(len(data) + 10)), data)})

    def test_redirect_off_site_refused_before_request(self):
        router = Router({URL: (302, headers(Location="https://evil.example/x.pdf"), b"")})
        with self.assertRaises(BadResponse):
            session_with(router).get(URL, dest=self.dest, expect_pdf=True)
        self.assertEqual(router.calls, [URL])

    def test_malformed_urls_refused(self):
        for bad in ("http://subscribe.mt/newspapers/", "https://subscribe.mt:8443/x",
                    "https://user:pw@subscribe.mt/x", "https://subscribe.mt.evil.com/x",
                    "https://[::1/x", "javascript:alert(1)"):
            router = Router({})
            with self.assertRaises(BadResponse, msg=bad):
                session_with(router).get(bad)
            self.assertEqual(router.calls, [])

    def test_stop_mid_download(self):
        stop = threading.Event()
        data = pdf_bytes() + b"x" * (3 * 1024 * 1024)
        router = Router({URL: (200, headers(), FakeStream(data, on_read=stop.set))})
        with self.assertRaises(InterruptedError):
            session_with(router).get(URL, dest=self.dest, expect_pdf=True, stop=stop)


class Retries(unittest.TestCase):
    def run_with(self, routes, retries=3):
        router = Router(routes)
        waits = []
        stop = threading.Event()
        stop.wait = lambda t=None: waits.append(t) or False
        session = session_with(router)
        call = lambda: session.get("https://subscribe.mt/newspapers/daily/")  # noqa: E731
        return with_retries(call, retries=retries, stop=stop, emit=lambda _l: None), router, waits

    def test_retry_after_is_honoured(self):
        daily = "https://subscribe.mt/newspapers/daily/"
        ok = (200, headers(), b"<html><body>ok</body></html>")
        result, router, waits = self.run_with({daily: [(429, headers(Retry_After="7"), b""), ok]})
        self.assertEqual(result.kind, "html")
        self.assertEqual(waits, [7.0])
        self.assertEqual(len(router.calls), 2)

    def test_server_errors_are_bounded(self):
        daily = "https://subscribe.mt/newspapers/daily/"
        with self.assertRaises(ServerError):
            self.run_with({daily: [(503, headers(), b"")]}, retries=2)

    def test_huge_retry_after_stops_instead_of_waiting(self):
        daily = "https://subscribe.mt/newspapers/daily/"
        with self.assertRaises(RateLimited):
            self.run_with({daily: [(429, headers(Retry_After="7200"), b"")]})

    def test_auth_failure_is_never_retried(self):
        daily = "https://subscribe.mt/newspapers/daily/"
        router = Router({daily: (200, headers(), LOGIN_HTML)})
        session = session_with(router)
        with self.assertRaises(AuthRequired):
            with_retries(lambda: session.get(daily), retries=3, stop=threading.Event(), emit=print)
        self.assertEqual(len(router.calls), 1)


class CookieScope(unittest.TestCase):
    def test_cookies_only_sent_to_subscribe_mt(self):
        jar = http.cookiejar.CookieJar()
        cookie = http.cookiejar.Cookie(0, "wordpress_logged_in_x", "SECRET", None, False, ".subscribe.mt",
                                       True, True, "/", True, True, None, False, None, None, {})
        jar.set_cookie(cookie)
        mine = urllib.request.Request("https://subscribe.mt/newspapers/daily/")
        other = urllib.request.Request("https://example.com/")
        lookalike = urllib.request.Request("https://notsubscribe.mt/")
        for req in (mine, other, lookalike):
            jar.add_cookie_header(req)
        self.assertIn("SECRET", mine.get_header("Cookie"))
        self.assertIsNone(other.get_header("Cookie"))
        self.assertIsNone(lookalike.get_header("Cookie"))

    def test_login_url_detection(self):
        self.assertTrue(net.is_login_url(LOGIN))
        self.assertTrue(net.is_login_url("https://subscribe.mt/newspapers/login/"))
        self.assertFalse(net.is_login_url(URL))


if __name__ == "__main__":
    unittest.main()


class DroppedConnections(unittest.TestCase):
    """Server cuts the response off mid-way (http.client.IncompleteRead), seen live 2026-11-03."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name) / "x.part"

    @staticmethod
    def cut_stream(data, after):
        import http.client
        stream = FakeStream(data)
        inner = stream.read

        def read(n=-1):
            if stream.tell() >= after:
                raise http.client.IncompleteRead(b"", 23388)
            return inner(min(n if n > 0 else len(data), after - stream.tell()))
        stream.read = read
        return stream

    def test_cut_not_found_page_still_classified_as_missing(self):
        page = BLOG_HTML + b"x" * 50000
        router = Router({URL: (200, headers(), self.cut_stream(page, 26371))})
        with self.assertRaises(IssueMissing):
            session_with(router).get(URL, dest=self.dest, expect_pdf=True)

    def test_cut_before_page_is_identifiable_is_temporary(self):
        page = b"<html><head>" + b" " * 50000 + b"</head><body class='blog'>"
        router = Router({URL: (200, headers(), self.cut_stream(page, 1000))})
        with self.assertRaises(NetworkError):
            session_with(router).get(URL, dest=self.dest, expect_pdf=True)

    def test_cut_pdf_is_temporary(self):
        data = pdf_bytes() + b"x" * 3 * 1024 * 1024
        router = Router({URL: (200, headers(), self.cut_stream(data, 2 * 1024 * 1024))})
        with self.assertRaises(NetworkError):
            session_with(router).get(URL, dest=self.dest, expect_pdf=True)

    def test_cut_is_retried_then_succeeds(self):
        page = b"<html><head>" + b" " * 50000
        good = pdf_bytes()
        router = Router({URL: [(200, headers(), self.cut_stream(page, 100)), (200, headers(), good)]})
        stop = threading.Event()
        stop.wait = lambda t=None: False
        session = session_with(router)
        result = with_retries(lambda: session.get(URL, dest=self.dest, expect_pdf=True),
                              retries=3, stop=stop, emit=lambda _l: None)
        self.assertEqual((result.kind, len(router.calls)), ("pdf", 2))

    def test_connection_reset_on_open_is_temporary(self):
        import http.client
        router = Router({URL: http.client.RemoteDisconnected("Remote end closed connection")})
        with self.assertRaises(NetworkError):
            session_with(router).get(URL, dest=self.dest, expect_pdf=True)
