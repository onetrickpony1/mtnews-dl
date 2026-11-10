import contextlib
import io
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mtnews_dl.sources.talkmt import auth
from mtnews_dl.sources.talkmt.cli import main
from mtnews_dl.sources.talkmt.net import AuthRequired, SessionExpired
from tests.talkmt.helpers import LOGIN_HTML, Router, TempConfig, listing_html

FUTURE = str(int(time.time()) + 86400 * 10)
PAST = str(int(time.time()) - 60)


def cookie_file(folder, *lines):
    path = Path(folder) / "export.txt"
    path.write_text("# Netscape HTTP Cookie File\n" + "\n".join("\t".join(l) for l in lines) + "\n")
    return path


GOOD = [("#HttpOnly_.subscribe.mt", "TRUE", "/", "TRUE", FUTURE, "wordpress_logged_in_abc", "SECRETVALUE"),
        ("subscribe.mt", "FALSE", "/", "TRUE", "0", "cf_session", "CFVALUE"),
        (".google.com", "TRUE", "/", "TRUE", FUTURE, "SID", "GOOGLESECRET"),
        (".notsubscribe.mt", "TRUE", "/", "TRUE", FUTURE, "x", "LOOKALIKE")]


class CookieImport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_import_keeps_only_site_cookies_privately(self):
        with TempConfig() as config:
            info = auth.import_cookies(cookie_file(self.temp.name, *GOOD))
            stored = (config / "cookies.txt")
            text = stored.read_text()
            self.assertEqual(info["kept"], 2)
            self.assertEqual(info["dropped_other_domains"], 2)
            self.assertNotIn("GOOGLESECRET", text)
            self.assertNotIn("LOOKALIKE", text)
            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o700)
            jar = auth.load_jar()
            self.assertEqual(sorted(c.name for c in jar), ["cf_session", "wordpress_logged_in_abc"])

    def test_cli_never_prints_cookie_values(self):
        with TempConfig():
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["auth", "--cookies", str(cookie_file(self.temp.name, *GOOD)), "--offline"])
                code2 = main(["auth", "--status", "--offline"])
            self.assertEqual((code, code2), (0, 0))
            for secret in ("SECRETVALUE", "CFVALUE", "GOOGLESECRET"):
                self.assertNotIn(secret, out.getvalue() + err.getvalue())

    def test_expired_login_rejected_on_import_and_load(self):
        with TempConfig() as config:
            expired = [("#HttpOnly_.subscribe.mt", "TRUE", "/", "TRUE", PAST, "wordpress_logged_in_abc", "V")]
            with self.assertRaises(SessionExpired):
                auth.import_cookies(cookie_file(self.temp.name, *expired))
            self.assertFalse((config / "cookies.txt").exists())
            auth.write_private(config / "cookies.txt", auth.HEADER + "\t".join(expired[0]) + "\n")
            with self.assertRaises(SessionExpired):
                auth.load_jar()

    def test_bad_formats(self):
        bad = Path(self.temp.name) / "c.json"
        bad.write_text('[{"name": "x", "value": "y"}]')
        with self.assertRaises(ValueError):
            auth.import_cookies(bad)
        only_other = cookie_file(self.temp.name, GOOD[2])
        with TempConfig(), self.assertRaises(ValueError):
            auth.import_cookies(only_other)

    def test_missing_session_stops_download_without_network(self):
        with TempConfig(), tempfile.TemporaryDirectory() as out_dir, \
                patch("mtnews_dl.sources.talkmt.net.Session._open", side_effect=AssertionError("network used")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = main(["download", "--date", "2026-11-03", "-P", out_dir])
            self.assertEqual(code, 5)
            self.assertIn("talkmt-dl auth --cookies", err.getvalue())


class OnlineCheck(unittest.TestCase):
    def test_login_form_means_logged_out(self):
        from tests.talkmt.helpers import session_with
        router = Router({"https://subscribe.mt/newspapers/daily/": (200, {}, LOGIN_HTML)})
        with self.assertRaises(AuthRequired):
            auth.check_online(session_with(router))

    def test_logged_in_listing(self):
        from tests.talkmt.helpers import session_with
        router = Router({"https://subscribe.mt/newspapers/daily/":
                         (200, {}, listing_html(("l-orizzont-2026-11-03.pdf", "L-Orizzont")))})
        self.assertIn("/mp-files/", auth.check_online(session_with(router)))


if __name__ == "__main__":
    unittest.main()
