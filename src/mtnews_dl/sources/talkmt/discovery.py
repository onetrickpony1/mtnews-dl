"""Read download links from the authenticated e-Newspapers page and learn filename patterns.

Patterns come only from links actually observed (plus the user-confirmed
l-orizzont example). A template is the observed filename with its date replaced
by tokens, e.g. "l-orizzont-{Y}-{m}-{d}". Candidate URLs for unlisted dates are
generated only from verified templates.
"""

import datetime as dt
import json
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

from .config import ensure_private_dir, patterns_path
from .net import BASE, is_allowed
from ...common.pdfcheck import dates_in, fold

FILE_PATH_RE = re.compile(r"^/newspapers/mp-files/([^/?#]{1,200})/?$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.pdf$")
PAPERS = {
    "l-orizzont": dict(label="LOrizzont", title="L-Orizzont", aliases=("l-orizzont", "lorizzont", "orizzont")),
    "it-torca": dict(label="ItTorca", title="It-Torċa", aliases=("it-torca", "ittorca", "torca")),
}
PUBLICATIONS = (*PAPERS, "supplements")
# Owner-confirmed downloads (authenticated browser). Both l-orizzont conventions exist;
# neither is assumed for it-Torca or supplements, which learn only from observed links.
SEED = {"l-orizzont": {
    "l-orizzont-{Y}-{m}-{d}.pdf": dict(
        evidence="user-confirmed example l-orizzont-2026-11-03.pdf", seed_dates=["2026-11-03"],
        listing_dates=[], unlisted_confirmed=[], titles=[]),
    "l-orizzont-{d}-{m}-{Y}.pdf": dict(
        evidence="user-confirmed example l-orizzont-04-11-2025.pdf", seed_dates=["2025-11-04"],
        listing_dates=[], unlisted_confirmed=[], titles=[]),
}}
# Date forms recognised in observed filenames. Separators inside the date are kept in the
# learned template, so e.g. 2025_03_29 or 2025.03.29 would be reproduced exactly.
DATE_FORMS = (
    (re.compile(r"(?<!\d)(\d{4})([-_.])(\d{2})\2(\d{2})(?!\d)"), "{Y}%s{m}%s{d}", (0, 2, 3)),
    (re.compile(r"(?<!\d)(\d{2})([-_.])(\d{2})\2(\d{4})(?!\d)"), "{d}%s{m}%s{Y}", (3, 2, 0)),
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"), "{Y}{m}{d}", (0, 1, 2)),
)

# Bounded fallback registry for the main papers, tried after observed patterns, before an
# issue is reported missing. Tokens: {Y} year, {m}/{d} zero-padded, {M}/{D} unpadded.
# Add entries here (e.g. month names, two-digit years) only when real links show them.
FALLBACK_FORMATS = (
    ("YYYY-MM-DD", "{Y}-{m}-{d}"),
    ("DD-MM-YYYY", "{d}-{m}-{Y}"),
    ("MM-DD-YYYY", "{m}-{d}-{Y}"),
    ("YYYY-M-D", "{Y}-{M}-{D}"),
    ("D-M-YYYY", "{D}-{M}-{Y}"),
    ("M-D-YYYY", "{M}-{D}-{Y}"),
    ("YYYYMMDD", "{Y}{m}{d}"),
    ("DDMMYYYY", "{d}{m}{Y}"),
    ("MMDDYYYY", "{m}{d}{Y}"),
)
MAX_CANDIDATES = 12  # per publication and date, after de-duplication


def file_url(slug: str) -> str:
    return f"{BASE}mp-files/{quote(slug, safe='._-')}/"


def slug_from_url(url: str):
    """Return the mp-files filename for a subscribe.mt download URL, else None."""
    url = urljoin(BASE, url.strip())
    if not is_allowed(url):
        return None
    match = FILE_PATH_RE.match(urlparse(url).path)
    if not match:
        return None
    slug = unquote(match[1])
    return slug if SLUG_RE.match(slug) else None


