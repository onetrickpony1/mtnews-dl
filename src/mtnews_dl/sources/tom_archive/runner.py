#!/usr/bin/env python3
"""Times of Malta archive GUI. Uses the signed-in Chrome profile through a companion extension."""
import csv
import hashlib
import shutil
import datetime as dt
import os
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import parse_qs, urlparse

from core import Ledger

BASE = 'https://timesofmalta.com'
HERE = Path(__file__).resolve().parent


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
    def __init__(self, config, events, stop, finish_login, browser_observer=None):
        self.c, self.events, self.stop, self.finish_login = config, events, stop, finish_login
        self.root = Path(config['folder']).resolve()
        self.ledger = Ledger(self.root)
        self.last_request = 0
        self.recovery_checked = set()
        self.browser_observer = browser_observer

    def log(self, message):
        self.events.put(('log', message))

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

    def interactive_session(self, page, mode, initial_url=None, challenge_timeout=60):
        self.log('Browser ready. Complete sign-in or inspect the controls, then click Finish browser session. Stop disconnects the app; Chrome stays open.')
        try:
            self.navigate(page, initial_url or BASE + ('/archive' if mode == 'login' else '/epaper'), interactive=True)
        except Exception as exc:
            self.log(f'Navigation did not finish: {exc}. Browser remains open for manual inspection.')
        challenge_since = None
        next_check = 0
        while not page.is_closed():
            self.check()
            now = time.monotonic()
            if now >= next_check:
                next_check = now + 1
                try:
                    title = page.title()
                    body = page.locator('body').inner_text(timeout=1000)
                except Exception:
                    # A challenge can navigate while we inspect it. Preserve the
                    # timer across these transient navigation errors.
                    title, body = '', ''
                challenged = re.search(r'just a moment|performing security verification|verify you are human', title + '\n' + body, re.I)
                if challenged:
                    if challenge_since is None:
                        challenge_since = now
                        self.log('Site verification detected. The site may refresh itself; the app is not retrying. It will stop if verification remains unresolved for 60 seconds.')
                    if now - challenge_since >= challenge_timeout:
                        ray = re.search(r'Ray ID:\s*([a-zA-Z0-9]+)', body)
                        detail = f' Ray ID: {ray.group(1)}.' if ray else ''
                        raise RuntimeError('Site verification did not complete. The app is disconnecting. Close the archive tab to stop the site’s refresh loop.' + detail + ' Chrome archive access is blocked; no newspaper downloads started in this sign-in session.')
                elif body:
                    challenge_since = None
            if self.finish_login.is_set():
                self.finish_login.clear()
                if mode == 'inspect':
                    return
                try:
                    # Do not reload a successfully opened archive: that can trigger
                    # another challenge before the existing session is checked.
                    if urlparse(page.url).path != '/archive':
                        self.navigate(page, BASE + '/archive', interactive=True)
                    self.verify_archive(page)
                except Exception as exc:
                    self.log(f'Sign-in is not verified yet: {exc}. Browser remains open; complete access and click Finish again, or Stop.')
                else:
                    self.log('Archive access verified in your Chrome profile.')
                    return
            page.wait_for_timeout(250)
        self.log('Browser closed. Login state retained, but archive access was not verified.')

    def verify_archive(self, page):
        if '/restricted-access' in page.url or '/login' in page.url:
            raise RuntimeError('Premium session is not active. Use Sign in, then resume.')
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
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
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
                    reader = PdfReader(source)
                    if reader.is_encrypted or len(reader.pages) != 1:
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
                except (OSError, ValueError, PdfReadError) as exc:
                    self.log(f'Could not reuse {page_id}: {exc}')
        self.recovery_checked.update(item[0] for item in missing)
        saved = sum(self.ledger.valid(item[0]) for item in items)
        self.log(f'Resume check: {saved}/{len(items)} pages verified ({recovered} recovered); {len(items) - saved} remaining.')
        self.events.put(('stats', self.ledger.stats()))

    def save_page(self, page, item):
        from pypdf import PdfReader
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
            reader = PdfReader(temporary)
            if reader.is_encrypted or len(reader.pages) != 1:
                raise RuntimeError('Unexpected PDF structure or page count; review before proceeding.')
            os.replace(temporary, dest)
            self.ledger.complete(page_id, dest, usage_id)
            self.log(f'Saved {page_id} ({dest.stat().st_size:,} bytes).')
            self.events.put(('stats', self.ledger.stats()))
            return True
        except Exception as exc:
            raise RuntimeError(f'{page_id}: this page is listed, but its download failed: {exc}. The issue remains incomplete; saved pages are retained. Attempt remains counted; stopped to avoid duplicate charges.') from exc

    def assemble(self, date, items):
        from pypdf import PdfWriter, PdfReader
        if not items or not all(self.ledger.valid(item[0]) for item in items):
            return
        dest = self.root / 'editions' / date[:4] / f'Times-of-Malta-{date}.pdf'
        dest.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        try:
            for page_id, _, _ in items:
                row = self.ledger.db.execute('SELECT path FROM pages WHERE id=?', (page_id,)).fetchone()
                writer.append(self.root / row[0])
            temporary = dest.with_suffix('.part')
            writer.write(temporary)
            if len(PdfReader(temporary).pages) != len(items):
                raise RuntimeError('Merged edition page count mismatch.')
            os.replace(temporary, dest)
        finally:
            writer.close()
        self.log(f'Assembled complete edition {date}: {len(items)} pages.')

    def export(self):
        with (self.root / 'index.csv').open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['page_id', 'issue_date', 'download_url', 'listing_url', 'local_path', 'sha256', 'bytes'])
            writer.writerows(self.ledger.db.execute('SELECT * FROM pages ORDER BY id'))
        with (self.root / 'issues.csv').open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['date', 'status', 'listed_pages'])
            writer.writerows(self.ledger.db.execute('SELECT * FROM issues ORDER BY date'))

    def run(self, mode):
        try:
            from chrome_bridge import ChromeBridge, ChromePage
            if mode in ('download', 'single'):
                import pypdf
            context = ChromeBridge(self.stop, diagnostic_root=self.root)
            try:
                self.log('Waiting for your Chrome companion extension. Keep its connection tab open. No separate browser will be launched.')
                deadline = time.monotonic() + 60
                while not context.connected.wait(.2):
                    self.check()
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Chrome is not connected. Click Chrome connection setup in the GUI, load the companion extension in your signed-in Chrome, then click its toolbar icon and retry.')
                page = ChromePage(context)
                if mode in ('login', 'inspect'):
                    self.interactive_session(page, mode)
                else:
                    start, end = dt.date.fromisoformat(self.c['start']), dt.date.fromisoformat(self.c['end'])
                    dates = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
                    if self.c['newest']:
                        dates.reverse()
                    downloaded = 0
                    for date in dates:
                        self.check()
                        try:
                            items = self.list_issue(page, date.isoformat())
                        except IssueDateRedirect as exc:
                            if mode == 'single':
                                raise
                            self.log(f'SKIPPED: {exc}')
                            self.export()
                            continue
                        if mode == 'single' and not items:
                            raise IssueUnavailable(f'{date}: no issue is listed for this date. No PDFs downloaded. Try another date; this may be a non-publication day, archive gap, or changed layout.')
                        if mode in ('download', 'single'):
                            self.recover_pages(page, items)
                            for item in items:
                                if self.ledger.valid(item[0]):
                                    continue
                                if mode != 'single' and downloaded >= self.c['run_limit']:
                                    self.assemble(date.isoformat(), items)
                                    self.log('Per-run page limit reached. Resume skips saved pages.')
                                    return
                                downloaded += int(self.save_page(page, item))
                            self.assemble(date.isoformat(), items)
                        self.export()
                    self.log('Selected date range finished.')
            finally:
                context.close()
        except Stopped:
            self.log('Stopped. Progress retained.')
        except Exception as exc:
            self.log(f'PAUSED: {exc}')
            self.events.put(('error', str(exc)))
        finally:
            self.export()
            self.events.put(('stats', self.ledger.stats()))
            self.ledger.close()
            self.events.put(('done', None))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Times of Malta — Archive Downloader')
        self.geometry('960x720')
        self.minsize(800, 600)
        self.events = queue.Queue()
        self.stop_event, self.finish_event = threading.Event(), threading.Event()
        self.worker = None
        self.folder = tk.StringVar(value=str(HERE / 'library'))
        self.start_date = tk.StringVar(value='2021-11-01')
        self.end_date = tk.StringVar(value='2026-11-01')
        self.issue_date = tk.StringVar(value='2026-10-31')
        self.ceiling, self.interval, self.run_limit = tk.StringVar(value='290'), tk.IntVar(value=1), tk.StringVar(value='10')
        self.manual_pages = tk.StringVar(value='5')
        self.newest, self.auto_resume = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Times of Malta archive', font=('', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Uses your signed-in Chrome via the companion extension. Keep its connection tab open. One-time setup is required.', wraplength=880).pack(anchor='w', pady=(4, 12))
        ttk.Button(frame, text='Chrome connection setup', command=self.chrome_setup).pack(anchor='w', pady=4)
        ttk.Button(frame, text='KDE notification instructions…', command=self.notification_help).pack(anchor='w', pady=2)
        from chrome_bridge import prepare_extension
        prepare_extension()
        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Label(row, text='Library folder').pack(side='left')
        ttk.Entry(row, textvariable=self.folder).pack(side='left', fill='x', expand=True, padx=8)
        self.browse = ttk.Button(row, text='Browse…', command=self.choose_folder); self.browse.pack(side='left')
        grid = ttk.Frame(frame); grid.pack(fill='x', pady=12)
        for i, (label, variable) in enumerate([('From (YYYY-MM-DD)', self.start_date), ('Through', self.end_date), ('Local 24-hour ceiling ≤1000', self.ceiling), ('Max pages this run', self.run_limit)]):
            ttk.Label(grid, text=label).grid(row=0, column=i, sticky='w', padx=4)
            ttk.Entry(grid, textvariable=variable, width=17).grid(row=1, column=i, padx=4, sticky='ew')
            grid.columnconfigure(i, weight=1)
        delay_row = ttk.Frame(frame); delay_row.pack(fill='x', pady=(0, 8))
        ttk.Label(delay_row, text='Delay between requests:').pack(side='left')
        self.delay_slider = tk.Scale(delay_row, from_=1, to=30, resolution=1,
                                     orient='horizontal', variable=self.interval,
                                     showvalue=True, length=260, highlightthickness=0)
        self.delay_slider.pack(side='left', fill='x', expand=True, padx=8)
        ttk.Label(delay_row, text='seconds · applies to the next run').pack(side='left')
        opts = ttk.Frame(frame); opts.pack(fill='x')
        ttk.Checkbutton(opts, text='Newest first', variable=self.newest).pack(side='left')
        ttk.Checkbutton(opts, text='Wait and resume as local 24-hour allowance becomes available', variable=self.auto_resume).pack(side='left', padx=16)
        ttk.Label(frame, text='Server reset time is unverified. Accounting uses a conservative rolling 24 hours; server blocks always pause the run.', wraplength=880).pack(anchor='w', pady=8)
        row = ttk.Frame(frame); row.pack(fill='x', pady=4)
        ttk.Label(row, text='Additional pages used outside this library:').pack(side='left')
        ttk.Entry(row, textvariable=self.manual_pages, width=6).pack(side='left', padx=8)
        self.manual_button = ttk.Button(row, text='Record usage', command=self.record_usage); self.manual_button.pack(side='left')
        ttk.Label(frame, text='For the earlier pilot, record 5 once (4 saved pages + 1 uncertain attempt) if still within the last 24 hours.', wraplength=880).pack(anchor='w')
        row = ttk.Frame(frame); row.pack(fill='x', pady=12)
        self.start_buttons = []
        single_row = ttk.Frame(frame); single_row.pack(fill='x', pady=6)
        ttk.Label(single_row, text='One issue (YYYY-MM-DD):').pack(side='left')
        ttk.Entry(single_row, textvariable=self.issue_date, width=14).pack(side='left', padx=8)
        single_button = ttk.Button(single_row, text='Download / resume complete issue', command=lambda: self.launch('single'))
        single_button.pack(side='left'); self.start_buttons.append(single_button)
        ttk.Label(single_row, text='Ignores per-run page limit; respects quota.').pack(side='left', padx=8)
        for title, mode in [('Sign in', 'login'), ('Inventory only', 'inventory'), ('Download / resume', 'download'), ('Inspect e-paper', 'inspect')]:
            b = ttk.Button(row, text=title, command=lambda m=mode: self.launch(m)); b.pack(side='left', padx=(0, 8)); self.start_buttons.append(b)
        self.finish_button = ttk.Button(row, text='Finish browser session', command=self.finish_event.set, state='disabled'); self.finish_button.pack(side='left')
        self.stop_button = ttk.Button(row, text='Stop', command=self.stop_event.set, state='disabled'); self.stop_button.pack(side='right')
        self.status = tk.StringVar(value='Idle — no downloads started.')
        ttk.Label(frame, textvariable=self.status).pack(anchor='w', pady=4)
        self.logbox = tk.Text(frame, height=18, wrap='word', state='disabled')
        self.logbox.pack(fill='both', expand=True)
        self.protocol('WM_DELETE_WINDOW', self.close_app)
        self.after(150, self.poll)

    def notification_help(self):
        messagebox.showinfo('KDE download notifications',
            'These notifications come from Chrome’s Plasma Integration extension.\n\n'
            'In Chrome: Extensions → Manage extensions → Plasma Integration → Details → Extension options.\n'
            'Turn off “Show downloads in notification area”.\n\n'
            'This disables KDE download notifications for all Chrome downloads in this profile. '
            'Other Plasma integration features can remain enabled. The downloader cannot change another extension’s preferences.')

    def chrome_setup(self):
        from chrome_bridge import prepare_extension
        prepare_extension()
        path = str(HERE / 'chrome-extension')
        self.clipboard_clear()
        self.clipboard_append(path)
        messagebox.showinfo('Connect your actual Chrome',
            'In your signed-in Chrome:\n\n'
            '1. Open chrome://extensions\n'
            '2. Enable Developer mode. Click Load unpacked.\n'
            '3. Select this folder (copied to clipboard):\n' + path + '\n\n'
            '4. Click the Times of Malta Desktop Downloader Bridge extension icon. Keep its connection tab open.\n'
            '5. Return here and use Sign in or Download.\n\n'
            'This is a separate extension from the ChatGPT extension. Your existing Chrome login is used; no cookies are copied.')

    def log(self, value):
        self.logbox.configure(state='normal')
        self.logbox.insert('end', f'{dt.datetime.now():%H:%M:%S}  {value}\n')
        self.logbox.see('end'); self.logbox.configure(state='disabled')

    def choose_folder(self):
        result = filedialog.askdirectory(initialdir=self.folder.get())
        if result:
            self.folder.set(result)

    def config(self, mode=None):
        if mode == 'single':
            start = end = dt.date.fromisoformat(self.issue_date.get())
        else:
            start, end = dt.date.fromisoformat(self.start_date.get()), dt.date.fromisoformat(self.end_date.get())
        ceiling, interval = int(self.ceiling.get()), float(self.interval.get())
        limit = 300 if mode == 'single' else int(self.run_limit.get())
        if start > end or not 1 <= ceiling <= 1000 or not 1 <= interval <= 30 or limit < 1:
            raise ValueError('Check dates, ceiling (1–1000), interval (1–30 seconds), and positive run limit.')
        if not self.folder.get().strip():
            raise ValueError('Choose a library folder.')
        return dict(folder=self.folder.get(), start=start.isoformat(), end=end.isoformat(), ceiling=ceiling,
                    interval=interval, run_limit=limit, newest=self.newest.get(), auto_resume=self.auto_resume.get())

    def record_usage(self):
        try:
            ledger = Ledger(Path(self.folder.get()).resolve())
            try:
                amount = int(self.manual_pages.get())
                ledger.manual(amount)
                self.log(f'Recorded {amount} additional pages; {ledger.used()} counted in last 24 hours.')
            finally:
                ledger.close()
        except Exception as exc:
            messagebox.showerror('Usage', str(exc))

    def launch(self, mode):
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self.config(mode)
        except Exception as exc:
            messagebox.showerror('Settings', str(exc)); return
        self.stop_event.clear(); self.finish_event.clear()
        for b in self.start_buttons + [self.manual_button, self.browse]:
            b.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.finish_button.configure(state='normal' if mode in ('login', 'inspect') else 'disabled')
        self.status.set(f'Running: {mode}. Settings are captured for this run.')
        def work():
            try:
                Runner(config, self.events, self.stop_event, self.finish_event).run(mode)
            except Exception as exc:
                self.events.put(('log', f'Cannot start: {exc}'))
                self.events.put(('done', None))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.log(value)
                elif kind == 'error':
                    messagebox.showerror('Issue download paused', value)
                elif kind == 'stats':
                    self.status.set(f"{value['saved_pages']} saved pages · {value['listed_issues']} issues inventoried · {value['used_24h']} pages counted / 24h · {value['bytes']/1048576:.1f} MB")
                elif kind == 'done':
                    for b in self.start_buttons + [self.manual_button, self.browse]:
                        b.configure(state='normal')
                    self.stop_button.configure(state='disabled'); self.finish_button.configure(state='disabled')
                    self.log('Idle. You can resume or change the date range.')
        except queue.Empty:
            pass
        self.after(150, self.poll)

    def close_app(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.log('Stopping before closing; waiting for the current browser operation.')
            self.after(250, self.close_when_stopped)
        else:
            self.destroy()

    def close_when_stopped(self):
        if self.worker and self.worker.is_alive():
            self.after(250, self.close_when_stopped)
        else:
            self.destroy()


if __name__ == '__main__':
    App().mainloop()
