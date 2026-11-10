import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from mtnews_dl.sources.mttd import catalog, cli, core
from tests.pdfs import pdf_bytes

U = "https://maltatoday.uberflip.com/read/flipbook3_title_settings/73329/{}"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.entries = {"2026-10-25": [U.format(1001)], "2026-10-27": [U.format(1002)],
                        "2026-10-30": [U.format(1003)]}
        self.pdfs = {U.format(1001): pdf_bytes("SUNDAY 25 OCTOBER 2026"),
                     U.format(1002): pdf_bytes("TUESDAY 27 OCTOBER 2026"),
                     U.format(1003): pdf_bytes("SUNDAY 25 OCTOBER 2026")}   # misprinted
        self.calls = []
        patches = [
            mock.patch.object(catalog, "discover", side_effect=lambda **kw: self.entries),
            mock.patch.object(catalog, "resolve_pdf", side_effect=lambda url, stop=None: url),
            mock.patch.object(core, "fetch", side_effect=self.fetch),
            mock.patch.object(threading.Event, "wait", lambda self, timeout=None: self.is_set()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fetch(self, url, dest=None, stop=None):
        self.calls.append(url)
        Path(dest).write_bytes(self.pdfs[url])

    def run_cli(self, *args):
        return cli.main(["download", *args, "-P", str(self.out), "--no-legacy"])


class DownloadTests(Base):
    def test_range_downloads_only_selected_dates(self):
        self.assertEqual(self.run_cli("--from", "2026-10-26", "--to", "2026-10-28"), 0)
        self.assertEqual(self.calls, [U.format(1002)])
        self.assertEqual([p.name for p in self.out.glob("*.pdf")], ["MediaToday-2026-10-27-1002.pdf"])

    def test_several_dates_and_review_exit_code(self):
        code = self.run_cli("--date", "2026-10-25", "--date", "2026-10-30")
        self.assertEqual(code, 3)
        rows = cli.read_review(self.out, flat=True)
        self.assertEqual(rows["2026-10-30@1003"]["status"], "wrong_date")
        self.assertEqual(cli.main(["accept", "2026-10-30", "--yes", "-P", str(self.out)]), 0)

    def test_resume_makes_no_requests(self):
        self.run_cli("--date", "2026-10-27")
        self.calls.clear()
        self.assertEqual(self.run_cli("--date", "2026-10-27"), 0)
        self.assertEqual(self.calls, [])

    def test_dry_run_downloads_nothing(self):
        self.assertEqual(self.run_cli("2026-10", "--dry-run"), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(list(self.out.glob("*.pdf")))

    def test_temporary_errors_are_retried(self):
        attempts = []

        def flaky(url, dest=None, stop=None):
            attempts.append(url)
            if len(attempts) == 1:
                raise urllib.error.HTTPError(url, 503, "busy", {}, None)
            Path(dest).write_bytes(self.pdfs[url])
        core.fetch.side_effect = flaky
        self.assertEqual(self.run_cli("--date", "2026-10-27", "--retries", "2"), 0)
        self.assertEqual(len(attempts), 2)


class ParsingTests(unittest.TestCase):
    def test_one_selection_only(self):
        with self.assertRaises(SystemExit):
            cli.main(["download", "2026-10", "--date", "2026-10-01"])

    def test_collection_alias_still_accepted(self):
        args = cli.build_parser().parse_args(["download", "--archive", "all"])
        self.assertEqual(args.archive, ["all"])

    def test_maltese_and_english_dates(self):
        self.assertEqual(core.dates_in("Il-Ħadd 5 ta' Jannar 2025"), ["2025-01-05"])
        self.assertEqual(core.dates_in("SUNDAY • 1 JANUARY 2023 • ISSUE 1209"), ["2023-01-01"])


if __name__ == "__main__":
    unittest.main()
