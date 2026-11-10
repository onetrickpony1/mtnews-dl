"""Publisher-linked issue downloads with front-page date verification and quarantine."""

import calendar
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .platform_support import library_lock

FEED = "https://app.timesofmalta.com/endpoint.xml"
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
DATE_RE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\b", re.I
)
DMY_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")\s*,?\s*(\d{4})\b", re.I
)


def validate_download_date(value):
    """Validate an exact issue date before creating state or fetching the feed."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use YYYY-MM-DD for the issue date.")
    date = dt.date.fromisoformat(value)
    if date < dt.date(1930, 1, 1) or date > dt.date.today():
        raise ValueError("Issue date must be between 1930-01-01 and today.")
    return value


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def dates_in(text):
    text = " ".join(text.split())
    found = set()
    for pattern, order in ((DATE_RE, (2, 0, 1)), (DMY_RE, (2, 1, 0))):
        for match in pattern.finditer(text):
            values = match.groups()
            try:
                found.add(
                    dt.date(
                        int(values[order[0]]),
                        MONTHS[values[order[1]].lower()],
                        int(values[order[2]]),
                    ).isoformat()
                )
            except ValueError:
                pass
    return sorted(found)


def classify(expected, found):
    if found == [expected]:
        return "verified"
    if found and expected not in found:
        return "wrong_date"
    return "uncertain"


def inspect_pdf(path, expected):
    path = Path(path)
    with path.open("rb") as f:
        if f.read(5) != b"%PDF-":
            raise ValueError("Response is not a PDF (possibly a sign-in or security page).")
    info = subprocess.run(
        ["pdfinfo", str(path)], capture_output=True, text=True, check=True, timeout=45
    ).stdout
    match = re.search(r"^Pages:\s+(\d+)", info, re.M)
    if not match or int(match[1]) < 1:
        raise ValueError("PDF has no readable pages.")
    text = subprocess.run(
        ["pdftotext", "-f", "1", "-l", "1", "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
    ).stdout
    found = dates_in(text)
    method = "embedded text (whole front page)"
    # Masthead dates outrank dates mentioned in stories. Use actual PDF word positions.
    try:
        bbox = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", "-bbox-layout", str(path), "-"],
            capture_output=True,
            text=True,
            check=True,
            timeout=45,
        ).stdout
        tree = ET.fromstring(bbox)
        ns = {"h": "http://www.w3.org/1999/xhtml"}
        page = tree.find(".//h:page", ns)
        height = float(page.get("height"))
        lines = [
            " ".join(w.text or "" for w in line.findall("h:word", ns))
            for line in page.findall(".//h:line", ns)
            if float(line.get("yMax")) <= height * 0.28
        ]
        masthead_dates = dates_in(" ".join(lines))
        if masthead_dates:
            found = masthead_dates
            method = "embedded text (top 28% masthead region)"
    except (ET.ParseError, AttributeError, ValueError, subprocess.SubprocessError):
        pass
    # Only OCR when no full written date is extractable. No image resampling.
    if not found:
        try:
            with tempfile.TemporaryDirectory(prefix="tom-date-") as temp:
                prefix = str(Path(temp) / "front")
                subprocess.run(
                    [
                        "pdftoppm",
                        "-f",
                        "1",
                        "-l",
                        "1",
                        "-r",
                        "150",
                        "-singlefile",
                        "-png",
                        str(path),
                        prefix,
                    ],
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                cmd = ["tesseract", prefix + ".png", "stdout", "-l", "eng", "--psm", "3"]
                ocr = subprocess.run(
                    cmd, capture_output=True, text=True, check=True, timeout=90
                ).stdout
                found = dates_in(ocr)
                text += "\nOCR:\n" + ocr
                method = "OCR" if found else "text + OCR; no date found"
        except (OSError, subprocess.SubprocessError):
            method = "text; OCR unavailable or failed"
    return dict(
        status=classify(expected, found),
        detected_dates=found,
        method=method,
        pages=int(match[1]),
        sha256=sha(path),
        front_text=text[:30000],
    )


def parse_feed(data):
    ns = {"a": "http://www.w3.org/2005/Atom", "d": "http://purl.org/dc/terms/"}
    found = {}
    for entry in ET.fromstring(data).findall("a:entry", ns):
        date = entry.findtext("d:issued", "", ns)
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            continue
        for link in entry.findall("a:link", ns):
            if link.get("type") != "application/pdf":
                continue
            url = urljoin(FEED, link.get("href", ""))
            if not is_publisher_url(url):
                continue
            found.setdefault(date, set()).add(url)
    return {date: sorted(urls) for date, urls in found.items()}


def is_publisher_url(url):
    """Accept HTTPS links on the publisher's e-paper host only."""
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "app.timesofmalta.com"
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
    req = urllib.request.Request(url, headers={"User-Agent": "tom-dl/0.1.0"})
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
    return bytes(data) if not dest else None


