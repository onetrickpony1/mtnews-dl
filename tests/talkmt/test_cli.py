import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mtnews_dl.sources.talkmt import auth
from mtnews_dl.sources.talkmt.cli import main
from mtnews_dl.sources.talkmt.easy import run_easy
from tests.talkmt.helpers import BLOG_HTML, LOGIN_HTML, Router, TempConfig, listing_html, pdf_bytes

DAILY = "https://subscribe.mt/newspapers/daily/"
FILE = "https://subscribe.mt/newspapers/mp-files/{}/"
NOV3 = "It-Tlieta, 3 ta' Novembru 2026"


def store_session(config):
    line = "\t".join(("#HttpOnly_.subscribe.mt", "TRUE", "/", "TRUE", str(int(time.time()) + 9999),
                      "wordpress_logged_in_x", "V"))
    auth.write_private(config / "cookies.txt", auth.HEADER + line + "\n")


class CliDownloads(unittest.TestCase):
    def setUp(self):
        self.cfg = TempConfig()
        self.config = self.cfg.__enter__()
        self.addCleanup(self.cfg.__exit__)
        store_session(self.config)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def cli(self, argv, routes, fallback=None):
        router = Router(routes, fallback=fallback)
        out, err = io.StringIO(), io.StringIO()
        with patch("mtnews_dl.sources.talkmt.net.Session._open", side_effect=lambda url: router(url)), \
                patch("threading.Event.wait", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, router, out.getvalue() + err.getvalue()

    def test_date_download_success_and_exit_codes(self):
        page = listing_html(("l-orizzont-2026-11-03.pdf", "L-Orizzont"))
        routes = {DAILY: (200, {}, page), FILE.format("l-orizzont-2026-11-03.pdf"): (200, {}, pdf_bytes(NOV3))}
        code, router, text = self.cli(["download", "--date", "2026-11-03", "-p", "l-orizzont", "-P", str(self.out)], routes)
        self.assertEqual(code, 0, text)
        self.assertTrue((self.out / "LOrizzont-2026-11-03.pdf").is_file())
        self.assertEqual(router.calls, [DAILY, FILE.format("l-orizzont-2026-11-03.pdf")])

    def test_missing_single_date_returns_4(self):
        routes = {DAILY: (200, {}, listing_html(("l-orizzont-2026-11-03.pdf", "L"))),
                  FILE.format("l-orizzont-2026-11-01.pdf"): (200, {}, BLOG_HTML),
                  FILE.format("l-orizzont-01-11-2026.pdf"): (200, {}, BLOG_HTML)}
        code, router, _t = self.cli(["download", "--date", "2026-11-01", "-p", "l-orizzont", "-P", str(self.out)],
                                    routes, fallback=(200, {}, BLOG_HTML))
        self.assertGreater(len(router.calls), 3)  # every variant tried before "missing"
        self.assertEqual(code, 4)

    def test_mismatch_returns_3(self):
        routes = {DAILY: (200, {}, listing_html(("l-orizzont-2026-11-03.pdf", "L"))),
                  FILE.format("l-orizzont-2026-11-03.pdf"): (200, {}, pdf_bytes("1 ta' Novembru 2026"))}
        code, _r, text = self.cli(["download", "--date", "2026-11-03", "-p", "l-orizzont", "-P", str(self.out)],
                                  routes, fallback=(200, {}, BLOG_HTML))
        self.assertEqual(code, 3, text)
        code, _r, text = self.cli(["review", "-P", str(self.out)], {})
        self.assertIn("wrong_date", text)

    def test_expired_session_on_site_stops_with_instructions(self):
        code, router, text = self.cli(["download", "2026-10", "-P", str(self.out)], {DAILY: (200, {}, LOGIN_HTML)})
        self.assertEqual(code, 5)
        self.assertEqual(router.calls, [DAILY])
        self.assertIn("auth --cookies", text)

    def test_dry_run_makes_only_the_listing_request(self):
        routes = {DAILY: (200, {}, listing_html(("l-orizzont-2026-11-03.pdf", "L")))}
        code, router, text = self.cli(["download", "--from", "2026-10-30", "--to", "2026-11-01",
                                       "-p", "l-orizzont", "--dry-run"], routes)
        self.assertEqual((code, router.calls), (0, [DAILY]))
        self.assertIn("l-orizzont-2026-10-31.pdf", text)

    def test_argument_validation(self):
        for argv in (["download"], ["download", "--date", "2026-11-03", "2026-10"],
                     ["download", "--to", "2026-11-01"], ["download", "--date", "2026-13-01"],
                     ["download", "--date", "2999-01-01"], ["download", "--year", "2026", "-p", "nope"],
                     ["download", "--date", "2026-11-03", "--delay", "0"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                main(argv)
            self.assertEqual(ctx.exception.code, 2, argv)


class EasyMode(unittest.TestCase):
    def wizard(self, answers, status="ok", callback=None):
        callback = callback or Mock(return_value=0)
        out = io.StringIO()
        with patch("mtnews_dl.sources.talkmt.easy.sys.stdin.isatty", return_value=True), \
                patch("mtnews_dl.sources.talkmt.auth.offline_status", return_value=dict(
                    stored=status != "missing", state=status, cookies=3, login_cookie_expires="2026-12-01 10:00")), \
                patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(out):
            code = run_easy(callback)
        return code, callback, out.getvalue()

    def test_single_date_current_folder(self):
        code, cb, text = self.wizard(["", "l", "d", "2026-11-03", "", "y"])
        self.assertEqual(code, 0)
        cb.assert_called_once_with(["download", "--date", "2026-11-03", "--publication", "l-orizzont"])
        self.assertIn("Equivalent command: talkmt-dl download --date 2026-11-03 --publication l-orizzont", text)

    def test_year_custom_folder_and_delay_with_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "Papers 2025"
            code, cb, _ = self.wizard(["y", "", "y", "1800", "2025", "n", str(folder), "0", "5", "yes"])
            cb.assert_called_once_with(["download", "--year", "2025", "--output-dir", str(folder.resolve()),
                                        "--delay", "5"])
            self.assertFalse(folder.exists())

    def test_range_rejects_reversed_dates(self):
        code, cb, _ = self.wizard(["", "t", "r", "2026-10-10", "2026-10-01", "2026-10-31", "", "3", "y"])
        cb.assert_called_once_with(["download", "--from", "2026-10-10", "--to", "2026-10-31",
                                    "--publication", "it-torca", "--delay", "3"])

    def test_missing_session_asks_for_a_path_not_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            cookie = Path(temp) / "c.txt"
            cookie.write_text("x")
            cb = Mock(return_value=0)
            code, cb, text = self.wizard(["/no/such/file", str(cookie), "", "c", "", "y"], status="missing",
                                         callback=cb)
            self.assertEqual(cb.call_args_list[0].args[0], ["auth", "--cookies", str(cookie.resolve())])
            self.assertEqual(cb.call_args_list[1].args[0], ["download", "--listed"])
            self.assertIn("never paste cookies", text)

    def test_failed_auth_downloads_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            cookie = Path(temp) / "c.txt"
            cookie.write_text("x")
            code, cb, _ = self.wizard([str(cookie)], status="expired", callback=Mock(return_value=5))
            self.assertEqual(code, 5)
            self.assertEqual(cb.call_count, 1)

    def test_quit_and_default_no(self):
        for answers in (["q"], ["", "a", "d", "", "", ""], ["", "q"]):
            code, cb, _ = self.wizard(answers)
            self.assertEqual(code, 0)
            cb.assert_not_called()

    def test_needs_terminal(self):
        with patch("mtnews_dl.sources.talkmt.easy.sys.stdin.isatty", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_easy(Mock()), 1)


if __name__ == "__main__":
    unittest.main()
