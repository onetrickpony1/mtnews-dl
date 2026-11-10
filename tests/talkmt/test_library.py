import json
import tempfile
import threading
import unittest
from pathlib import Path

from mtnews_dl.sources.talkmt.discovery import learn, load_patterns, parse_listing
from mtnews_dl.sources.talkmt.library import Library
from mtnews_dl.sources.talkmt.net import AuthRequired, NotEntitled
from tests.talkmt.helpers import (BLOG_HTML, LOGIN_HTML, FakeStream, Router, listing_html, no_wait_event,
                           pdf_bytes, session_with)

FILE = "https://subscribe.mt/newspapers/mp-files/{}/"
LOGIN_302 = (302, {"Location": "https://subscribe.mt/newspapers/login/?action=mepr_unauthorized"}, b"")
NOV3 = "It-Tlieta, 3 ta' Novembru 2026"


class LibraryRuns(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.listing = parse_listing(listing_html(("l-orizzont-2026-11-03.pdf", "L-Orizzont")).decode())
        self.patterns = learn(load_patterns(path="/nonexistent"), self.listing)
        del self.patterns["l-orizzont"]["l-orizzont-{d}-{m}-{Y}.pdf"]  # one pattern; see BothFilenameFormats
        self.logs = []

    def run_lib(self, routes, dates, pubs=("l-orizzont",), *, flat=True, stop=None, recheck=None, **kw):
        router = Router(routes)
        lib = Library(self.root, flat=flat)
        groups = Library.plan(self.listing, self.patterns, dates, list(pubs), formats="observed")
        summary = lib.run(session_with(router), groups, delay=1, stop=stop or no_wait_event(),
                          emit=self.logs.append, recheck_session=recheck, patterns=self.patterns, **kw)
        return lib, summary, router

    def test_listed_issue_verified_then_skipped_on_resume(self):
        routes = {FILE.format("l-orizzont-2026-11-03.pdf"): (200, {}, pdf_bytes(NOV3))}
        lib, summary, router = self.run_lib(routes, ["2026-11-03"])
        self.assertEqual(summary["downloaded"], 1)
        saved = self.root / "LOrizzont-2026-11-03.pdf"
        self.assertTrue(saved.is_file())
        row = lib.rows["2026-11-03@l-orizzont-2026-11-03.pdf"]
        self.assertEqual(row["status"], "verified")
        self.assertEqual(len(row["sha256"]), 64)
        self.assertEqual(list((self.root / ".talkmt-dl" / "pending").iterdir()), [])
        _lib, summary, router = self.run_lib({}, ["2026-11-03"])  # no routes: any request fails
        self.assertEqual((summary["skipped"], router.calls), (1, []))

    def test_structured_library_layout(self):
        routes = {FILE.format("l-orizzont-2026-11-03.pdf"): (200, {}, pdf_bytes(NOV3))}
        self.run_lib(routes, ["2026-11-03"], flat=False)
        self.assertTrue((self.root / "editions/l-orizzont/2026/LOrizzont-2026-11-03.pdf").is_file())

    def test_unlisted_date_uses_pattern_and_records_evidence(self):
        routes = {FILE.format("l-orizzont-2025-06-10.pdf"): (200, {}, pdf_bytes("It-Tlieta, 10 ta' Gunju 2025"))}
        _lib, summary, _ = self.run_lib(routes, ["2025-06-10"])
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(self.patterns["l-orizzont"]["l-orizzont-{Y}-{m}-{d}.pdf"]["unlisted_confirmed"],
                         ["2025-06-10"])

    def test_filename_date_mismatch_goes_to_pending_with_metadata(self):
        routes = {FILE.format("l-orizzont-2026-11-03.pdf"): (200, {}, pdf_bytes("It-Tnejn, 2 ta' Novembru 2026"))}
        lib, summary, _ = self.run_lib(routes, ["2026-11-03"])
        row = lib.rows["2026-11-03@l-orizzont-2026-11-03.pdf"]
        self.assertEqual((row["status"], row["detected_dates"]), ("wrong_date", ["2026-11-02"]))
        self.assertEqual(Path(row["path"]).parent.parts[-2:], (".talkmt-dl", "pending"))
        sidecar = json.loads(Path(row["path"]).with_suffix(".review.json").read_text())
        self.assertEqual(sidecar["issue_date"], "2026-11-03")
        self.assertFalse((self.root / "LOrizzont-2026-11-03.pdf").exists())
        # Not downloaded again on resume; accept promotes it.
        _l, summary, router = self.run_lib({}, ["2026-11-03"])
        self.assertEqual(router.calls, [])
        dest = Library(self.root).accept("2026-11-03@l-orizzont-2026-11-03.pdf")
        self.assertEqual(dest.name, "LOrizzont-2026-11-02.pdf")
        self.assertFalse(Path(row["path"]).with_suffix(".review.json").exists())

    def test_missing_dates_continue_and_are_remembered(self):
        routes = {FILE.format("l-orizzont-2025-01-01.pdf"): (200, {}, BLOG_HTML),
                  FILE.format("l-orizzont-2025-01-02.pdf"): (200, {}, pdf_bytes("2 ta' Jannar 2025"))}
        lib, summary, _ = self.run_lib(routes, ["2025-01-01", "2025-01-02"])
        self.assertEqual((summary["missing"], summary["downloaded"]), (1, 1))  # issues, not URLs
        self.assertEqual(lib.rows["2025-01-01@l-orizzont-2025-01-01.pdf"]["status"], "missing")
        _l, _s, router = self.run_lib({}, ["2025-01-01"])
        self.assertEqual(router.calls, [])  # known missing, no new request
        _l, _s, router = self.run_lib({FILE.format("l-orizzont-2025-01-01.pdf"): (200, {}, BLOG_HTML)},
                                      ["2025-01-01"], retry_missing=True)
        self.assertEqual(len(router.calls), 1)

    def test_login_redirect_while_logged_out_stops_run(self):
        routes = {FILE.format("l-orizzont-2025-01-01.pdf"): LOGIN_302,
                  FILE.format("l-orizzont-2025-01-02.pdf"): (200, {}, pdf_bytes("2 ta' Jannar 2025"))}

        def recheck():
            raise AuthRequired("logged out")
        with self.assertRaises(AuthRequired):
            self.run_lib(routes, ["2025-01-01", "2025-01-02"], recheck=recheck)
        state = json.loads((self.root / ".talkmt-dl/state.json").read_text())
        self.assertNotIn("2025-01-02@l-orizzont-2025-01-02.pdf", state)

    def test_refused_while_logged_in_is_recorded_then_stops_after_three(self):
        dates = ["2025-01-0%d" % d for d in range(1, 6)]
        routes = {FILE.format(f"l-orizzont-{d}.pdf"): LOGIN_302 for d in dates}
        with self.assertRaises(NotEntitled):
            self.run_lib(routes, dates, recheck=lambda: None)
        state = json.loads((self.root / ".talkmt-dl/state.json").read_text())
        self.assertEqual([r["status"] for r in state.values()], ["not_entitled"] * 3)

    def test_interrupted_download_leaves_nothing_and_resumes(self):
        stop = threading.Event()
        data = pdf_bytes(NOV3) + b"%" * (2 * 1024 * 1024)
        routes = {FILE.format("l-orizzont-2026-11-03.pdf"): (200, {}, FakeStream(data, on_read=stop.set))}
        with self.assertRaises(InterruptedError):
            self.run_lib(routes, ["2026-11-03"], stop=stop)
        self.assertEqual(list((self.root / ".talkmt-dl/pending").iterdir()), [])
        self.assertFalse((self.root / "LOrizzont-2026-11-03.pdf").exists())
        routes = {FILE.format("l-orizzont-2026-11-03.pdf"): (200, {}, pdf_bytes(NOV3))}
        _l, summary, _ = self.run_lib(routes, ["2026-11-03"])
        self.assertEqual(summary["downloaded"], 1)

    def test_truncated_pdf_is_failed_not_saved(self):
        routes = {FILE.format("l-orizzont-2026-11-03.pdf"): (200, {}, pdf_bytes(NOV3, eof=False))}
        lib, summary, _ = self.run_lib(routes, ["2026-11-03"])
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(lib.rows["2026-11-03@l-orizzont-2026-11-03.pdf"]["status"], "failed")
        self.assertEqual(list((self.root / ".talkmt-dl/pending").iterdir()), [])

    def test_existing_file_is_checked_not_overwritten(self):
        mine = self.root / "LOrizzont-2026-11-03.pdf"
        mine.write_bytes(pdf_bytes(NOV3))
        before = mine.read_bytes()
        _l, summary, router = self.run_lib({}, ["2026-11-03"])
        self.assertEqual(router.calls, [])
        self.assertEqual(mine.read_bytes(), before)

    def test_lock_prevents_concurrent_runs(self):
        lib = Library(self.root)
        with lib.lock():
            with self.assertRaises(RuntimeError):
                self.run_lib({}, ["2026-11-03"])

    def test_recheck_promotes_after_correction(self):
        routes = {FILE.format("l-orizzont-2026-11-03.pdf"): (200, {}, pdf_bytes("no date here"))}
        lib, _s, _ = self.run_lib(routes, ["2026-11-03"])
        row = lib.rows["2026-11-03@l-orizzont-2026-11-03.pdf"]
        self.assertEqual(row["status"], "uncertain")
        Path(row["path"]).write_bytes(pdf_bytes(NOV3))  # e.g. a better copy placed by the user
        Library(self.root).recheck(emit=self.logs.append)
        self.assertTrue((self.root / "LOrizzont-2026-11-03.pdf").is_file())

    def test_supplement_title_preserved_in_name(self):
        self.listing = parse_listing(listing_html(("il-gwida-tas-sahha.pdf", "Il-Gwida tas-Saħħa")).decode())
        routes = {FILE.format("il-gwida-tas-sahha.pdf"): (200, {}, pdf_bytes("Gwida"))}
        lib = Library(self.root)
        groups = Library.plan(self.listing, self.patterns, [], ["supplements"], listed_only=True)
        lib.run(session_with(Router(routes)), groups, delay=1, stop=no_wait_event(), emit=self.logs.append)
        row = lib.rows["undated@il-gwida-tas-sahha.pdf"]
        self.assertEqual(row["title"], "Il-Gwida tas-Saħħa")
        self.assertEqual(row["status"], "uncertain")  # no printed date to verify: kept for review
        lib.accept("undated@il-gwida-tas-sahha.pdf")
        self.assertTrue((self.root / "Supplement-undated-Il-Gwida-tas-Saħħa.pdf").is_file())


if __name__ == "__main__":
    unittest.main()


class BothFilenameFormats(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patterns = load_patterns(path="/nonexistent")

    def run_lib(self, routes, dates, recheck=None, listing=(), pubs=("l-orizzont",)):
        router = Router(routes, fallback=(200, {}, BLOG_HTML))
        lib = Library(self.root)
        groups = Library.plan(list(listing), self.patterns, dates, list(pubs))
        summary = lib.run(session_with(router), groups, delay=1, stop=no_wait_event(), emit=lambda _l: None,
                          recheck_session=recheck, patterns=self.patterns)
        return lib, summary, router

    def test_http200_html_falls_back_to_other_format(self):
        iso, dmy = FILE.format("l-orizzont-2025-11-04.pdf"), FILE.format("l-orizzont-04-11-2025.pdf")
        routes = {iso: (200, {}, BLOG_HTML), dmy: (200, {}, pdf_bytes("It-Tlieta, 4 ta' Novembru 2025"))}
        lib, summary, router = self.run_lib(routes, ["2025-11-04"])
        self.assertEqual(router.calls, [dmy])  # closest evidence (owner example) tried first
        self.assertEqual(summary["downloaded"], 1)
        row = lib.rows["2025-11-04@l-orizzont-04-11-2025.pdf"]
        self.assertEqual((row["status"], row["source"], row["template"]),
                         ("verified", dmy, "l-orizzont-{d}-{m}-{Y}.pdf"))
        self.assertTrue((self.root / "LOrizzont-2025-11-04.pdf").is_file())

    def test_iso_preferred_for_recent_dates_and_fallback_works_both_ways(self):
        iso, dmy = FILE.format("l-orizzont-2026-10-26.pdf"), FILE.format("l-orizzont-26-10-2026.pdf")
        routes = {iso: (200, {}, BLOG_HTML), dmy: (200, {}, pdf_bytes("It-Tnejn, 26 ta' Ottubru 2026"))}
        lib, summary, router = self.run_lib(routes, ["2026-10-26"])
        self.assertEqual(router.calls, [iso, dmy])  # ISO evidence is closest; DD-MM-YYYY follows
        self.assertEqual(lib.rows["2026-10-26@l-orizzont-2026-10-26.pdf"]["status"], "missing")
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(self.patterns["l-orizzont"]["l-orizzont-{d}-{m}-{Y}.pdf"]["unlisted_confirmed"],
                         ["2026-10-26"])

    def test_all_variants_tried_before_missing_and_resume_asks_nothing(self):
        lib, summary, router = self.run_lib({}, ["2026-10-25"])
        expected = [FILE.format(f"l-orizzont-{v}.pdf") for v in (
            "2026-10-25", "25-10-2026", "10-25-2026", "20261025", "25102026", "10252026")]
        self.assertEqual(sorted(router.calls), sorted(expected))  # unpadded forms equal padded here
        self.assertEqual((summary["missing"], summary["requests"]), (1, 6))
        attempts = (self.root / ".talkmt-dl/attempts.jsonl").read_text().splitlines()
        self.assertEqual(len(attempts), 6)
        self.assertNotIn("Cookie", "".join(attempts))
        _l, _s, router = self.run_lib({}, ["2026-10-25"])
        self.assertEqual(router.calls, [])

    def test_unpadded_variant_found(self):
        url = FILE.format("l-orizzont-4-6-2025.pdf")
        lib, summary, router = self.run_lib({url: (200, {}, pdf_bytes("Il-Erbgha, 4 ta' Gunju 2025"))},
                                            ["2025-06-04"])
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(router.calls[-1], url)
        self.assertIn("2025-06-04", self.patterns["l-orizzont"]["l-orizzont-{D}-{M}-{Y}.pdf"]["unlisted_confirmed"])

    def test_mm_dd_candidate_with_other_issue_is_not_labelled_as_requested_date(self):
        # 2025-06-04 as MM-DD-YYYY is 06-04-2025, which may hold the 6 April issue.
        url = FILE.format("l-orizzont-06-04-2025.pdf")
        lib, summary, router = self.run_lib({url: (200, {}, pdf_bytes("Il-Hadd, 6 ta' April 2025"))},
                                            ["2025-06-04"])
        row = lib.rows["2025-06-04@l-orizzont-06-04-2025.pdf"]
        self.assertEqual((row["status"], row["detected_dates"]), ("wrong_date", ["2025-04-06"]))
        self.assertFalse((self.root / "LOrizzont-2025-06-04.pdf").exists())
        self.assertGreater(router.calls.index(router.calls[-1]), router.calls.index(url))  # kept looking

    def test_rate_limit_and_server_errors_do_not_trigger_more_guessing(self):
        first = FILE.format("l-orizzont-04-11-2025.pdf")
        lib, summary, router = self.run_lib({first: (503, {}, b"")}, ["2025-11-04"])
        self.assertEqual(set(router.calls), {first})  # retried with backoff, no other filenames
        self.assertEqual(summary["failed"], 1)
        from mtnews_dl.sources.talkmt.net import RateLimited
        with self.assertRaises(RateLimited):
            self.run_lib({first: (429, {"Retry-After": "7200"}, b"")}, ["2025-11-04"])

    def test_torca_both_formats_from_observed_links_only(self):
        listing = parse_listing(listing_html(("it-torca-01-11-2026.pdf", "IT-TORĊA 01-11-2026"),
                                             ("it-torca-2026-10-18.pdf", "it-torca-2026-10-18")).decode())
        learn(self.patterns, listing)
        url = FILE.format("it-torca-2026-10-11.pdf")
        lib, summary, router = self.run_lib({url: (200, {}, pdf_bytes("Il-Hadd, 11 ta' Ottubru 2026 Torca"))},
                                            ["2026-10-11"], listing=listing, pubs=("it-torca",))
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(lib.rows["2026-10-11@it-torca-2026-10-11.pdf"]["publication_check"], "confirmed")
        self.assertTrue((self.root / "ItTorca-2026-10-11.pdf").is_file())

    def test_wrong_publication_is_held(self):
        url = FILE.format("l-orizzont-04-11-2025.pdf")
        body = pdf_bytes("It-Tlieta, 4 ta' Novembru 2025 Torca Torca Torca")
        lib, _s, _r = self.run_lib({url: (200, {}, body)}, ["2025-11-04"])
        self.assertEqual(lib.rows["2025-11-04@l-orizzont-04-11-2025.pdf"]["status"], "wrong_publication")

    def test_dated_supplements_get_no_invented_variants(self):
        listing = parse_listing(listing_html(("ekonomija-2025-07-26.pdf", "EKONOMIJA 2025-07-26"),
                                             ("kurrenti-2025-10-25.pdf", "kurrenti-2025-10-25"),
                                             ("kurrenti.pdf", "KURRENTI")).decode())
        learn(self.patterns, listing)
        groups = Library.plan(listing, self.patterns, ["2025-07-26", "2025-08-02"], ["supplements"])
        self.assertEqual([c["slug"] for g in groups for c in g["candidates"]], ["ekonomija-2025-07-26.pdf"])
        listed = Library.plan(listing, self.patterns, [], ["supplements"], listed_only=True)
        self.assertEqual({c["title"] for g in listed for c in g["candidates"]},
                         {"EKONOMIJA 2025-07-26", "kurrenti-2025-10-25", "KURRENTI"})

    def test_wrong_printed_date_does_not_stop_at_filename(self):
        # 04-11-2025 could be read as April 11; only the printed date decides.
        dmy = FILE.format("l-orizzont-04-11-2025.pdf")
        routes = {dmy: (200, {}, pdf_bytes("Il-Gimgha, 11 ta' April 2025"))}
        lib, summary, _ = self.run_lib(routes, ["2025-11-04"])
        self.assertEqual(lib.rows["2025-11-04@l-orizzont-04-11-2025.pdf"]["status"], "wrong_date")
        self.assertEqual(summary["review"], 1)

    def test_login_redirect_checks_session_instead_of_marking_missing(self):
        dmy = FILE.format("l-orizzont-04-11-2025.pdf")
        checks = []

        def recheck():
            checks.append(1)
            raise AuthRequired("logged out")
        with self.assertRaises(AuthRequired):
            self.run_lib({dmy: LOGIN_302}, ["2025-11-04"], recheck=recheck)
        state = json.loads((self.root / ".talkmt-dl/state.json").read_text())
        self.assertEqual(checks, [1])
        self.assertEqual(state["2025-11-04@l-orizzont-04-11-2025.pdf"]["status"], "auth_required")

    def test_same_pdf_through_two_urls_is_saved_once(self):
        # DD-MM and MM-DD variants both return the same (wrong-date) PDF: stored once.
        data = pdf_bytes("Il-Hadd, 11 ta' Mejju 2025")
        a, b = FILE.format("l-orizzont-05-11-2025.pdf"), FILE.format("l-orizzont-11-05-2025.pdf")
        lib, summary, router = self.run_lib({a: (200, {}, data), b: (200, {}, data)}, ["2025-11-05"])
        rows = [lib.rows[f"2025-11-05@{u.split('/')[-2]}"] for u in (a, b)]
        statuses = sorted(r["status"] for r in rows)
        self.assertEqual(statuses, ["duplicate", "wrong_date"])
        pending = [p for p in (self.root / ".talkmt-dl/pending").glob("*.pdf")]
        self.assertEqual(len(pending), 1)
        self.assertEqual(summary["review"], 1)

    def test_mislabelled_listed_file_is_found_by_printed_date(self):
        # Real case: it-torca-04-09-2026.pdf is printed 4 October 2026.
        listing = parse_listing(listing_html(("it-torca-04-09-2026.pdf", "IT-TORĊA 04-09-2026")).decode())
        learn(self.patterns, listing)
        url = FILE.format("it-torca-04-09-2026.pdf")
        self.run_lib({url: (200, {}, pdf_bytes("Il-Hadd, 4 ta' Ottubru 2026"))}, ["2026-09-04"],
                     listing=listing, pubs=("it-torca",))
        _l, summary, router = self.run_lib({}, ["2026-10-04"], listing=listing, pubs=("it-torca",))
        self.assertEqual((router.calls, summary["review"]), ([], 1))
        dest = Library(self.root).accept("2026-09-04@it-torca-04-09-2026.pdf")
        self.assertEqual(dest.name, "ItTorca-2026-10-04.pdf")  # printed date, not filename date
        _l, summary, router = self.run_lib({}, ["2026-10-04"], listing=listing, pubs=("it-torca",))
        self.assertEqual((router.calls, summary["skipped"]), ([], 1))

    def test_previously_successful_url_means_no_requests(self):
        url = FILE.format("l-orizzont-04-11-2025.pdf")
        self.run_lib({url: (200, {}, pdf_bytes("It-Tlieta, 4 ta' Novembru 2025"))}, ["2025-11-04"])
        _l, summary, router = self.run_lib({}, ["2025-11-04"])
        self.assertEqual((router.calls, summary["skipped"]), ([], 1))


class MastheadDates(unittest.TestCase):
    def test_masthead_date_beats_lower_advert_date(self):
        from mtnews_dl.common.pdfcheck import dates_in
        self.assertEqual(dates_in("It-Tlieta 3 ta’ Novembru 2026 | €1.00"), ["2026-11-03"])
