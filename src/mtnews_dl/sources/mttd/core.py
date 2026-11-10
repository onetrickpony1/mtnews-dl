"""Publisher-linked issue downloads with front-page date verification and quarantine."""

import calendar
import gzip
import datetime as dt
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from ... import __version__
from ...common.lock import library_lock as _lock


def library_lock(path):
    return _lock(path, "mttd-dl")


from ...common.pdfcheck import (DATE_CHECK_VERSION, MONTHS, check_structure, classify,  # noqa: F401
                               dates_in, inspect_pdf, sha)


def validate_download_date(value):
    """Validate an exact issue date before creating state or fetching the feed."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use YYYY-MM-DD for the issue date.")
    date = dt.date.fromisoformat(value)
    if date < dt.date(1930, 1, 1) or date > dt.date.today():
        raise ValueError("Issue date must be between 1930-01-01 and today.")
    return value


def is_publisher_url(url):
    """Accept HTTPS links on the publisher's e-paper host only."""
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"maltatoday.uberflip.com", "content.cdntwrk.com"}
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
    )


class PublisherRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects outside the publisher host before making another request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_publisher_url(newurl):
            raise ValueError("Unexpected redirect; stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, dest=None, stop=None):
    if not is_publisher_url(url):
        raise ValueError("Unexpected download URL; stopped.")
    req = urllib.request.Request(url, headers={"User-Agent": f"mttd-dl/{__version__} (mtnews-dl)"})
    with urllib.request.build_opener(PublisherRedirectHandler()).open(req, timeout=45) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}; stopped.")
        # Check the final URL as well as each redirect.
        if not is_publisher_url(response.url):
            raise ValueError("Unexpected redirect; stopped.")
        total = 0
        data = bytearray()
        f = Path(dest).open("wb") if dest else None
        try:
            while chunk := response.read(1024 * 1024):
                if stop and stop.is_set():
                    raise InterruptedError("Stopped by user.")
                total += len(chunk)
                if total > (250 if dest else 20) * 1024 * 1024:
                    raise ValueError("Response exceeded size limit.")
                if f:
                    f.write(chunk)
                else:
                    data.extend(chunk)
        finally:
            if f:
                f.close()
    if not dest:
        result = bytes(data)
        return gzip.decompress(result) if result[:2] == b"\x1f\x8b" else result
    return None


