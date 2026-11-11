import tempfile
import unittest
from pathlib import Path
from mtnews_dl.sources.tom_archive.ledger import Ledger, WINDOW


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.usage = Path(self.temp.name) / 'account' / 'usage.sqlite3'
        self.ledger = Ledger(self.temp.name, self.usage)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_cap_survives_restart_and_counts_uncertain_attempts(self):
        self.ledger.manual(289, now=100000)
        self.ledger.reserve('20211101-001', 290, now=100001)
        self.ledger.close()
        self.ledger = Ledger(self.temp.name, self.usage)
        with self.assertRaises(RuntimeError):
            self.ledger.reserve('20211101-002', 290, now=100002)
        self.assertEqual(self.ledger.used(100002), 290)

    def test_rolling_window_releases_only_old_usage(self):
        self.ledger.manual(299, now=100000)
        self.ledger.reserve('p', 300, now=100010)
        self.assertEqual(self.ledger.used(100000 + WINDOW), 1)
        self.assertEqual(self.ledger.used(100010 + WINDOW), 0)

    def test_inventory_refresh_preserves_download_and_detects_corruption(self):
        item = ('20211101-001', 'https://timesofmalta.com/download/20211101-001', 'listing')
        self.ledger.inventory('2021-11-01', [item])
        file = Path(self.temp.name) / 'sample.pdf'
        file.write_bytes(b'example bytes')
        usage = self.ledger.reserve(item[0], 290)
        self.ledger.complete(item[0], file, usage)
        self.ledger.inventory('2021-11-01', [item])
        self.assertTrue(self.ledger.valid(item[0]))
        file.write_bytes(b'corrupt')
        self.assertFalse(self.ledger.valid(item[0]))

    def test_ceiling_cannot_exceed_1000(self):
        with self.assertRaises(ValueError):
            self.ledger.reserve('p', 1001)

    def test_1000_ceiling_preserves_accounting_and_stops_at_limit(self):
        self.ledger.manual(999)
        self.ledger.reserve('last', 1000)
        self.assertEqual(self.ledger.used(), 1000)
        with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
            self.ledger.reserve('extra', 1000)
        self.assertEqual(self.ledger.used(), 1000)

    def test_uncertain_attempt_is_not_marked_saved(self):
        self.ledger.reserve('p', 290)
        self.assertTrue(self.ledger.uncertain('p'))
        self.assertEqual(self.ledger.stats()['saved_pages'], 0)


if __name__ == '__main__':
    unittest.main()


class SharedAllowanceTests(unittest.TestCase):
    """The allowance is per account: every library folder draws on one count."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.usage = base / 'account' / 'usage.sqlite3'
        self.a = Ledger(base / '2022', self.usage)
        self.b = Ledger(base / '2023', self.usage)

    def tearDown(self):
        self.a.close()
        self.b.close()
        self.temp.cleanup()

    def test_two_libraries_share_one_count(self):
        self.a.manual(300, now=100000)
        self.b.reserve('20230101-001', 301, now=100001)
        with self.assertRaises(RuntimeError):
            self.a.reserve('20220101-001', 301, now=100002)
        self.assertEqual(self.a.used(100002), 301)
        self.assertEqual(self.b.used(100002), 301)

    def test_usage_stored_in_a_library_moves_to_the_account_once(self):
        import sqlite3
        old = Path(self.temp.name) / 'imported'
        old.mkdir()
        db = sqlite3.connect(old / 'progress.sqlite3')
        db.execute('CREATE TABLE usage (id INTEGER PRIMARY KEY, ts REAL NOT NULL, '
                   'pages INTEGER NOT NULL, page_id TEXT, status TEXT NOT NULL)')
        db.execute("INSERT INTO usage(ts,pages,page_id,status) VALUES (100000, 120, NULL, 'manual')")
        db.commit()
        db.close()
        for _ in range(2):   # opening again must not count it twice
            ledger = Ledger(old, self.usage)
            ledger.close()
        self.assertEqual(self.a.used(100001), 120)
