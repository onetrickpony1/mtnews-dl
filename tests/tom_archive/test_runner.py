import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from pypdf import PdfReader, PdfWriter
from app import Runner, Stopped


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runner = Runner(dict(folder=str(self.root), ceiling=290, interval=5,
                                  auto_resume=False), queue.Queue(), threading.Event(), threading.Event())
        self.item = ('20211101-001', 'https://timesofmalta.com/download/20211101-001', 'https://timesofmalta.com/archive/issue?from=2021-11-01')
        self.runner.ledger.inventory('2021-11-01', [self.item])
        self.fixture = self.root / 'fixture.pdf'
        writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
        writer.write(self.fixture); writer.close()

    def tearDown(self):
        self.runner.ledger.close(); self.temp.cleanup()

    def page(self, bad=False):
        page = Mock(); page.url = self.item[2]
        page.locator.return_value.inner_text.return_value = 'Digital Archive'
        page.locator.return_value.count.return_value = 1
        download = Mock(); download.failure.return_value = None
        download.save_as.side_effect = lambda path: Path(path).write_bytes(b'<html>quota reached</html>' if bad else self.fixture.read_bytes())
        page.expect_download.return_value.__enter__ = Mock(return_value=Mock(value=download))
        page.expect_download.return_value.__exit__ = Mock(return_value=False)
        return page

    def test_success_validates_saves_and_skips_on_resume(self):
        page = self.page()
        self.assertTrue(self.runner.save_page(page, self.item))
        self.assertFalse(self.runner.save_page(page, self.item))
        self.assertEqual(self.runner.ledger.used(), 1)
        self.assertEqual(page.locator.return_value.click.call_count, 1)

    def test_html_response_pauses_and_remains_counted(self):
        page = self.page(bad=True)
        with self.assertRaisesRegex(RuntimeError, 'not a PDF'):
            self.runner.save_page(page, self.item)
        self.assertEqual(self.runner.ledger.used(), 1)
        self.assertFalse(self.runner.ledger.valid(self.item[0]))
        with self.assertRaisesRegex(RuntimeError, 'uncertain outcome'):
            self.runner.save_page(page, self.item)
        self.assertEqual(page.locator.return_value.click.call_count, 1)

    def test_merge_requires_every_listed_page(self):
        second = ('20211101-002', self.item[1].replace('001', '002'), self.item[2])
        self.runner.ledger.inventory('2021-11-01', [self.item, second])
        self.runner.save_page(self.page(), self.item)
        self.runner.assemble('2021-11-01', [self.item, second])
        output = self.root / 'editions/2021/Times-of-Malta-2021-11-01.pdf'
        self.assertFalse(output.exists())
        self.runner.assemble('2021-11-01', [self.item])
        self.assertEqual(len(PdfReader(output).pages), 1)

    def test_restart_recovers_unrecorded_chrome_download_then_merges_without_redownload(self):
        second = ('20211101-002', self.item[1].replace('001', '002'), self.item[2])
        self.runner.ledger.inventory('2021-11-01', [self.item, second])
        self.runner.ledger.reserve(self.item[0], 290)
        config = self.runner.c
        self.runner.ledger.close()
        self.runner = Runner(config, queue.Queue(), threading.Event(), threading.Event())
        page = self.page()
        page.completed_files.return_value = [{'url': self.item[1], 'filename': str(self.fixture),
                                              'endTime': '2026-11-01T00:00:00Z'}]
        self.runner.recover_pages(page, [self.item, second])
        self.assertTrue(self.runner.ledger.valid(self.item[0]))
        self.assertFalse(self.runner.ledger.uncertain(self.item[0]))
        self.assertEqual(self.runner.ledger.used(), 1)
        self.assertFalse(self.runner.save_page(page, self.item))
        page.locator.return_value.click.assert_not_called()
        self.runner.save_page(page, second)
        self.runner.assemble('2021-11-01', [self.item, second])
        output = self.root / 'editions/2021/Times-of-Malta-2021-11-01.pdf'
        self.assertEqual(len(PdfReader(output).pages), 2)
        self.assertEqual(page.locator.return_value.click.call_count, 1)
        self.assertEqual(self.runner.ledger.used(), 2)

    def test_recovery_rejects_invalid_file_and_retains_uncertain_attempt(self):
        self.runner.ledger.reserve(self.item[0], 290)
        self.fixture.write_bytes(b'%PDF-corrupted')
        page = self.page()
        page.completed_files.return_value = [{'url': self.item[1], 'filename': str(self.fixture)}]
        self.runner.recover_pages(page, [self.item])
        self.assertFalse(self.runner.ledger.valid(self.item[0]))
        self.assertTrue(self.runner.ledger.uncertain(self.item[0]))
        self.assertEqual(self.runner.ledger.used(), 1)

    def test_recovery_ignores_other_page_download(self):
        page = self.page()
        page.completed_files.return_value = [{'url': self.item[1] + '9', 'filename': str(self.fixture)}]
        self.runner.recover_pages(page, [self.item])
        self.assertFalse(self.runner.ledger.valid(self.item[0]))
        self.assertEqual(self.runner.ledger.used(), 0)

    def test_explicit_resume_after_history_check_retains_both_attempt_charges(self):
        self.runner.ledger.reserve(self.item[0], 290)
        page = self.page()
        page.completed_files.return_value = []
        self.runner.recover_pages(page, [self.item])
        self.assertTrue(self.runner.save_page(page, self.item))
        self.assertEqual(self.runner.ledger.used(), 2)
        page.locator.return_value.click.assert_called_once()

    def test_resume_does_not_duplicate_an_in_progress_download(self):
        page = self.page()
        page.completed_files.return_value = [{'url': self.item[1], 'state': 'in_progress'}]
        with self.assertRaisesRegex(RuntimeError, 'still has a download in progress'):
            self.runner.recover_pages(page, [self.item])
        page.locator.return_value.click.assert_not_called()

    def test_existing_download_link_avoids_source_navigation(self):
        page = self.page()
        page.url = self.item[2] + '&page=2'
        self.runner.save_page(page, self.item)
        page.goto.assert_not_called()
        page.locator.return_value.click.assert_called_once()

    def test_security_challenge_still_stops_without_reload_or_download(self):
        page = self.page()
        page.locator.return_value.inner_text.return_value = 'Performing security verification'
        with self.assertRaisesRegex(RuntimeError, 'Access challenge'):
            self.runner.save_page(page, self.item)
        page.goto.assert_not_called()
        page.locator.return_value.click.assert_not_called()
        self.assertEqual(self.runner.ledger.used(), 0)

    def test_stop_interrupts_wait(self):
        self.runner.stop.set()
        with self.assertRaises(Stopped):
            self.runner.wait(3600)


if __name__ == '__main__':
    unittest.main()
