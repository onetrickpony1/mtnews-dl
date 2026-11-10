"""Public archive discovery and XML PDF resolution. No login or cookie handling."""

import calendar
import datetime as dt
import json
import os
import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from .core import fetch, is_publisher_url, dates_in

BASE = "https://maltatoday.uberflip.com/"
# Unique archives behind all ten publication streams linked by the publisher hub.
ARCHIVES = ((67458, 1504229), (73329, 1547021), (80865, 1517915),
            (80868, 1496717), (263206, 1513566), (263203, 1496582),
            (287775, 1547436), (287822, 1547435), (289351, 1545759))
ARCHIVE_NAMES = {67458: "maltatoday-previous", 73329: "mediatoday-latest",
                 80865: "illum-previous", 80868: "illum-latest",
                 263206: "businesstoday-previous", 263203: "businesstoday-latest",
                 287775: "architecture-design", 287822: "gourmet-today",
                 289351: "organic-food-guide"}

ARCHIVE_PUBLICATIONS = {80865: "Illum", 80868: "Illum", 263206: "BusinessToday",
                        263203: "BusinessToday", 287775: "ArchitectureDesign",
                        287822: "GourmetToday", 289351: "OrganicFoodGuide"}
SEED_TITLES = {1504229: "maltatoday-23-july-2023", 1547021: "maltatoday-7-october-2026",
               1517915: "illum-24-march-2024", 1496717: "illum-9-april-2023",
               1513566: "business-today-23-december-2023", 1496582: "business-today-6-april-2023",
               1547436: "architecture-and-design-magazine-november",
               1547435: "gourmet-today-november-2026", 1545759: "organic-food-guide-first-edition"}
# Archive panels omit their seed. Only assign dates explicitly present in titles.
SEEDS = {"2023-07-23": f"{BASE}read/flipbook3_title_settings/67458/1504229",
         "2026-10-07": f"{BASE}read/flipbook3_title_settings/73329/1547021",
         "2024-03-24": f"{BASE}read/flipbook3_title_settings/80865/1517915",
         "2023-04-09": f"{BASE}read/flipbook3_title_settings/80868/1496717",
         "2023-12-23": f"{BASE}read/flipbook3_title_settings/263206/1513566",
         "2023-04-06": f"{BASE}read/flipbook3_title_settings/263203/1496582"}
ARCHIVE_PATTERN = "(?:" + "|".join(str(title) for title, _ in ARCHIVES) + ")"
TTL = 6 * 60 * 60


def include_seeds(entries):
    for date, url in SEEDS.items():
        if url not in entries.setdefault(date, []):
            entries[date].append(url)
    return {date: sorted(set(urls)) for date, urls in sorted(entries.items())}


class ArchiveParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = {}
        self.count = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-issue-count" in attrs:
            self.count = int(attrs["data-issue-count"])
        if tag == "a" and "archive-link" in attrs.get("class", "").split():
            url = attrs.get("href", "")
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc not in ("maltatoday.uberflip.com", "read.uberflip.com"):
                raise ValueError("Archive contains an unexpected issue host.")
            match = re.fullmatch(r"/i/(\d+)-(.+)", parsed.path)
            if not match:
                raise ValueError("Unrecognized archive issue URL.")
            self.links[match[1]] = unquote(match[2])


PUBLICATIONS = {"MaltaToday", "GozoToday", "BusinessToday", "Illum", "MediaToday", "ArchitectureDesign", "GourmetToday", "OrganicFoodGuide"}


def publication_name(slug):
    words = re.sub(r"[-_ ]+", " ", slug.lower()).strip()
    for pattern, name in ((r"^architecture(?: and)? design\b", "ArchitectureDesign"),
                          (r"^gourmet today\b", "GourmetToday"),
                          (r"^organic food guide\b", "OrganicFoodGuide"),
                          (r"^(?:gozo ?today)\b", "GozoToday"),
                          (r"^(?:business ?today|bt)\b", "BusinessToday"),
                          (r"^illum\b", "Illum"),
                          (r"^(?:malta ?today|mt|mw)\b", "MaltaToday")):
        if re.search(pattern, words):
            return name
    return "MediaToday"


