import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mtnews_dl import umbrella


class UmbrellaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {"TALKMT_DL_CONFIG": str(Path(self.temp.name) / "cfg")})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

        def fake_main(src):
            return lambda argv: self.calls.append((src.key, argv)) or 0
        patch = mock.patch.object(umbrella.Source, "main", fake_main)
        patch.start()
        self.addCleanup(patch.stop)

    def test_root_splits_ranges_by_year_and_source(self):
        root = Path(self.temp.name) / "news"
        code = umbrella.main(["download", "--from", "2025-12-31", "--to", "2026-01-01",
                              "--root", str(root), "-s", "tom", "--archive"])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [
            ("tom", ["download", "--from", "2025-12-31", "--to", "2025-12-31", "-P", str(root / "ToM/2025"), "--archive"]),
            ("tom", ["download", "--from", "2026-01-01", "--to", "2026-01-01", "-P", str(root / "ToM/2026"), "--archive"]),
        ])

    def test_subscription_source_without_session_is_skipped_unless_asked(self):
        umbrella.main(["download", "--date", "2026-11-03"])
        self.assertEqual([k for k, _ in self.calls], ["tom", "mttd"])
        self.calls.clear()
        umbrella.main(["download", "--date", "2026-11-03", "-s", "talkmt"])
        self.assertEqual([k for k, _ in self.calls], ["talkmt"])

    def test_most_serious_exit_code_wins(self):
        self.assertEqual(umbrella.worst([0, 3, 4]), 3)
        self.assertEqual(umbrella.worst([3, 5, 1]), 5)
        self.assertEqual(umbrella.worst([0, 130]), 130)

    def test_same_options_reach_every_tool(self):
        umbrella.main(["download", "2026-10", "--delay", "4", "--dry-run", "-P", "out", "-s", "mttd"])
        self.assertEqual(self.calls, [("mttd", ["download", "2026-10", "-P", "out", "--delay", "4", "--dry-run"])])


class CtrlCTests(unittest.TestCase):
    """Ctrl+C in one tool stops mtnews-dl, whatever exit code the tool returns."""

    def setUp(self):
        from mtnews_dl.common import cliargs
        self.cliargs = cliargs
        cliargs._INTERRUPTED.clear()
        self.addCleanup(cliargs._INTERRUPTED.clear)

    def test_interrupted_tool_stops_the_remaining_years(self):
        import threading
        calls = []

        def fake_main(src):
            def run(argv):
                calls.append(argv)
                handler = self.cliargs.sigint_handler(threading.Event())
                handler(2, None)          # Ctrl+C while the tool works
                return 6                  # ...surfacing as "stopped by the site"
            return run
        with mock.patch.object(umbrella.Source, "main", fake_main):
            code = umbrella.main(["download", "--from", "2023-12-31", "--to", "2024-01-01",
                                  "--root", "news", "-s", "tom", "--archive"])
        self.assertEqual(code, 130)
        self.assertEqual(len(calls), 1)

    def test_second_ctrl_c_quits_even_in_a_new_run(self):
        import threading
        first = self.cliargs.sigint_handler(threading.Event())
        first(2, None)
        later = threading.Event()
        second = self.cliargs.sigint_handler(later)
        self.assertTrue(later.is_set())   # a run started after Ctrl+C stops at once
        with self.assertRaises(KeyboardInterrupt):
            second(2, None)


if __name__ == "__main__":
    unittest.main()


class OwnBrowserPrefsTests(unittest.TestCase):
    def test_archive_site_may_download_several_files(self):
        import json
        import tempfile
        from pathlib import Path
        from mtnews_dl.common.browser import allow_site_downloads
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp)
            (profile / "Default").mkdir()
            (profile / "Default" / "Preferences").write_text('{"keep": 1}', encoding="utf-8")
            allow_site_downloads(profile)
            prefs = json.loads((profile / "Default" / "Preferences").read_text(encoding="utf-8"))
            self.assertEqual(prefs["keep"], 1)
            self.assertEqual(prefs["profile"]["content_settings"]["exceptions"]["automatic_downloads"]
                             ["https://timesofmalta.com:443,*"]["setting"], 1)
            self.assertFalse(prefs["download"]["prompt_for_download"])
