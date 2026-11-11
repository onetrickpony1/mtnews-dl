import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from tests.pdfs import blank_pdf, page_count
from mtnews_dl.sources.tom_archive.runner import Runner, Stopped


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runner = Runner(dict(folder=str(self.root), ceiling=290, interval=5,
                                  usage_db=str(self.root / 'account-usage.sqlite3'),
                                  auto_resume=False), lambda message: None, threading.Event())
        self.item = ('20211101-001', 'https://timesofmalta.com/download/20211101-001', 'https://timesofmalta.com/archive/issue?from=2021-11-01')
        self.runner.ledger.inventory('2021-11-01', [self.item])
        self.fixture = self.root / 'fixture.pdf'
        blank_pdf(self.fixture)

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

    def test_site_limit_notice_stops_counted_but_not_uncertain(self):
        from mtnews_dl.sources.tom_archive.runner import SiteLimit
        page = self.page()
        page.locator.return_value.click.side_effect = RuntimeError(
            'SITE_LIMIT: the site says "Download limit exceeded for today". No PDF was served.')
        with self.assertRaisesRegex(SiteLimit, 'exceeded for today'):
            self.runner.save_page(page, self.item)
        self.assertEqual(self.runner.ledger.used(), 1)
        self.assertFalse(self.runner.ledger.uncertain(self.item[0]))
        page.locator.return_value.click.side_effect = None
        self.assertTrue(self.runner.save_page(self.page(), self.item))   # tomorrow: no block

    def test_merge_requires_every_listed_page(self):
        second = ('20211101-002', self.item[1].replace('001', '002'), self.item[2])
        self.runner.ledger.inventory('2021-11-01', [self.item, second])
        self.runner.save_page(self.page(), self.item)
        self.runner.assemble('2021-11-01', [self.item, second])
        output = self.root / 'editions/2021/Times-of-Malta-2021-11-01.pdf'
        self.assertFalse(output.exists())
        self.runner.assemble('2021-11-01', [self.item])
        self.assertEqual(page_count(output), 1)

    def test_restart_recovers_unrecorded_chrome_download_then_merges_without_redownload(self):
        second = ('20211101-002', self.item[1].replace('001', '002'), self.item[2])
        self.runner.ledger.inventory('2021-11-01', [self.item, second])
        self.runner.ledger.reserve(self.item[0], 290)
        config = self.runner.c
        self.runner.ledger.close()
        self.runner = Runner(config, lambda message: None, threading.Event())
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
        self.assertEqual(page_count(output), 2)
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


class SignInTests(unittest.TestCase):
    """tom-dl connect: a sign-in done in any tab is noticed by reloading the archive tab."""

    class Page:
        def __init__(self, signed_in_after):
            self.url, self.loads, self.after = '', 0, signed_in_after

        def goto(self, url, **kwargs):
            self.loads += 1
            self.url = url if self.loads > self.after else url + '/restricted-access'
            if self.loads <= self.after:
                raise RuntimeError('Chrome reached restricted-access')

        def title(self):
            return 'Times of Malta'

        def locator(self, selector):
            return Mock(inner_text=Mock(return_value='Digital Archive'))

        def get_by_role(self, *args, **kwargs):
            return Mock()

    def runner(self, temp, messages):
        clock = [0.0]
        runner = Runner(dict(folder=temp, interval=0, ceiling=290, auto_resume=False,
                             usage_db=str(Path(temp) / 'usage.sqlite3')),
                        messages.append, threading.Event())
        runner.wait = lambda seconds: clock.__setitem__(0, clock[0] + max(0, seconds))
        return runner, clock

    def test_reloads_subscription_page_until_signed_in(self):
        with tempfile.TemporaryDirectory() as temp:
            messages = []
            runner, clock = self.runner(temp, messages)
            page = self.Page(signed_in_after=2)
            import mtnews_dl.sources.tom_archive.runner as module
            real = module.time.monotonic
            module.time.monotonic = lambda: clock[0]
            try:
                self.assertTrue(runner.wait_for_sign_in(page, recheck=20))
            finally:
                module.time.monotonic = real
                runner.ledger.close()
            self.assertEqual(page.loads, 3)
            self.assertEqual(sum('click "Log in"' in m for m in messages), 1)

    def test_leaves_a_tab_the_user_moved_to_the_login_form(self):
        with tempfile.TemporaryDirectory() as temp:
            messages = []
            runner, clock = self.runner(temp, messages)
            page = self.Page(signed_in_after=99)
            original = page.goto

            def goto(url, **kwargs):
                try:
                    original(url)
                finally:
                    page.url = 'https://timesofmalta.com/login'   # user clicked "Log in"
            page.goto = goto
            import mtnews_dl.sources.tom_archive.runner as module
            real = module.time.monotonic
            module.time.monotonic = lambda: clock[0]
            try:
                with self.assertRaises(RuntimeError):
                    runner.wait_for_sign_in(page, timeout=120, recheck=20)
            finally:
                module.time.monotonic = real
                runner.ledger.close()
            self.assertEqual(page.loads, 1)
