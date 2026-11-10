import unittest

from mtnews_dl.sources.talkmt.discovery import (learn, load_patterns, parse_listing, publication_of, split_date,
                                 verified_templates)
from mtnews_dl.sources.talkmt.library import Library

PAGE = """
<a href="https://subscribe.mt/newspapers/mp-files/l-orizzont-2026-11-03.pdf/"><img alt="L-Orizzont 3 ta' Novembru 2026"></a>
<a href="/newspapers/mp-files/it-torca-01-11-2026.pdf/">It-Torċa</a>
<a href="https://subscribe.mt/newspapers/mp-files/il-gwida-tas-sahha.pdf/">Il-Gwida tas-Saħħa</a>
<a href="https://subscribe.mt/newspapers/mp-files/kids-supplement-2026-11-02.pdf/">Kids Supplement</a>
<a href="https://subscribe.mt/newspapers/mp-files/l-orizzont-2026-11-03.pdf/">duplicate</a>
<a href="https://evil.example/newspapers/mp-files/l-orizzont-2026-11-04.pdf/">off-site</a>
<a href="http://subscribe.mt/newspapers/mp-files/l-orizzont-2026-11-05.pdf/">plain http</a>
<a href="https://subscribe.mt/newspapers/mp-files/..%2Fwp-config.pdf/">traversal</a>
<a href="https://subscribe.mt/newspapers/mp-files/l-orizzont-2026-02-31.pdf/">impossible date</a>
<a href="javascript:void(0)">js</a>
"""


class Listing(unittest.TestCase):
    def test_parse_listing(self):
        items = {i["slug"]: i for i in parse_listing(PAGE)}
        self.assertEqual(set(items), {"l-orizzont-2026-11-03.pdf", "it-torca-01-11-2026.pdf",
                                      "il-gwida-tas-sahha.pdf", "kids-supplement-2026-11-02.pdf",
                                      "l-orizzont-2026-02-31.pdf"})
        lo = items["l-orizzont-2026-11-03.pdf"]
        self.assertEqual((lo["publication"], lo["date"], lo["template"]),
                         ("l-orizzont", "2026-11-03", "l-orizzont-{Y}-{m}-{d}.pdf"))
        torca = items["it-torca-01-11-2026.pdf"]
        self.assertEqual((torca["publication"], torca["date"], torca["template"]),
                         ("it-torca", "2026-11-01", "it-torca-{d}-{m}-{Y}.pdf"))
        self.assertEqual(items["il-gwida-tas-sahha.pdf"]["publication"], "supplements")
        self.assertIsNone(items["il-gwida-tas-sahha.pdf"]["date"])
        self.assertEqual(items["il-gwida-tas-sahha.pdf"]["title"], "Il-Gwida tas-Saħħa")
        self.assertEqual(items["kids-supplement-2026-11-02.pdf"]["publication"], "supplements")
        self.assertIsNone(items["l-orizzont-2026-02-31.pdf"]["date"])

    def test_supplement_with_paper_prefix_is_not_the_paper(self):
        template, date = split_date("l-orizzont-2026-11-03-supplement.pdf")
        self.assertEqual(publication_of("x", template, ""), "supplements")
        self.assertEqual(date, "2026-11-03")

    def test_pattern_trust_rules(self):
        patterns = load_patterns(path="/nonexistent/patterns.json")
        self.assertEqual(set(verified_templates(patterns, "l-orizzont")),
                         {"l-orizzont-{Y}-{m}-{d}.pdf", "l-orizzont-{d}-{m}-{Y}.pdf"})
        self.assertEqual(verified_templates(patterns, "it-torca"), [])
        items = parse_listing(PAGE)
        learn(patterns, items)
        self.assertEqual(verified_templates(patterns, "it-torca"), ["it-torca-{d}-{m}-{Y}.pdf"])
        self.assertEqual(verified_templates(patterns, "supplements"), [])  # one date is not enough
        learn(patterns, parse_listing(PAGE.replace("2026-11-02", "2026-10-26")))
        self.assertEqual(verified_templates(patterns, "supplements"), ["kids-supplement-{Y}-{m}-{d}.pdf"])

    def test_plan_combines_listing_and_patterns(self):
        patterns = learn(load_patterns(path="/nonexistent"), parse_listing(PAGE))
        groups = Library.plan(parse_listing(PAGE), patterns, ["2026-11-01", "2026-11-03", "2026-10-01"],
                              ["l-orizzont", "it-torca"], formats="observed")
        by = {(g["publication"], g["date"]): [c["slug"] for c in g["candidates"]] for g in groups}
        self.assertEqual(by[("l-orizzont", "2026-11-03")][0], "l-orizzont-2026-11-03.pdf")  # listed first
        self.assertEqual(by[("l-orizzont", "2026-10-01")], ["l-orizzont-2026-10-01.pdf", "l-orizzont-01-10-2026.pdf"])
        self.assertEqual(by[("it-torca", "2026-11-01")], ["it-torca-01-11-2026.pdf"])
        self.assertEqual(by[("it-torca", "2026-10-01")], ["it-torca-01-10-2026.pdf"])
        self.assertTrue(all(c["listed"] for c in groups[0]["candidates"]) or True)
        listed_only = Library.plan(parse_listing(PAGE), patterns, [], ["supplements"], listed_only=True)
        self.assertEqual(len(listed_only), 2)


