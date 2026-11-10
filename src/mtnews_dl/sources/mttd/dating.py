"""Year selection with explicit date precision and publisher posting fallback."""
import calendar
import datetime as dt
import json
import os
import re
import tempfile
from urllib.parse import quote
from .core import dates_in
from .catalog import BASE, cache_path, issue_date
from .core import MONTHS, fetch


def title_date(title):
    exact = issue_date(title)
    if exact:
        return dict(selection_year=exact[:4], date_label=exact, date_basis='issue',
                    date_precision='day', expected_issue_date=exact)
    years = set(re.findall(r'(?<!\d)((?:19|20)\d{2})(?!\d)', title))
    if len(years) != 1:
        return None
    year = years.pop()
    words = re.sub(r'[-_]+', ' ', title.lower())
    months = dict(MONTHS)
    months.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
    found = {number for name, number in months.items() if re.search(r'\b'+name+r'\b', words)}
    label = f'{year}-{next(iter(found)):02d}' if len(found) == 1 else year
    return dict(selection_year=year, date_label=label, date_basis='issue',
                date_precision='month' if len(found) == 1 else 'year', expected_issue_date=None)


def posted_date(item, *, stop=None):
    """Read the reader's publish_date, never its later modification timestamp."""
    path = cache_path().with_name('posting-dates.json')
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    iid = item['issue_id']
    value = saved.get(iid)
    if value:
        dt.date.fromisoformat(value)
        return value
    html = fetch(f'{BASE}i/{iid}', stop=stop).decode('utf-8')
    match = re.search(r'"issueData"\s*:\s*', html)
    if match:
        data = json.JSONDecoder().raw_decode(html[match.end():])[0]
        if str(data.get('id')) != iid:
            raise ValueError('Reader issue ID does not match the catalog.')
        value = str(data.get('publish_date', ''))[:10]
        dt.date.fromisoformat(value)
    else:
        streams = {67458: 'maltatoday-previous-editions', 73329: 'maltatoday-latest-edition',
                   80865: 'illum-previous-editions', 80868: 'illum-latest-edition',
                   263206: 'businesstoday-previous-editions', 263203: 'businesstoday-latest-edition',
                   287775: 'architecture-design', 287822: 'gourmet-today', 289351: 'organic-food-guide'}
        stream = streams[item['archive_id']]
        hub = fetch(f"{BASE}{stream}/{quote(item['title'], safe='-')}", stop=stop).decode('utf-8')
        # Check this is the requested item, not a redirect to a stream or a related issue.
        iframe = re.search(r'<iframe[^>]+src="https://(?:maltatoday|read)\.uberflip\.com/i/(\d+)[?"/]', hub)
        stamp = re.search(r'<span\s+class="date"\s*>([^<]+)</span>', hub)
        found = dates_in(stamp[1]) if stamp else []
        if not iframe or iframe[1] != iid or len(found) != 1:
            raise ValueError(f"No public posting date for issue {iid} ({item['title']}); cannot assign a year.")
        value = found[0]
    saved[iid] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        json.dump(saved, stream, indent=2)
        temp = stream.name
    os.replace(temp, path)
    return value


def date_metadata(item, *, stop=None):
    metadata = title_date(item['title'])
    if metadata is None:
        posted = posted_date(item, stop=stop)
        metadata = dict(selection_year=posted[:4], date_label='posted-'+posted,
                        date_basis='posted', date_precision='day', expected_issue_date=None,
                        posted_date=posted)
    return dict(metadata, catalog_title=item['title'])
