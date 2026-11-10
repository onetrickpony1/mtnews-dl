"""Times of Malta e-paper: the publisher's public Atom feed, and a growing record of its links.

The feed (https://app.timesofmalta.com/endpoint.xml) lists whole-issue PDFs for roughly the
last two years; older entries drop off as new ones arrive, but their PDF links keep working
(checked 2026-11-04). So every link ever seen is kept in a local link record, seeded from
feed snapshots saved since November 2024 (data/links-seed.json). Nothing is guessed: only
links the publisher listed are used.
"""

import datetime as dt
import json
import os
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from importlib.resources import files
from pathlib import Path
from urllib.parse import urljoin, urlparse

from ... import __version__
from ...common.paths import data_dir

FEED = "https://app.timesofmalta.com/endpoint.xml"
HOST = "app.timesofmalta.com"
USER_AGENT = f"tom-dl/{__version__} (mtnews-dl; personal archive)"
FEED_LIMIT = 20 * 1024 * 1024
PDF_LIMIT = 300 * 1024 * 1024
NS = {"a": "http://www.w3.org/2005/Atom", "d": "http://purl.org/dc/terms/"}


class FeedError(Exception):
    pass


class LinkMissing(FeedError):
    """The listed PDF is gone (404/410)."""


class Temporary(FeedError):
    """Timeout, connection error, 429 or 5xx: worth retrying later."""


def is_publisher_url(url) -> bool:
    """HTTPS on the e-paper host only."""
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname == HOST and parsed.port in (None, 443)
            and parsed.username is None and parsed.password is None)


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_publisher_url(newurl):
            raise FeedError("Unexpected redirect away from the publisher; stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, dest=None, stop=None, timeout=60):
    """GET a publisher URL into memory (feed) or into `dest` (PDF)."""
    import socket
    import urllib.error
    if not is_publisher_url(url):
        raise FeedError("Unexpected URL; stopped.")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    limit = PDF_LIMIT if dest else FEED_LIMIT
    try:
        response = urllib.request.build_opener(_Redirects()).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            raise LinkMissing(f"HTTP {exc.code}") from None
        if exc.code == 429 or exc.code >= 500:
            raise Temporary(f"HTTP {exc.code}") from None
        raise FeedError(f"HTTP {exc.code}; stopped.") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
        raise Temporary(f"network error: {getattr(exc, 'reason', exc)}") from None
    with response:
        if not is_publisher_url(response.url):
            raise FeedError("Unexpected redirect; stopped.")
        total, data = 0, bytearray()
        out = Path(dest).open("wb") if dest else None
        try:
            while True:
                try:
                    chunk = response.read(1024 * 1024)
                except (socket.timeout, TimeoutError, ConnectionError) as exc:
                    raise Temporary(f"network error: {exc}") from None
                if not chunk:
                    break
                if stop is not None and stop.is_set():
                    raise InterruptedError("Stopped by user.")
                total += len(chunk)
                if total > limit:
                    raise FeedError("Response exceeded the size limit; stopped.")
                if out:
                    out.write(chunk)
                else:
                    data.extend(chunk)
        finally:
            if out:
                out.close()
    return None if dest else bytes(data)


def parse_feed(data) -> dict:
    """{date: [{"url", "title"}]} for entries with an explicit application/pdf link."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError(f"The feed is not valid XML ({exc}).") from None
    found = {}
    for entry in root.findall("a:entry", NS):
        date = (entry.findtext("d:issued", "", NS) or "").strip()[:10]
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            continue
        title = (entry.findtext("a:title", "", NS) or "").strip()
        for link in entry.findall("a:link", NS):
            if link.get("type") != "application/pdf":
                continue
            url = urljoin(FEED, link.get("href", ""))
            if is_publisher_url(url) and not any(e["url"] == url for e in found.get(date, [])):
                found.setdefault(date, []).append(dict(url=url, title=title))
    return found


# ---- link record ---------------------------------------------------------------------------

def record_path() -> Path:
    return data_dir("tom-dl") / "links.json"


def snapshots_dir() -> Path:
    return data_dir("tom-dl") / "feeds"


def _seed() -> dict:
    try:
        return json.loads((files(__package__) / "data" / "links-seed.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


class LinkRecord:
    """Every e-paper PDF link seen: {date: {"links": [{url, title}], "first_seen", "last_seen"}}."""

    def __init__(self, path=None):
        self.path = Path(path) if path else record_path()
        self.rows = _seed().get("dates", {})
        if self.path.exists():
            try:
                own = json.loads(self.path.read_text(encoding="utf-8")).get("dates", {})
            except ValueError:
                own = {}
            for date, row in own.items():
                self._merge_row(date, row)
        self.live = {}          # dates in the feed read during this run

    def _merge_row(self, date, row):
        mine = self.rows.setdefault(date, dict(links=[], first_seen=row.get("first_seen"),
                                               last_seen=row.get("last_seen")))
        for link in row.get("links", []):
            if not any(l["url"] == link["url"] for l in mine["links"]):
                mine["links"].append(dict(url=link["url"], title=link.get("title", "")))
        for key, pick in (("first_seen", min), ("last_seen", max)):
            values = [v for v in (mine.get(key), row.get(key)) if v]
            mine[key] = pick(values) if values else None

    def add_feed(self, entries, seen_at=None):
        seen_at = seen_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for date, links in entries.items():
            self._merge_row(date, dict(links=links, first_seen=seen_at, last_seen=seen_at))
        return len(entries)

    def links(self, date) -> list:
        return [l["url"] for l in self.rows.get(date, {}).get("links", [])]

    def dates(self) -> list:
        return sorted(d for d, r in self.rows.items() if r.get("links"))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".links-")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(dict(format=1, dates=dict(sorted(self.rows.items()))), out, indent=1)
            out.write("\n")
        os.replace(temp, self.path)

    def refresh(self, stop=None, save_snapshot=True):
        """Read the live feed, merge it in and save the record. Returns the feed's entries."""
        data = fetch(FEED, stop=stop)
        entries = parse_feed(data)
        if not entries:
            raise FeedError("The feed has no PDF links (changed format?); nothing updated.")
        self.add_feed(entries)
        self.live = entries
        self.save()
        if save_snapshot:
            folder = snapshots_dir()
            folder.mkdir(parents=True, exist_ok=True)
            day = dt.date.today().isoformat()
            (folder / f"publisher-feed-{day}.xml").write_bytes(data)
        return entries