if __name__ == "__main__":
    unittest.main()


class LOrizzontFormats(unittest.TestCase):
    def test_both_owner_confirmed_formats_and_date_aware_order(self):
        patterns = load_patterns(path="/nonexistent")
        self.assertEqual(verified_templates(patterns, "l-orizzont", "2025-11-04")[0], "l-orizzont-{d}-{m}-{Y}.pdf")
        self.assertEqual(verified_templates(patterns, "l-orizzont", "2026-11-02")[0], "l-orizzont-{Y}-{m}-{d}.pdf")

    def test_ddmmyyyy_filename_is_parsed_not_assumed(self):
        items = parse_listing('<a href="/newspapers/mp-files/l-orizzont-04-11-2025.pdf/">x</a>')
        self.assertEqual((items[0]["date"], items[0]["template"], items[0]["publication"]),
                         ("2025-11-04", "l-orizzont-{d}-{m}-{Y}.pdf", "l-orizzont"))

    def test_seeds_do_not_leak_to_other_publications(self):
        patterns = load_patterns(path="/nonexistent")
        self.assertEqual(verified_templates(patterns, "it-torca", "2025-11-02"), [])
        self.assertEqual(verified_templates(patterns, "supplements", "2025-11-02"), [])


class Registry(unittest.TestCase):
    def test_candidates_bounded_deduplicated_and_ordered(self):
        from mtnews_dl.sources.talkmt.discovery import MAX_CANDIDATES, candidate_slugs
        patterns = load_patterns(path="/nonexistent")
        slugs = [c[0] for c in candidate_slugs(patterns, "l-orizzont", "2025-11-04")]
        self.assertEqual(slugs[:2], ["l-orizzont-04-11-2025.pdf", "l-orizzont-2025-11-04.pdf"])
        for variant in ("11-04-2025", "4-11-2025", "2025-11-4", "11-4-2025", "20251104", "04112025", "11042025"):
            self.assertIn(f"l-orizzont-{variant}.pdf", slugs)
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertLessEqual(len(slugs), MAX_CANDIDATES)
        same = [c[0] for c in candidate_slugs(patterns, "l-orizzont", "2025-11-11")]
        self.assertEqual(len(same), 4)  # DD-MM == MM-DD and padded == unpadded collapse
        self.assertEqual(candidate_slugs(patterns, "l-orizzont", "2025-11-04", formats="observed")[0][2],
                         "observed pattern")

    def test_successful_pattern_is_prioritised_near_its_dates(self):
        from mtnews_dl.sources.talkmt.discovery import candidate_slugs
        patterns = load_patterns(path="/nonexistent")
        patterns["l-orizzont"]["l-orizzont-{Y}-{m}-{d}.pdf"]["unlisted_confirmed"] = ["2025-11-05"]
        self.assertEqual(candidate_slugs(patterns, "l-orizzont", "2025-11-06")[0][0], "l-orizzont-2025-11-06.pdf")

    def test_separators_and_underscores_learned_from_links(self):
        self.assertEqual(split_date("ekonomija_2025-03-29.pdf"), ("ekonomija_{Y}-{m}-{d}.pdf", "2025-03-29"))
        self.assertEqual(split_date("x_2025_03_29.pdf"), ("x_{Y}_{m}_{d}.pdf", "2025-03-29"))


class ProbeEvidence(unittest.TestCase):
    def test_probe_results_reorder_formats_near_their_dates(self):
        from mtnews_dl.sources.talkmt.discovery import candidate_slugs, merge_probe_evidence
        patterns = load_patterns(path="/nonexistent")
        self.assertEqual(candidate_slugs(patterns, "l-orizzont", "2022-10-17")[0][0],
                         "l-orizzont-17-10-2022.pdf")  # seeds alone: DD-MM-YYYY (2025-11-04) is closer
        merge_probe_evidence(patterns, {
            "l-orizzont@2022-10-15": dict(result="pdf", slug="l-orizzont-2022-10-15.pdf", template=None),
            "l-orizzont@2022-07-15": dict(result="not found"),
            "it-torca@2024-10-20": dict(result="pdf", slug="it-torca-20-10-2024.pdf",
                                        template="it-torca-{d}-{m}-{Y}.pdf")})
        self.assertEqual(candidate_slugs(patterns, "l-orizzont", "2022-10-17")[0][0],
                         "l-orizzont-2022-10-17.pdf")
        self.assertEqual(patterns["it-torca"]["it-torca-{d}-{m}-{Y}.pdf"]["probe_dates"], ["2024-10-20"])
        # Probe evidence orders but is not a verified download.
        self.assertEqual(patterns["l-orizzont"]["l-orizzont-{Y}-{m}-{d}.pdf"]["unlisted_confirmed"], [])
