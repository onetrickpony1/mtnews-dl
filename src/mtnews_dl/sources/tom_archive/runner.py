"""Times of Malta digital archive (timesofmalta.com/archive), driven through the user's
signed-in Chrome via the tom-dl extension.

The archive serves one PDF per page, with a daily page allowance for premium accounts (the reset time is unknown). Every click is reserved in a local rolling
24-hour ledger *before* it happens, uncertain attempts stay counted, saved pages are checked by
SHA-256, and a whole issue is only merged once every listed page is verified.
"""
import csv
import datetime as dt
import hashlib
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ...common.pdfcheck import check_structure, merge
from .ledger import Ledger

BASE = 'https://timesofmalta.com'
DEFAULT_CEILING = 302   # local pages per rolling 24 hours (owner's setting)


def listing_key(url):
    """Treat an omitted page number and page=1 as the same archive listing."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.path == '/archive/issue' and set(query) <= {'from', 'page'} and len(query.get('from', [])) == 1:
        page_number = query.get('page', ['1'])
        if len(page_number) == 1 and page_number[0].isdigit():
            return (parsed.scheme, parsed.netloc, parsed.path, query['from'][0], int(page_number[0]))
    return url


class Stopped(Exception):
    pass


class IssueUnavailable(RuntimeError):
    pass


class IssueDateRedirect(IssueUnavailable):
    pass


class Runner:
    """config: folder (archive state root), interval (s), ceiling (pages per rolling 24 h),
    auto_resume (wait for the local allowance instead of stopping), usage_db (optional:
    the per-user allowance database; tests point it elsewhere)."""

    def __init__(self, config, emit, stop):
        self.c, self.emit, self.stop = config, emit, stop
        self.root = Path(config['folder']).resolve()
        self.ledger = Ledger(self.root, config.get('usage_db'))
        self.last_request = 0
        self.recovery_checked = set()
        self.pages_requested = 0

    def log(self, message):
        self.emit(message)

    def check(self):
        if self.stop.is_set():
            raise Stopped()

    def wait(self, seconds):
        if self.stop.wait(max(0, seconds)):
            raise Stopped()

    def throttle(self):
        self.check()
        self.wait(self.c['interval'] - (time.monotonic() - self.last_request))
        self.last_request = time.monotonic()

    def navigate(self, page, url, interactive=False):
        self.throttle()
        response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
        if response and response.status >= 400:
            retry = response.headers.get('retry-after', '')
            if interactive:
                self.log(f'Browser received HTTP {response.status}. Leaving it open for you to inspect and complete sign-in or site verification. No automatic retry or downloads.')
                return
            raise RuntimeError(f'Server returned HTTP {response.status}. Paused. Retry-After: {retry or "not supplied"}')

    def wait_for_sign_in(self, page, timeout=600, challenge_timeout=60, recheck=20):
        """Open the archive in Chrome and wait until it is usable (the user signs in there).

        While the archive tab still shows the subscription page it is reloaded every `recheck`
        seconds, so a sign-in made in any tab is noticed; a tab the user has moved on (to the
        login form, say) is left alone. Page views cost nothing; no PDF is requested."""
        self.log('Opening the Times of Malta archive in the browser window (Ctrl+C to stop).')
        deadline = time.monotonic() + timeout
        challenge_since = None
        told = False
        opened = None
        while time.monotonic() < deadline:
            self.check()
            if opened is None or ('/restricted-access' in page.url
                                  and time.monotonic() - opened >= recheck):
                opened = time.monotonic()
                try:
                    self.navigate(page, BASE + '/archive', interactive=True)
                except Exception:
                    pass   # e.g. redirected to the subscription page; the checks below decide
            try:
                title = page.title()
                body = page.locator('body').inner_text(timeout=1000)
            except Exception:
                title, body = '', ''
            if re.search(r'just a moment|performing security verification|verify you are human', title + '\n' + body, re.I):
                challenge_since = challenge_since or time.monotonic()
                if time.monotonic() - challenge_since >= challenge_timeout:
                    raise RuntimeError('Site verification did not complete in Chrome. Close the archive tab, '
                                       'open timesofmalta.com/archive yourself, then run tom-dl connect again.')
            else:
                challenge_since = None
                try:
                    self.verify_archive(page)
                except Exception:
                    if not told and ('/restricted-access' in page.url or '/login' in page.url):
                        told = True
                        self.log('Not signed in yet. In the browser window, click "Log in" on the Times of '
                                 'Malta page and sign in with your premium account. tom-dl notices by '
                                 f'itself (it rechecks every {recheck} s); leave this running.')
                else:
                    self.log('Archive access verified in your Chrome profile.')
                    return True
            self.wait(3)
        raise RuntimeError('The archive did not become available. Sign in at timesofmalta.com in Chrome '
                           '(premium subscription needed), then run tom-dl connect again.')

    def verify_archive(self, page):
        if '/restricted-access' in page.url or '/login' in page.url:
            raise RuntimeError('Premium session is not active in Chrome. Run tom-dl connect, sign in, then resume.')
        text = page.locator('body').inner_text(timeout=15000)
        if re.search(r'verify you are human|performing security verification|just a moment|daily.{0,30}limit.{0,30}reached|download limit.{0,30}(reached|exceeded)', text, re.I):
            raise RuntimeError('Access challenge or server quota notice detected. Paused for review.')
        page.get_by_role('heading', name='Digital Archive', exact=True).wait_for(timeout=15000)

    def list_issue(self, page, date):
        first = f'{BASE}/archive/issue?from={date}'
        pending, seen, found, previews = [first], set(), {}, set()
        while pending:
            self.check()
            url = pending.pop(0)
            key = listing_key(url)
            if key in seen:
                continue
            if len(seen) >= 100:
                raise RuntimeError('Unexpected issue pagination; stopped for review.')
            seen.add(key)
            self.log(f'Checking issue {date}: {url}')
            if listing_key(page.url) != key:
                self.navigate(page, url)
            self.verify_archive(page)
            reached = urlparse(page.url)
            returned_date = parse_qs(reached.query).get('from', [None])[0]
            if reached.path == '/archive/issue' and returned_date and returned_date != date:
                with self.ledger.db:
                    self.ledger.db.execute('INSERT OR REPLACE INTO issues VALUES (?,?,?)',
                                           (date, 'redirected_to_' + returned_date, 0))
                raise IssueDateRedirect(f'{date}: the archive redirected this date to {returned_date}. '
                                        'This issue is not currently available at the requested archive date; no substitute pages downloaded. Try this date again later.')
            links = page.locator('a[href]').evaluate_all('(els) => els.map(e => ({text:e.textContent.trim(), href:e.href}))')
            for link in links:
                parsed = urlparse(link['href'])
                preview = re.fullmatch(r'/(\d{8}-\d+)\.jpg', parsed.path)
                if parsed.netloc == 'cdn-archives.timesofmalta.com' and preview and preview.group(1).startswith(date.replace('-', '') + '-'):
                    previews.add(preview.group(1))
                if parsed.netloc != 'timesofmalta.com':
                    continue
                match = re.fullmatch(r'/download/(\d{8}-\d+)', parsed.path)
                if match and link['text'] == 'Download':
                    page_id = match.group(1)
                    if not page_id.startswith(date.replace('-', '') + '-'):
                        raise RuntimeError(f'Requested issue {date}, but the archive returned page {page_id} at {page.url}. No PDFs downloaded for this issue; stopped for review.')
                    found.setdefault(page_id, (page_id, link['href'], page.url))
                query = parse_qs(parsed.query)
                if parsed.path == '/archive/issue' and query.get('from') == [date] and 'page' in query:
                    if listing_key(link['href']) not in seen and all(listing_key(link['href']) != listing_key(p) for p in pending):
                        pending.append(link['href'])
        result = sorted(found.values(), key=lambda item: int(item[0].split('-')[1]))
        self.ledger.inventory(date, result)
        missing = previews - set(found)
        if missing:
            with self.ledger.db:
                self.ledger.db.execute('UPDATE issues SET status=?,count=? WHERE date=?',
                                       ('present_downloads_missing', len(previews | set(found)), date))
            raise IssueUnavailable(f'{date}: the issue is present, but {len(missing)} listed page(s) have no download link. No complete issue can be downloaded. Missing: {", ".join(sorted(missing))}. No PDF requests made for this issue.')
        if not result:
            with self.ledger.db:
                self.ledger.db.execute('UPDATE issues SET status=? WHERE date=?', ('not_listed', date))
            self.log(f'{date}: no issue pages are listed in the archive. This may be a non-publication day, an archive gap, or a changed page layout.')
        else:
            self.log(f'{date}: {len(result)} downloadable pages listed.')
        return result

    def budget(self):
        while self.ledger.used() >= self.c['ceiling']:
            if not self.c['auto_resume']:
                raise RuntimeError('Local rolling-24-hour allowance reached. Progress saved.')
            delay = max(1, self.ledger.next_release() - time.time())
            self.log(f'Waiting for local allowance to become available (about {delay / 3600:.1f} hours). Stop remains available.')
            self.wait(min(delay, 60))

    def recover_pages(self, page, items):
        missing = [item for item in items if not self.ledger.valid(item[0])]
        if not missing:
            self.log(f'All {len(items)} pages already verified locally; no PDF downloads needed.')
            return
        completed = page.completed_files([item[1] for item in missing])
        active = [entry['url'] for entry in completed if entry.get('state') == 'in_progress']
        if active:
            raise RuntimeError('Chrome still has a download in progress for this issue. Finish or cancel it in Chrome, then resume; no duplicate request started.')
        recovered = 0
        for page_id, url, _ in missing:
            self.check()
            day = f'{page_id[:4]}-{page_id[4:6]}-{page_id[6:8]}'
            dest = self.root / 'pages' / page_id[:4] / day / f'{page_id}.pdf'
            known = self.ledger.db.execute('SELECT sha256 FROM pages WHERE id=?', (page_id,)).fetchone()[0]
            candidates = [(dest, None)] if dest.is_file() else []
            for entry in completed:
                if entry['url'] == url:
                    try:
                        stamp = dt.datetime.fromisoformat(entry['endTime'].replace('Z', '+00:00')).timestamp()
                    except (KeyError, ValueError, TypeError):
                        stamp = None
                    candidates.append((Path(entry['filename']), stamp))
            for source, stamp in candidates:
                try:
                    if not source.is_file():
                        continue
                    data = source.read_bytes()
                    if not data.startswith(b'%PDF-'):
                        continue
                    if known and hashlib.sha256(data).hexdigest() != known:
                        continue
                    if check_structure(source) != 1:
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if source.resolve() != dest.resolve():
                        temporary = dest.with_suffix('.recover.part')
                        shutil.copyfile(source, temporary)
                        os.replace(temporary, dest)
                    self.ledger.recover(page_id, dest, stamp)
                    recovered += 1
                    self.log(f'Recovered {page_id} from an existing PDF; no download request.')
                    break
                except (OSError, ValueError) as exc:
                    self.log(f'Could not reuse {page_id}: {exc}')
        self.recovery_checked.update(item[0] for item in missing)
        saved = sum(self.ledger.valid(item[0]) for item in items)
        self.log(f'Resume check: {saved}/{len(items)} pages verified ({recovered} recovered); {len(items) - saved} remaining.')

    def save_page(self, page, item):
        page_id, url, source = item
        if self.ledger.valid(page_id):
            return False
        if self.ledger.uncertain(page_id) and page_id not in self.recovery_checked:
            raise RuntimeError(f'{page_id}: an earlier attempt has an uncertain outcome. It remains counted for 24 hours; no automatic repeat.')
        if self.ledger.uncertain(page_id):
            self.log(f'{page_id}: no completed or active Chrome download found during resume. Retrying once for this run; the earlier attempt stays counted in addition to this request.')
        self.budget()
        locator = page.locator(f'a[href="{url}"]')
        # Read the current DOM first. A listing can contain the same download
        # control even when its URL differs from the inventoried source.
        count = locator.count()
        if count != 1 and listing_key(page.url) != listing_key(source):
            self.navigate(page, source)
            locator = page.locator(f'a[href="{url}"]')
            count = locator.count()
        self.verify_archive(page)
        if count != 1:
            raise RuntimeError('Download control changed. Stopped instead of guessing.')
        self.throttle()
        usage_id = self.ledger.reserve(page_id, self.c['ceiling'])
        day = f'{page_id[:4]}-{page_id[4:6]}-{page_id[6:8]}'
        dest = self.root / 'pages' / page_id[:4] / day / f'{page_id}.pdf'
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_suffix('.part')
        try:
            with page.expect_download(timeout=45000) as pending:
                locator.click(timeout=15000)
            download = pending.value
            failure = download.failure()
            if failure:
                raise RuntimeError(failure)
            download.save_as(temporary)
            with temporary.open('rb') as f:
                if f.read(5) != b'%PDF-':
                    raise RuntimeError('Response is not a PDF.')
            try:
                pages = check_structure(temporary)
            except ValueError as exc:
                raise RuntimeError(f'Unreadable PDF ({exc}); review before proceeding.') from None
            if pages != 1:
                raise RuntimeError('Unexpected PDF page count; review before proceeding.')
            os.replace(temporary, dest)
            self.ledger.complete(page_id, dest, usage_id)
            self.pages_requested += 1
            self.log(f'Saved {page_id} ({dest.stat().st_size:,} bytes).')
            return True
        except Exception as exc:
            raise RuntimeError(f'{page_id}: this page is listed, but its download failed: {exc}. The issue remains incomplete; saved pages are retained. Attempt remains counted; stopped to avoid duplicate charges.') from exc

    def assemble(self, date, items):
        """Merge a complete issue into editions/YYYY/; returns its path, or None if incomplete."""
        if not items or not all(self.ledger.valid(item[0]) for item in items):
            return None
        dest = self.root / 'editions' / date[:4] / f'Times-of-Malta-{date}.pdf'
        sources = []
        for page_id, _, _ in items:
            row = self.ledger.db.execute('SELECT path FROM pages WHERE id=?', (page_id,)).fetchone()
            sources.append(self.root / row[0])
        merge(sources, dest)
        self.log(f'Assembled complete issue {date}: {len(items)} pages.')
        return dest

    def export(self):
        with (self.root / 'index.csv').open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['page_id', 'issue_date', 'download_url', 'listing_url', 'local_path', 'sha256', 'bytes'])
            writer.writerows(self.ledger.db.execute('SELECT * FROM pages ORDER BY id'))
        with (self.root / 'issues.csv').open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['date', 'status', 'listed_pages'])
            writer.writerows(self.ledger.db.execute('SELECT * FROM issues ORDER BY date'))

    def issue(self, page, date, *, page_limit=None):
        """Fetch one issue: list its pages, reuse saved ones, download the rest, merge.

        Returns the merged PDF path; None when the archive lists no pages for the date.
        Raises IssueUnavailable/IssueDateRedirect, PageLimit, Stopped or RuntimeError (pause).
        """
        try:
            items = self.list_issue(page, date)
            if not items:
                return None
            self.recover_pages(page, items)
            for item in items:
                if self.ledger.valid(item[0]):
                    continue
                if page_limit is not None and self.pages_requested >= page_limit:
                    raise PageLimit(f'Page limit for this run reached ({page_limit}); saved pages are kept. '
                                    'Run the same command again to continue.')
                self.save_page(page, item)
            return self.assemble(date, items)
        finally:
            self.export()

    def close(self):
        self.export()
        self.ledger.close()


class PageLimit(RuntimeError):
    pass