def split_date(slug: str):
    """Return (template, iso_date) for the first valid date in a filename, else (None, None)."""
    for pattern, token, order in DATE_FORMS:
        for match in pattern.finditer(slug):
            parts = match.groups()
            if "%s" in token:
                sep = parts[1]
                token_text = token % (sep, sep)
            else:
                token_text = token
            try:
                date = dt.date(int(parts[order[0]]), int(parts[order[1]]), int(parts[order[2]]))
            except ValueError:
                continue
            if not 1930 <= date.year <= 2100:
                continue
            return slug[:match.start()] + token_text + slug[match.end():], date.isoformat()
    return None, None


def render(template: str, date: str) -> str:
    year, month, day = date.split("-")
    return (template.replace("{Y}", year).replace("{m}", month).replace("{d}", day)
            .replace("{M}", str(int(month))).replace("{D}", str(int(day))))


def paper_affixes(patterns: dict, publication: str) -> list:
    """(prefix, suffix) pairs around the date, from observed templates, plus the plain name."""
    seen = {}
    for template, info in patterns.get(publication, {}).items():
        if "{" not in template:
            continue
        prefix = template[:template.index("{")]
        suffix = template[template.rindex("}") + 1:]
        weight = len(info.get("listing_dates", [])) + len(info.get("unlisted_confirmed", []))
        seen[(prefix, suffix)] = seen.get((prefix, suffix), 0) + weight + 1
    seen.setdefault((publication + "-", ".pdf"), 0)
    return [pair for pair, _w in sorted(seen.items(), key=lambda kv: -kv[1])]


def candidate_slugs(patterns: dict, publication: str, date: str, formats="all") -> list:
    """Ordered, de-duplicated, bounded candidates: [(slug, template, origin)].

    Observed/trusted templates first (closest evidence first). For the main papers with
    formats="all", the FALLBACK_FORMATS registry follows. Supplements never get fallbacks.
    """
    out, seen = [], set()

    def add(template, origin):
        slug = render(template, date)
        if slug not in seen and SLUG_RE.match(slug) and len(out) < MAX_CANDIDATES:
            seen.add(slug)
            out.append((slug, template, origin))

    for template in verified_templates(patterns, publication, date):
        add(template, "observed pattern")
    if publication in PAPERS and formats == "all":
        for prefix, suffix in paper_affixes(patterns, publication):
            for name, tokens in FALLBACK_FORMATS:
                add(prefix + tokens + suffix, f"fallback {name}")
    return out


def publication_of(slug: str, template, title: str) -> str:
    base = fold(template or slug).lower()
    base = base[:-4] if base.endswith(".pdf") else base
    if template:
        prefix, _, suffix = base.partition("{")
        suffix = suffix.split("}")[-1] if suffix else ""
        prefix = prefix.strip("-_. ")
        if not suffix.strip("-_. "):
            for pub, info in PAPERS.items():
                if prefix in info["aliases"]:
                    return pub
        return "supplements"
    # Undated filenames: only a whole-name match counts as a main paper.
    for pub, info in PAPERS.items():
        for alias in info["aliases"]:
            rest = base.strip("-_. ")[len(alias):] if base.strip("-_. ").startswith(alias) else None
            if rest is not None and re.fullmatch(r"[-_\d]*", rest):
                return pub  # e.g. the paper's name plus an unparseable number
    return "supplements"


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self._stack = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self._stack.append(dict(href=attrs["href"], text=[], attr_title=attrs.get("title") or "",
                                    aria=attrs.get("aria-label") or ""))
        elif tag == "img" and self._stack:
            self._stack[-1]["text"].append(attrs.get("alt") or "")

    def handle_data(self, data):
        if self._stack:
            self._stack[-1]["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._stack:
            self.links.append(self._stack.pop())


def parse_listing(html: str) -> list:
    parser = _Links()
    parser.feed(html)
    items, seen = [], set()
    for link in parser.links:
        slug = slug_from_url(link["href"])
        if not slug or slug in seen:
            continue
        seen.add(slug)
        title = " ".join(" ".join(link["text"]).split()) or link["attr_title"] or link["aria"]
        if title.lower() in ("download", "shkarga", "niżżel", "nizzel", "pdf", ""):
            title = ""
        template, date = split_date(slug)
        title_dates = dates_in(title) if title else []
        items.append(dict(slug=slug, url=file_url(slug), title=title or slug[:-4],
                          template=template, date=date or (title_dates[0] if len(title_dates) == 1 else None),
                          date_source="filename" if date else ("link text" if len(title_dates) == 1 else None),
                          title_dates=title_dates,
                          publication=publication_of(slug, template, title)))
    return items


def load_patterns(path=None) -> dict:
    path = Path(path or patterns_path())
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    for pub, templates in SEED.items():
        for template, info in templates.items():
            data.setdefault(pub, {}).setdefault(template, json.loads(json.dumps(info)))
    return data


def save_patterns(data, path=None):
    path = Path(path or patterns_path())
    ensure_private_dir(path.parent)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".patterns-")
    with os.fdopen(fd, "w") as out:
        json.dump(data, out, indent=2, ensure_ascii=False)
        out.write("\n")
    os.replace(temp, path)