def issue_date(slug):
    """Parse dates for every publication, including historical MT/MW names."""
    words = slug.lower().replace("-", " ")
    words = words.replace("februaty", "february")
    words = re.sub(r"^(mt|mw|illum)(?=\d)", r"\1 ", words)
    words = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", words)
    # Written Maltese dates and titles missing a space before the year.
    written = re.sub(r"([a-z])(?=\d{4}\b)", r"\1 ", words)
    found = dates_in(written) if not re.search(r"\d{5,}", written) else []
    if len(found) == 1:
        return found[0]
    for pattern, order in ((r"\b(\d{4}) (\d{2}) (\d{2})\b", (0, 1, 2)),
                           (r"\b(\d{2}) (\d{2}) (\d{4})\b", (2, 1, 0))):
        match = re.search(pattern, words)
        if match:
            parts = list(map(int, match.groups()))
            try:
                return dt.date(*(parts[i] for i in order)).isoformat()
            except ValueError:
                return None
    # Legacy titles include YYYYMMDD, DDMMYYYY and DD-MM-YY.
    compact = re.search(r"\b(\d{8})\b", words)
    numeric = re.search(r"\b(\d{2}) (\d{2}) (\d{2})\b", words)
    if compact or numeric:
        try:
            if compact:
                raw = compact[1]
                if raw.startswith("20"):
                    return dt.date(int(raw[:4]), int(raw[4:6]), int(raw[6:])).isoformat()
                return dt.date(int(raw[4:]), int(raw[2:4]), int(raw[:2])).isoformat()
            day, month, year = map(int, numeric.groups())
            return dt.date(2000 + year, month, day).isoformat()
        except ValueError:
            return None
    months = {name.lower(): n for n, name in enumerate(calendar.month_name) if name}
    months.update({name.lower(): n for n, name in enumerate(calendar.month_abbr) if name})
    months["sept"] = 9
    words = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", words)
    names = "|".join(sorted(months, key=len, reverse=True))
    for pattern, order in ((rf"\b(\d{{1,2}}) ({names}) (\d{{4}})\b", (2, 1, 0)),
                           (rf"\b({names}) (\d{{1,2}}) (\d{{4}})\b", (2, 0, 1))):
        match = re.search(pattern, words)
        if match:
            parts = match.groups()
            try:
                return dt.date(int(parts[order[0]]), months[parts[order[1]]],
                               int(parts[order[2]])).isoformat()
            except ValueError:
                return None
    return None


def cache_path():
    """~/.cache/mttd-dl/catalog.json on Linux (same as mttd-dl 0.8.1), per-OS cache folder elsewhere."""
    from ...common.paths import cache_dir
    override = os.environ.get("MTTD_DL_CACHE")
    return Path(override) / "catalog.json" if override else cache_dir("mttd-dl") / "catalog.json"


def valid_entries(entries):
    if not isinstance(entries, dict):
        return False
    try:
        for date, urls in entries.items():
            dt.date.fromisoformat(date)
            if not isinstance(urls, list) or not urls:
                return False
            for url in urls:
                if not isinstance(url, str) or not re.fullmatch(
                    re.escape(BASE) + rf"read/flipbook3_title_settings/{ARCHIVE_PATTERN}/\d+", url
                ):
                    return False
    except (TypeError, ValueError):
        return False
    return True


