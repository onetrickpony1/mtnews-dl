"""Bounded coverage survey: which sampled dates are served as PDFs, without downloading them.

A probe reads only the first bytes of the response (enough to see %PDF- versus the site's
"not found" HTML) and closes the connection. A served PDF is evidence that a file exists
under that name; its printed date is NOT checked (that needs a full download).
"""

import datetime as dt
import json
import os
import tempfile
from pathlib import Path

from .config import config_dir, ensure_private_dir
from .discovery import candidate_slugs, file_url
from .net import AuthRequired, IssueMissing, Temporary, TalkError, with_retries

RESULTS = "coverage.json"


def month_range(first: str, last: str):
    y, m = map(int, first.split("-"))
    ly, lm = map(int, last.split("-"))
    while (y, m) <= (ly, lm):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def sample_date(month: str, publication: str, today=None):
    """One sample per month. it-Torca: the Sunday on/after the 15th (every listed, correctly
    named Torca issue is a Sunday). l-orizzont: the 15th, or the 16th if the 15th is a Sunday."""
    today = today or dt.date.today()
    day = dt.date.fromisoformat(month + "-15")
    if publication == "it-torca":
        day += dt.timedelta(days=(6 - day.weekday()) % 7)
    elif day.weekday() == 6:
        day += dt.timedelta(days=1)
    return day.isoformat() if day <= today else None


def load_results(path=None):
    path = Path(path or config_dir() / RESULTS)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_results(data, path=None):
    path = Path(path or config_dir() / RESULTS)
    ensure_private_dir(path.parent)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".coverage-")
    with os.fdopen(fd, "w") as out:
        json.dump(data, out, indent=2, sort_keys=True)
        out.write("\n")
    os.replace(temp, path)


def survey(session, patterns, listing, publications, months, *, every=1, formats="observed",
           delay=3, retries=2, stop, emit, refresh=False):
    results = load_results()
    listed = {(i["publication"], i["date"]): i["slug"] for i in listing if i.get("date")}
    plan = [(pub, d) for idx, month in enumerate(months) if idx % every == 0
            for pub in publications if (d := sample_date(month, pub))]
    requests = 0
    for pub, date in plan:
        if stop.is_set():
            break
        key = f"{pub}@{date}"
        old = results.get(key)
        if old and not refresh and old["result"] == "pdf" or (
                old and not refresh and old["result"] == "not found" and old.get("formats") in (formats, "all")):
            continue
        cands = ([(listed[(pub, date)], None, "listed")] if (pub, date) in listed else []) + \
            candidate_slugs(patterns, pub, date, formats)
        seen, tried, row = set(), [], None
        for slug, template, origin in cands:
            if slug in seen:
                continue
            seen.add(slug)
            url = file_url(slug)
            tried.append(slug)
            requests += 1
            try:
                res = with_retries(lambda: session.get(url, probe=True, stop=stop),
                                   retries=retries, stop=stop, emit=emit)
                row = dict(result="pdf", slug=slug, origin=origin, template=template, bytes=res.size)
            except IssueMissing:
                row = None
            except AuthRequired:
                raise
            except Temporary as exc:
                row = dict(result="error", error=str(exc))
            except TalkError as exc:
                row = dict(result="error", error=str(exc))
            if stop.wait(delay):
                break
            if row:
                break
        row = row or dict(result="not found")
        row.update(tried=tried, formats=formats, checked=dt.date.today().isoformat())
        results[key] = row
        save_results(results)
        if row["result"] == "pdf":
            from .discovery import merge_probe_evidence, save_patterns
            merge_probe_evidence(patterns, {key: row})
            save_patterns(patterns)
        shown = f"{row['slug']} ({row['origin']}, {row.get('bytes', 0) / 1e6:.0f} MB)" if row["result"] == "pdf" \
            else f"{row['result']} after {len(tried)} name(s)"
        emit(f"{date} {pub:10} {shown}")
    return results, requests


def report(results, publications, months, every=1):
    lines = []
    for pub in publications:
        found = missing = errors = 0
        first_found = None
        cells = []
        for idx, month in enumerate(months):
            if idx % every:
                continue
            date = sample_date(month, pub)
            row = results.get(f"{pub}@{date}") if date else None
            mark = "?" if not row else {"pdf": "#", "not found": ".", "error": "!"}[row["result"]]
            if row and row["result"] == "pdf":
                found += 1
                first_found = first_found or date
            elif row and row["result"] == "not found":
                missing += 1
            elif row:
                errors += 1
            cells.append((month, mark))
        lines.append(f"{pub}: {found} served, {missing} not found, {errors} errors; earliest served sample: "
                     f"{first_found or 'none'}")
        for year in sorted({m[:4] for m, _ in cells}):
            lines.append(f"  {year} " + "".join(mark for m, mark in cells if m.startswith(year)))
    lines.append("Legend: # PDF served, . not found under the tried names, ! error, ? not probed")
    return "\n".join(lines)