def learn(patterns: dict, items: list, today=None) -> dict:
    today = (today or dt.date.today()).isoformat()
    for item in items:
        if not item["template"]:
            continue
        info = patterns.setdefault(item["publication"], {}).setdefault(
            item["template"], dict(evidence="observed on the e-Newspapers page", listing_dates=[],
                                   unlisted_confirmed=[], titles=[]))
        if item["date"] not in info["listing_dates"]:
            info["listing_dates"] = sorted(set(info["listing_dates"]) | {item["date"]})
        if item["title"] and item["title"] not in info.setdefault("titles", []):
            info["titles"] = (info["titles"] + [item["title"]])[-5:]
        info["last_seen"] = today
    return patterns


def verified_templates(patterns: dict, publication: str, date=None) -> list:
    """Templates trusted for unlisted dates, best first.

    Supplement series need two observed dates. With `date`, the template whose evidence
    (listing, confirmed downloads, owner examples) lies closest to that date comes first,
    because the naming convention changed over time.
    """
    result = []
    target = dt.date.fromisoformat(date) if date else None
    for template, info in patterns.get(publication, {}).items():
        evidence = (info.get("listing_dates", []) + info.get("unlisted_confirmed", [])
                    + info.get("seed_dates", []) + info.get("probe_dates", []))
        observed = len(info.get("listing_dates", [])) + len(info.get("unlisted_confirmed", []))
        seeded = info.get("evidence", "").startswith("user-confirmed")
        probed = bool(info.get("probe_dates")) and publication != "supplements"
        needed = 2 if publication == "supplements" else 1
        if not (observed >= needed or (seeded and publication != "supplements") or probed):
            continue
        if target and evidence:
            distance = min(abs((dt.date.fromisoformat(d) - target).days) for d in evidence)
        else:
            distance = 10 ** 6
        result.append((distance, -len(evidence), template))
    return [template for *_rank, template in sorted(result)]


def merge_probe_evidence(patterns: dict, results: dict) -> dict:
    """Add dates where a coverage probe found a PDF under a template ("probe_dates").

    Probe evidence orders candidates (closest evidence first) so fewer requests are wasted;
    it is not a verified download and never replaces the printed-date check.
    """
    for key, row in results.items():
        if row.get("result") != "pdf" or "@" not in key:
            continue
        pub, date = key.split("@", 1)
        template = row.get("template") or split_date(row.get("slug", ""))[0]
        if pub not in PAPERS or not template:
            continue
        info = patterns.setdefault(pub, {}).setdefault(template, dict(
            evidence="coverage probe (PDF served)", listing_dates=[], unlisted_confirmed=[], titles=[]))
        if date not in info.setdefault("probe_dates", []):
            info["probe_dates"] = sorted(info["probe_dates"] + [date])
    return patterns