def discover(*, stop=None, emit=lambda _: None, refresh=False, publications=None, archive_items=None):
    stop = stop or threading.Event()
    names = {}
    path = cache_path()
    if not refresh and path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - saved["saved_at"]
            if saved.get("version") == 6 and 0 <= age < TTL and valid_entries(saved["entries"]):
                names = saved.get("publications", {})
                if not isinstance(names, dict) or any(name not in PUBLICATIONS for name in names.values()):
                    raise ValueError("Invalid publication metadata")
                if publications is not None:
                    publications.update(names)
                if archive_items is not None:
                    archive_items.extend(saved["archive_items"])
                return include_seeds(saved["entries"])
        except (ValueError, KeyError, TypeError, OSError):
            pass
    issues = {}
    unparsed = []
    items = {}
    for title, seed in ARCHIVES:
        seen = set()
        total = None
        page = 1
        while True:
            if stop.is_set():
                raise InterruptedError("Stopped while discovering issues.")
            url = (f"{BASE}read/archives/issue/{title}/{seed}/4/"
                   f"?page={page}&sortBy=Date&sortDirection=DESC")
            parser = ArchiveParser()
            parser.feed(fetch(url, stop=stop).decode("utf-8"))
            if page == 1:
                total = parser.count
                if total is None or not 0 <= total <= 100000:
                    raise ValueError("Archive count missing or invalid; no partial catalog saved.")
            new = set(parser.links) - seen
            if not new and len(seen) < total:
                raise ValueError("Archive pagination stopped early; no partial catalog saved.")
            seen.update(parser.links)
            for iid, slug in parser.links.items():
                label = publication_name(slug)
                if label == "MediaToday":
                    label = ARCHIVE_PUBLICATIONS.get(title, label)
                if names.get(iid, "MediaToday") == "MediaToday":
                    names[iid] = label
                date = issue_date(slug)
                items[(title, iid)] = dict(archive_id=title, issue_id=iid, title=slug, date=date,
                                          url=f"{BASE}read/flipbook3_title_settings/{title}/{iid}")
                if date:
                    # Same ID in both listings is one issue. Prefer the older archive.
                    issues.setdefault(iid, (date, f"{BASE}read/flipbook3_title_settings/{title}/{iid}"))
                else:
                    unparsed.append({"title": slug, "issue_id": iid, "archive_id": title})
            emit(f"Archive {title}: {len(seen)}/{total} entries")
            if len(seen) >= total:
                break
            page += 1
            if page > 10000:
                raise ValueError("Archive pagination exceeded safety limit.")
            if stop.wait(1):
                raise InterruptedError("Stopped while discovering issues.")
    entries = {}
    for date, url in issues.values():
        entries.setdefault(date, []).append(url)
    entries = include_seeds(entries)
    for title, seed in ARCHIVES:
        slug = SEED_TITLES[seed]
        items[(title, str(seed))] = dict(archive_id=title, issue_id=str(seed), title=slug,
                                       date=issue_date(slug),
                                       url=f"{BASE}read/flipbook3_title_settings/{title}/{seed}")
        names[str(seed)] = ARCHIVE_PUBLICATIONS.get(title, publication_name(slug))
        if not issue_date(slug):
            unparsed.append({"title": slug, "issue_id": str(seed), "archive_id": title})
    if publications is not None:
        publications.update(names)
    if archive_items is not None:
        archive_items.extend(items.values())
    if unparsed:
        emit(f"{len(unparsed)} titles have no exact date; use --collection to include them.")
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump(dict(version=6, saved_at=time.time(), entries=entries, publications=names,
                           unparsed_titles=unparsed, archive_items=list(items.values())), stream, indent=2)
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()
    return entries


def resolve_pdf(settings_url, *, stop=None):
    if not re.fullmatch(re.escape(BASE) + rf"read/flipbook3_title_settings/{ARCHIVE_PATTERN}/\d+",
                        settings_url):
        raise ValueError("Unexpected issue settings URL.")
    try:
        root = ET.fromstring(fetch(settings_url, stop=stop))
    except ET.ParseError as exc:
        raise ValueError("Issue XML unavailable (possibly login required); stopped.") from exc
    uri = root.findtext("item[@id='pdf_download_uri']", "").strip()
    if not uri or not re.fullmatch(r"[A-Za-z0-9_%=+.-]+", uri):
        raise ValueError("Issue has no public PDF reference; stopped without logging in.")
    return "https://content.cdntwrk.com/files/" + uri
