import io
import threading
import unittest
from contextlib import redirect_stdout

from mtnews_dl.sources.talkmt.coverage import report, sample_date, survey
from mtnews_dl.sources.talkmt.discovery import load_patterns
from mtnews_dl.sources.talkmt.net import AuthRequired
from tests.talkmt.helpers import BLOG_HTML, LOGIN_HTML, Router, TempConfig, no_wait_event, pdf_bytes, session_with

FILE = "https://subscribe.mt/newspapers/mp-files/{}/"


class Coverage(unittest.TestCase):
    def test_sample_dates(self):
        self.assertEqual(sample_date("2026-02", "it-torca"), "2026-02-15")  # a Sunday
        self.assertEqual(sample_date("2026-03", "it-torca"), "2026-03-15")
        self.assertEqual(sample_date("2026-03", "l-orizzont"), "2026-03-16")  # 15th is Sunday
        self.assertEqual(sample_date("2026-04", "l-orizzont"), "2026-04-15")

    def test_probe_reads_little_and_falls_back_then_caches(self):
        with TempConfig():
            big = pdf_bytes() + b"x" * (5 * 1024 * 1024)
            dmy = FILE.format("l-orizzont-15-04-2026.pdf")
            router = Router({dmy: (200, {"Content-Length": str(len(big))}, big)}, fallback=(200, {}, BLOG_HTML))
            read = {}
            original = router.__call__

            def spy(url):
                status, headers, stream = original(url)
                inner = stream.read

                def counting(n=-1):
                    data = inner(n)
                    read[url] = read.get(url, 0) + len(data)
                    return data
                stream.read = counting
                return status, headers, stream
            session = session_with(spy)
            results, n = survey(session, load_patterns("/nonexistent"), [], ["l-orizzont"], ["2026-04", "2026-05"],
                                formats="observed", delay=0, stop=no_wait_event(), emit=lambda _l: None)
            self.assertEqual(results["l-orizzont@2026-04-15"]["result"], "pdf")
            self.assertLess(read[dmy], 1024)  # did not download the file
            self.assertEqual(results["l-orizzont@2026-05-15"]["result"], "not found")
            self.assertEqual(len(results["l-orizzont@2026-05-15"]["tried"]), 2)
            router.calls.clear()
            survey(session, load_patterns("/nonexistent"), [], ["l-orizzont"], ["2026-04", "2026-05"],
                   formats="observed", delay=0, stop=no_wait_event(), emit=lambda _l: None)
            self.assertEqual(router.calls, [])  # cached
            text = report(results, ["l-orizzont"], ["2026-04", "2026-05"])
            self.assertIn("2026 #.", text)

    def test_login_stops_survey(self):
        with TempConfig():
            router = Router({}, fallback=(200, {}, LOGIN_HTML))
            with self.assertRaises(AuthRequired):
                survey(session_with(router), load_patterns("/nonexistent"), [], ["l-orizzont"], ["2026-04"],
                       delay=0, stop=no_wait_event(), emit=lambda _l: None)
            self.assertEqual(len(router.calls), 1)


if __name__ == "__main__":
    unittest.main()
