import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mtnews_dl.sources.tom import cli, feed
from mtnews_dl.sources.tom.library import Library, StopRun
from tests.pdfs import pdf_bytes

FEED_XML = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dcterms="http://purl.org/dc/terms/">
 <entry><title>Times of Malta 2026-11-03</title><dcterms:issued>2026-11-03</dcterms:issued>
  <link type="application/pdf" href="./wp-content/uploads/a-2026-11-03.pdf"/></entry>
 <entry><title>The Sunday Times of Malta 2026-11-01</title><dcterms:issued>2026-11-01</dcterms:issued>
  <link type="application/pdf" href="https://app.timesofmalta.com/wp-content/uploads/b.pdf"/></entry>
 <entry><title>Elsewhere</title><dcterms:issued>2026-11-02</dcterms:issued>
  <link type="application/pdf" href="https://evil.example/x.pdf"/></entry>
 <entry><title>Article</title><dcterms:issued>2026-11-02</dcterms:issued>
  <link type="text/html" href="./article"/></entry>
</feed>"""

URL3 = "https://app.timesofmalta.com/wp-content/uploads/a-2026-11-03.pdf"
URL1 = "https://app.timesofmalta.com/wp-content/uploads/b.pdf"


class Env(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        patcher = mock.patch.dict(os.environ, {"TOM_DL_DATA": str(self.dir / "data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        self.out = self.dir / "out"
        self.served = {}
        self.calls = []

        def fake_fetch(url, dest=None, stop=None, timeout=60):
            self.calls.append(url)
            value = self.served.get(url, feed.LinkMissing("HTTP 404"))
            if isinstance(value, Exception):
                raise value
            if dest is None:
                return value
            Path(dest).write_bytes(value)
        patcher = mock.patch.object(feed, "fetch", side_effect=fake_fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, entries=None):
        record = feed.LinkRecord()
        record.rows = {}
        record.add_feed(entries if entries is not None else feed.parse_feed(FEED_XML))
        return record

    def run_lib(self, dates, record=None, **kw):
        lib = Library(self.out)
        lines = []
        summary = lib.run(dates, record or self.record(), delay=1, stop=_NoWait(), emit=lines.append, **kw)
        return lib, summary, lines


class _NoWait(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


class FeedTests(Env):
    def test_parse_keeps_publisher_pdf_links_only(self):
        entries = feed.parse_feed(FEED_XML)
        self.assertEqual(entries, {"2026-11-03": [dict(url=URL3, title="Times of Malta 2026-11-03")],
                                   "2026-11-01": [dict(url=URL1, title="The Sunday Times of Malta 2026-11-01")]})

    def test_record_keeps_links_that_left_the_feed(self):
        record = self.record()
        record.save()
        later = feed.LinkRecord()
        later.add_feed({"2026-11-04": [dict(url=URL3.replace("03", "04"), title="x")]})
        self.assertIn("2026-11-01", later.dates())
        self.assertIn("2026-11-04", later.dates())

    def test_seed_covers_first_epaper_issue(self):
        self.assertTrue(feed.LinkRecord().links("2024-11-19"))

    def test_rejects_other_hosts(self):
        self.assertFalse(feed.is_publisher_url("https://app.timesofmalta.com.evil.example/x.pdf"))
        self.assertFalse(feed.is_publisher_url("http://app.timesofmalta.com/x.pdf"))


class EpaperTests(Env):
    def test_verified_download_then_resume_without_requests(self):
        self.served[URL3] = pdf_bytes("Tuesday, November 3, 2026 | No. 28,300")
        lib, summary, _ = self.run_lib(["2026-11-03"])
        self.assertEqual(summary["downloaded"], 1)
        self.assertTrue((self.out / "Times-of-Malta-2026-11-03.pdf").is_file())
        self.calls.clear()
        _, summary, _ = self.run_lib(["2026-11-03"])
        self.assertEqual((summary["skipped"], self.calls), (1, []))

    def test_wrong_printed_date_is_held_for_review_and_accepted(self):
        self.served[URL3] = pdf_bytes("Monday, November 2, 2026")
        lib, summary, _ = self.run_lib(["2026-11-03"])
        self.assertEqual(summary["review"], 1)
        row = lib.rows["2026-11-03"]
        self.assertEqual((row["status"], row["detected_dates"]), ("wrong_date", ["2026-11-02"]))
        self.assertIn(".tom-dl", row["path"])
        self.assertFalse((self.out / "Times-of-Malta-2026-11-03.pdf").exists())
        self.assertEqual(cli.main(["accept", "2026-11-03", "--yes", "-P", str(self.out)]), 0)
        self.assertTrue((self.out / "Times-of-Malta-2026-11-03.pdf").is_file())

    def test_missing_link_and_unlisted_dates(self):
        lib, summary, lines = self.run_lib(["2026-11-01", "2024-05-05"])
        self.assertEqual(lib.rows["2026-11-01"]["status"], "missing")
        self.assertEqual(lib.rows["2024-05-05"]["status"], "unavailable")
        self.assertTrue(any("--archive" in line for line in lines))

    def test_non_pdf_response_stops_the_run(self):
        self.served[URL3] = b"<html>Just a moment...</html>"
        with self.assertRaises(StopRun) as ctx:
            self.run_lib(["2026-11-03"])
        self.assertEqual(ctx.exception.code, 6)

    def test_existing_file_is_checked_not_downloaded(self):
        self.out.mkdir(parents=True)
        (self.out / "Times-of-Malta-2026-11-03.pdf").write_bytes(pdf_bytes("Tuesday, November 3, 2026"))
        lib, summary, _ = self.run_lib(["2026-11-03"])
        self.assertEqual((summary["skipped"], self.calls), (1, []))
        self.assertEqual(lib.rows["2026-11-03"]["status"], "verified")

    def test_library_layout(self):
        self.served[URL3] = pdf_bytes("Tuesday, November 3, 2026")
        lib = Library(self.out, flat=False)
        lib.run(["2026-11-03"], self.record(), delay=1, stop=_NoWait(), emit=lambda _: None)
        self.assertTrue((self.out / "editions/2026/Times-of-Malta-2026-11-03.pdf").is_file())


class FakeRunner:
    def __init__(self, merged):
        self.merged, self.pages_requested, self.dates = merged, 0, []

    def issue(self, page, date, page_limit=None):
        self.dates.append(date)
        if self.merged is None:
            return None
        self.pages_requested += 2
        return self.merged(date)

    def close(self):
        pass


class ArchiveTests(Env):
    def open_with(self, runner):
        return mock.patch.object(Library, "_open_archive", return_value=(runner, object()))

    def test_archive_used_only_for_dates_without_epaper(self):
        self.served[URL3] = pdf_bytes("Tuesday, November 3, 2026")

        def merged(date):
            path = self.dir / f"merged-{date}.pdf"
            path.write_bytes(pdf_bytes("Thursday, May 2, 2024"))
            return path
        runner = FakeRunner(merged)
        with self.open_with(runner):
            lib, summary, _ = self.run_lib(["2024-05-02", "2026-11-03"],
                                           archive=dict(ceiling=290, wait=False, max_pages=None))
        self.assertEqual(runner.dates, ["2024-05-02"])
        self.assertEqual(summary["downloaded"], 2)
        self.assertEqual(lib.rows["2024-05-02"]["route"], "archive")
        self.assertTrue((self.out / "Times-of-Malta-2024-05-02.pdf").is_file())

    def test_archive_not_listed_is_recorded(self):
        with self.open_with(FakeRunner(None)):
            lib, summary, _ = self.run_lib(["2024-05-05"], archive=dict(ceiling=290, wait=False, max_pages=None))
        self.assertEqual(lib.rows["2024-05-05"]["status"], "archive_not_listed")

    def test_chrome_not_set_up_means_exit_5(self):
        code = cli.main(["download", "--date", "2024-05-02", "--archive", "--offline", "-P", str(self.out)])
        self.assertEqual(code, 5)


class CliTests(Env):
    def test_selection_and_review_json(self):
        with self.assertRaises(SystemExit):
            cli.main(["download", "-P", str(self.out)])
        self.assertEqual(cli.main(["review", "--json", "-P", str(self.out)]), 0)

    def test_feed_import(self):
        snap = self.dir / "snap.xml"
        snap.write_bytes(FEED_XML)
        self.assertEqual(cli.main(["feed", "import", str(snap)]), 0)
        self.assertIn(URL1, feed.LinkRecord().links("2026-11-01"))


if __name__ == "__main__":
    unittest.main()