class Library:
    def __init__(self, root, *, flat=False):
        self.flat = flat
        self.root = Path(root).expanduser().resolve()
        self.state_dir = self.root / (".tom-dl" if flat else "epaper")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self.rows = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def save(self):
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.rows, indent=2) + "\n")
        os.replace(temp, self.state_file)

    def final(self, date):
        if self.flat:
            return self.root / f"Times-of-Malta-{date}.pdf"
        return self.root / "editions" / date[:4] / f"Times-of-Malta-{date}.pdf"

    def record(self, date, path, inspection, source=None):
        self.rows[date] = dict(
            inspection,
            path=str(path),
            source=source,
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

    def run_issue(self, date, stop, emit):
        date = validate_download_date(date)
        self.run([date[:7]], 0, stop, emit, only_date=date)

    def run(self, months, delay, stop, emit, *, only_date=None):
        with library_lock(self.state_dir / "run.lock"):
            self.rows = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
            emit("Fetching publisher issue feed…")
            feed = fetch(FEED, stop=stop)
            (self.state_dir / "publisher-feed.xml").write_bytes(feed)
            entries = parse_feed(feed)
            today = dt.date.today()
            for month in sorted(months):
                year, mon = map(int, month.split("-"))
                for day in range(1, calendar.monthrange(year, mon)[1] + 1):
                    date = f"{month}-{day:02d}"
                    if only_date is not None and date != only_date:
                        continue
                    if stop.is_set():
                        emit(
                            "Stopped. Completed PDFs are saved; resume by selecting the same months."
                        )
                        return
                    if dt.date.fromisoformat(date) > today:
                        continue
                    dest = self.final(date)
                    old = self.rows.get(date, {})
                    oldpath = Path(old.get("path", "/nonexistent"))
                    if oldpath.is_file() and old.get("sha256") == sha(oldpath):
                        if old.get("status") in ("verified", "accepted_by_user"):
                            emit(f"{date}: skipped, already verified.")
                            continue
                        if old.get("status") in ("uncertain", "wrong_date"):
                            emit(f"{date}: awaiting your review; not downloaded again.")
                            continue
                    if dest.exists():
                        emit(f"{date}: checking existing front page…")
                        try:
                            check = inspect_pdf(dest, date)
                        except (ValueError, subprocess.SubprocessError) as exc:
                            check = dict(
                                status="uncertain",
                                detected_dates=[],
                                method="PDF inspection failed",
                                pages=0,
                                sha256=sha(dest),
                                front_text=str(exc),
                            )
                        self.record(date, dest, check)
                        emit(f"{date}: existing file {check['status']}.")
                        continue
                    urls = entries.get(date, [])
                    if len(urls) != 1:
                        self.rows[date] = dict(
                            status="unavailable" if not urls else "ambiguous_feed",
                            detected_dates=[],
                            path="",
                            source=urls,
                            front_text="No whole-issue PDF listed in the current feed."
                            if not urls
                            else "Multiple PDF links; manual selection required.",
                        )
                        self.save()
                        emit(f"{date}: {self.rows[date]['status']}.")
                        continue
                    pending = self.state_dir / "pending"
                    pending.mkdir(exist_ok=True)
                    part = pending / f"{date}.part"
                    path = pending / f"{date}.pdf"
                    emit(f"{date}: downloading whole issue…")
                    try:
                        fetch(urls[0], part, stop)
                        # Unexpected HTML / challenge pages stop the run, never silently retry.
                        with part.open("rb") as f:
                            if f.read(5) != b"%PDF-":
                                raise ValueError(
                                    "Server returned non-PDF content; run stopped for review."
                                )
                        try:
                            check = inspect_pdf(part, date)
                        except (ValueError, subprocess.SubprocessError) as exc:
                            check = dict(
                                status="uncertain",
                                detected_dates=[],
                                method="PDF inspection failed",
                                pages=0,
                                sha256=sha(part),
                                front_text=str(exc),
                            )
                        os.replace(part, path)
                        if check["status"] == "verified":
                            path = self.promote(date, path)
                        self.record(date, path, check, urls[0])
                        emit(f"{date}: {check['status']}, {check['pages']} pages.")
                    finally:
                        if part.exists():
                            part.unlink()
                    stop.wait(delay)
            emit("Finished. See Review for uncertain dates, mismatches, or unavailable issues.")