class Library:
    def __init__(self, root, *, flat=False, refresh=False, dry_run=False, retries=0):
        self.refresh = refresh
        self.dry_run = dry_run
        self.retries = retries
        self.selected_keys = set()
        self.publications = {}
        self.issue_metadata = {}
        self.flat = flat
        self.root = Path(root).expanduser().resolve()
        self.state_dir = self.root / ".mttd-dl"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self.rows = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def save(self):
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.rows, indent=2) + "\n")
        os.replace(temp, self.state_file)

    def final(self, key):
        from .catalog import PUBLICATIONS
        publication = self.publications.get(key.split('@')[-1],
                                            self.rows.get(key, {}).get('publication', 'MediaToday'))
        if publication not in PUBLICATIONS:
            publication = 'MediaToday'
        metadata = dict(self.rows.get(key, {}), **self.issue_metadata.get(key, {}))
        label = metadata.get('user_corrected_date') or metadata.get('date_label') or key.split('@')[0]
        name = (f"{publication}-{label}-{key.split('@')[-1]}.pdf" if '@' in key
                else f"MaltaToday-{key}.pdf")
        folder = metadata.get('selection_year') or ('undated' if key.startswith('undated@') else key[:4])
        return self.root / name if self.flat else self.root / "editions" / folder / name

    def record(self, date, path, inspection, source=None):
        metadata = {k: v for k, v in self.rows.get(date, {}).items()
                    if k in ('selection_year', 'date_label', 'date_basis', 'date_precision',
                             'expected_issue_date', 'posted_date', 'catalog_title', 'user_corrected_date')}
        metadata.update(self.issue_metadata.get(date, {}))
        self.rows[date] = dict(
            inspection, **metadata,
            path=str(path),
            source=source,
            publication=self.publications.get(date.split("@")[-1], self.rows.get(date, {}).get("publication", "MediaToday")),
            checked_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        self.save()

    def promote(self, date, path):
        dest = self.final(date)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if Path(path) == dest:
            return dest
        if dest.exists():
            backup = self.state_dir / "replaced" / (date + "-" + str(time.time_ns()) + ".pdf")
            backup.parent.mkdir(exist_ok=True)
            dest.rename(backup)
        os.replace(path, dest)
        return dest

    def accept(self, date):
        with library_lock(self.state_dir / "run.lock"):
            self.rows = json.loads(self.state_file.read_text())
            self._accept_locked(date)

    def _accept_locked(self, date):
        row = self.rows[date]
        path = Path(row["path"])
        if sha(path) != row["sha256"]:
            raise ValueError("File changed since inspection; run the month again before accepting.")
        dest = self.promote(date, path)
        row.update(status="accepted_by_user", path=str(dest))
        self.save()

    def run_issue(self, date, stop, emit, *, delay=2):
        date = validate_download_date(date)
        self.run([date[:7]], delay, stop, emit, only_dates={date})

    def run(self, months, delay, stop, emit, *, only_dates=None):
        from .catalog import discover
        with library_lock(self.state_dir / "run.lock"):
            self.rows = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
            emit("Loading all MediaToday archive listings…")
            entries = discover(stop=stop, emit=emit, refresh=self.refresh, publications=self.publications)
            (self.state_dir / "publisher-catalog.json").write_text(json.dumps(entries, indent=2))
            for month in sorted(set(months)):
                year, mon = map(int, month.split("-"))
                for day in range(1, calendar.monthrange(year, mon)[1] + 1):
                    date = f"{month}-{day:02d}"
                    if only_dates and date not in only_dates:
                        continue
                    if dt.date.fromisoformat(date) > dt.date.today():
                        continue
                    urls = entries.get(date, [])
                    if not urls:
                        # Keep previous download records even if a listing later omits a date.
                        if date not in self.rows:
                            self.rows[date] = dict(status="unavailable", path="", detected_dates=[])
                            self.save()
                        continue
                    if self.rows.get(date, {}).get('status') in ('unavailable', 'ambiguous_feed'):
                        del self.rows[date]
                    for url in dict.fromkeys(urls):
                        if stop.is_set():
                            return
                        iid = url.rsplit('/', 1)[-1]
                        if not iid.isdigit():
                            raise ValueError("Invalid issue ID in catalog.")
                        key = f"{date}@{iid}"
                        # Preserve old single-publication progress and filenames when source matches.
                        legacy = self.rows.get(date, {})
                        if key not in self.rows and legacy.get('source') == url:
                            self.rows[key] = self.rows.pop(date)
                            self.save()
                        self.selected_keys.add(key)
                        downloaded = self._download(key, date, url, stop, emit)
                        if downloaded and stop.wait(delay):
                            return
            self.save()
            emit("Finished. See review for uncertain dates, mismatches, or unavailable issues.")

    def run_periods(self, years, months, delay, stop, emit):
        """Year selections include partial issue dates and posting-date fallbacks."""
        from .catalog import discover
        from .dating import date_metadata
        with library_lock(self.state_dir / "run.lock"):
            self.rows = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
            items = []
            discover(stop=stop, emit=emit, refresh=self.refresh,
                     publications=self.publications, archive_items=items)
            unique = {}
            for item in items:
                unique.setdefault(item['issue_id'], item)
            selected = []
            # Resolve the small set of missing issue years before downloading, never silently omit them.
            for iid, item in unique.items():
                if stop.is_set():
                    return
                metadata = date_metadata(item, stop=stop)
                exact = metadata['expected_issue_date']
                if metadata['selection_year'] in years or (exact and exact[:7] in months):
                    selected.append((iid, item, metadata))
            emit(f"Selected {len(selected)} issues across all archives, including partial/posted dates.")
            self.selected_keys = set()
            for iid, item, metadata in selected:
                if stop.is_set():
                    return
                key = next((k for k in self.rows if k.endswith('@' + iid)),
                           f"{metadata['date_label']}@{iid}")
                self.selected_keys.add(key)
                self.issue_metadata[key] = metadata
                downloaded = self._download(key, metadata['expected_issue_date'], item['url'], stop, emit)
                self.rows[key].update(metadata)
                self.save()
                if downloaded and stop.wait(delay):
                    return
            emit("Finished year/month selection. See review for PDFs needing inspection.")

    def run_archives(self, archive_ids, delay, stop, emit):
        from .catalog import discover
        with library_lock(self.state_dir / "run.lock"):
            self.rows = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
            items = []
            discover(stop=stop, emit=emit, refresh=self.refresh,
                     publications=self.publications, archive_items=items)
            selected = {}
            for item in items:
                if item['archive_id'] in archive_ids:
                    selected.setdefault(item['issue_id'], item)
            self.selected_keys = set()
            for iid, item in selected.items():
                if stop.is_set():
                    return
                date = item['date']
                key = next((k for k in self.rows if k.endswith('@' + iid)),
                           f"{date or 'undated'}@{iid}")
                self.selected_keys.add(key)
                downloaded = self._download(key, date, item['url'], stop, emit)
                if key in self.rows:
                    self.rows[key]['catalog_title'] = item['title']
                    self.save()
                if downloaded and stop.wait(delay):
                    return
            emit(f"Finished archive selection: {len(selected)} distinct issues.")

    def _download(self, key, date, url, stop, emit):
        from .catalog import resolve_pdf
        old = self.rows.get(key, {})
        oldpath = Path(old.get('path') or '/nonexistent')
        if oldpath.is_file() and old.get('sha256') == sha(oldpath):
            if old.get('status') in ('verified', 'verified_pdf', 'accepted_by_user'):
                dest = self.final(key)
                if dest != oldpath and self.publications.get(key.split('@')[-1], 'MediaToday') != 'MediaToday':
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        raise ValueError(f"Cannot rename saved issue: destination already exists: {dest}")
                    os.replace(oldpath, dest)
                    old.update(path=str(dest), publication=self.publications.get(key.split('@')[-1],
                               old.get('publication', 'MediaToday')))
                    self.save()
                    emit(f"{key}: renamed to {dest.name}.")
                emit(f"{key}: skipped, already verified.")
                return
            if old.get('status') in ('uncertain', 'wrong_date'):
                if old.get('date_check_version', 0) < DATE_CHECK_VERSION:
                    self._recheck_one(key, emit)
                else:
                    emit(f"{key}: awaiting your review; not downloaded again.")
                return
        dest = oldpath if oldpath.is_file() else self.final(key)
        if dest.exists():
            check = self._inspect(dest, date)
            self.record(key, dest, check, url)
            emit(f"{key}: existing file {check['status']}.")
            return
        if self.dry_run:
            emit(f"{key}: would download {url}")
            return False
        pending = self.state_dir / 'pending'
        pending.mkdir(exist_ok=True)
        part, path = pending / f'{key}.part', pending / f'{key}.pdf'
        emit(f"{key}: downloading whole issue…")
        try:
            with_retries(lambda: fetch(resolve_pdf(url, stop=stop), part, stop), self.retries, stop, emit, key)
            with part.open('rb') as stream:
                if stream.read(5) != b'%PDF-':
                    raise ValueError('Server returned non-PDF content; run stopped for review.')
            check = self._inspect(part, date)
            os.replace(part, path)
            if check['status'] in ('verified', 'verified_pdf'):
                path = self.promote(key, path)
            self.record(key, path, check, url)
            emit(f"{key}: {check['status']}, {check['pages']} pages.")
            return True
        except Exception as exc:
            self.rows[key] = dict(status='failed', path='', source=url,
                                  error=str(exc), detected_dates=[])
            self.save()
            raise
        finally:
            if part.exists():
                part.unlink()

    def recheck(self, emit=print):
        """Reinspect pending local PDFs only; never fetch a catalog or PDF."""
        with library_lock(self.state_dir / "run.lock"):
            self.rows = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
            for key, row in list(self.rows.items()):
                if row.get('status') in ('uncertain', 'wrong_date') and row.get('path'):
                    self._recheck_one(key, emit)

    def _recheck_one(self, key, emit):
        row = self.rows[key]
        path = Path(row['path'])
        if not path.is_file():
            emit(f"{key}: missing file; left for review.")
            return
        check = self._inspect(path, row.get('expected_issue_date', None if key.startswith('undated@') else key[:10]))
        if check['status'] in ('verified', 'verified_pdf'):
            dest = self.final(key)
            if dest != path and dest.exists():
                raise ValueError(f"Cannot promote issue: destination already exists: {dest}")
            path = self.promote(key, path)
        self.record(key, path, check, row.get('source'))
        emit(f"{key}: rechecked, {check['status']}.")

    @staticmethod
    def _inspect(path, date):
        try:
            if date is None:
                pages = check_structure(path)
                return dict(date_check_version=DATE_CHECK_VERSION, status='verified_pdf',
                            detected_dates=[], method='PDF structure checked; catalog has no exact date',
                            pages=pages, sha256=sha(path), front_text='')
            return inspect_pdf(path, date)
        except (ValueError, OSError) as exc:
            return dict(date_check_version=DATE_CHECK_VERSION, status='uncertain', detected_dates=[],
                        method='PDF inspection failed', pages=0, sha256=sha(path), front_text=str(exc))


def with_retries(call, retries, stop, emit, key):
    """Retry timeouts, connection errors, 429 and 5xx with backoff; anything else stops."""
    import socket
    import urllib.error
    for attempt in range(retries + 1):
        try:
            return call()
        except urllib.error.HTTPError as exc:
            if not (exc.code == 429 or exc.code >= 500) or attempt == retries:
                raise
            reason = f"HTTP {exc.code}"
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            if attempt == retries:
                raise
            reason = f"network error: {getattr(exc, 'reason', exc)}"
        wait = min(300, 15 * 2 ** attempt)
        emit(f"{key}: {reason}; retrying in {wait}s ({attempt + 1}/{retries}).")
        if stop.wait(wait):
            raise InterruptedError("Stopped by user.")
