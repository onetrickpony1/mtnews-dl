"""Searchable daily CMS article collections for the retired-reader gap.

A bundled, audited CDX inventory bootstraps discovery. Every unknown publication
 date is resolved from the actual page; URL date hints never exclude an article.
Successful parsing and raw responses are cached across destination libraries.
"""
import datetime as dt
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .catalog import cache_path
from .core import dates_in, sha
from .legacy import render_pdf
from ... import __version__
from .core import library_lock

START, END = '2010-06-01', '2013-09-30'
VERSION = 1
ARTICLE_PATH = re.compile(r'^/(news|arts|sports|business|comment|lifestyle)/[^/]+/(\d+)/[^/]+/?$')
IMAGE_HOSTS = {'www.maltatoday.com.mt', 'content.maltatoday.com.mt', 'cdn.maltatoday.com.mt'}


def unwrap(url):
    p = urlsplit(url)
    if p.hostname == 'web.archive.org':
        if p.scheme not in ('http', 'https') or p.port not in (None, 80, 443) or p.username or p.password:
            raise ValueError('Invalid archive URL')
        match = re.fullmatch(r'/web/(\d{14})id_/(https?://.+)', p.path)
        if not match:
            raise ValueError('Not an exact archived capture')
        return match[2]
    return url


def article_id(url):
    p = urlsplit(unwrap(url))
    match = ARTICLE_PATH.fullmatch(p.path)
    if p.scheme not in ('http', 'https') or p.hostname != 'www.maltatoday.com.mt' or p.port not in (None, 80, 443) or p.username or p.password or not match:
        raise ValueError('Not a supported MaltaToday CMS article URL')
    return match[2]


def live_url(url):
    article_id(url)
    p = urlsplit(unwrap(url))
    return urlunsplit(('https', 'www.maltatoday.com.mt', p.path, '', ''))


def asset_url(url, base):
    p = urlsplit(unwrap(urljoin(base, url)))
    if p.scheme not in ('http', 'https') or p.hostname not in IMAGE_HOSTS or p.port not in (None, 80, 443) or p.username or p.password or '..' in unquote(p.path).split('/'):
        raise ValueError('Unsupported article image URL')
    if not p.path.startswith(('/ui/', '/ui_frontend/')):
        raise ValueError('Unsupported article image path')
    return urlunsplit(('https', p.hostname, p.path, '', ''))


def allowed_url(url, *, asset=False):
    if asset:
        asset_url(url, url)
    else:
        article_id(url)
    # All network requests, including Wayback replay redirects, use HTTPS.
    if urlsplit(url).scheme != 'https':
        raise ValueError('HTTPS is required for requests')


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, asset):
        self.asset = asset
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.startswith('http://web.archive.org/'):
            newurl = 'https://' + newurl[7:]
        allowed_url(newurl, asset=self.asset)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def load_catalog():
    data = json.loads(files('mttd_dl').joinpath('data/gap-catalog.json').read_text(encoding='utf-8'))
    unique = {}
    for row in data['items']:
        iid = article_id(row['url'])
        if iid != row['id'] or article_id(row['capture']) != iid:
            raise ValueError('Invalid bundled gap catalog')
        if row.get('verified_capture') and article_id(row['verified_capture']) != iid:
            raise ValueError('Invalid verified capture')
        if row.get('verified_date'):
            dt.date.fromisoformat(row['verified_date'])
        unique.setdefault(iid, row)
    return list(unique.values())


def selected(date, years, months, only_date):
    return START <= date <= END and (date == only_date if only_date else date[:4] in years or date[:7] in months)


def gap_requested(years, months, only_date):
    if only_date:
        return START <= only_date <= END
    return any('2010' <= y <= '2013' for y in years) or any('2010-06' <= m <= '2013-09' for m in months)


def parse_article(data, url, retrieved):
    """Recognize both archived and current CMS templates, excluding navigation text."""
    iid = article_id(url)
    if article_id(retrieved) != iid:
        raise ValueError('Redirect changed article identity')
    try:
        decoded = data.decode('utf-8')
    except UnicodeDecodeError:
        decoded = data
    soup = BeautifulSoup(decoded, 'html.parser')
    canonical = soup.find('meta', property='og:url')
    if canonical and canonical.get('content') and article_id(canonical['content']) != iid:
        raise ValueError('Page metadata identifies a different article')
    module = soup.select_one('[data-module-name="full_article"]')
    body = module.select_one('.content') if module else None
    headline = soup.select_one('[data-module-name="article_heading"] h1') or (module.find('h1') if module else None)
    date_node = soup.select_one('.date-text .date') or (module.select_one('.date') if module else None)
    if body is None or headline is None or date_node is None:
        raise ValueError('No recognizable CMS article body, headline or publication date')
    dates = dates_in(date_node.get_text(' ', strip=True))
    if len(dates) != 1:
        raise ValueError('Ambiguous or missing visible publication date')
    author = soup.select_one('.date-text .name') or (module.select_one('.user .name') if module else None)
    title = ' '.join(headline.get_text().split())
    if not title:
        raise ValueError('Missing article headline')
    images = []
    cover = soup.select_one('[data-module-name="article_cover"]')
    for region in [body, cover] if cover else [body]:
        for node in region.find_all('img', src=True):
            try:
                src = asset_url(node['src'], live_url(url))
                if src not in [x['url'] for x in images]:
                    images.append({'url': src, 'caption': node.get('alt', '').strip()})
            except ValueError:
                continue
    for node in body.select('script, style, iframe, form, .addthis_toolbox, .small_share, .comments, .share_buttons, .article-read-time'):
        node.decompose()
    for text in list(body.find_all(string=True)):
        # HTML source line wrapping must not split paragraphs or inline words.
        from bs4 import Comment
        if isinstance(text, Comment):
            text.extract()
        else:
            text.replace_with(re.sub(r'\s+', ' ', str(text)))
    for node in body.find_all(['br', 'p', 'div', 'li', 'h2', 'h3', 'blockquote']):
        if node.name == 'br':
            node.replace_with('\n')
        else:
            node.insert_before('\n')
            node.append('\n')
    paragraphs = [' '.join(p.split()) for p in body.get_text().splitlines() if p.strip()]
    if len(' '.join(paragraphs)) < 150:
        raise ValueError('Article text too short to distinguish from a preview')
    standfirst = soup.select_one('[data-module-name="article_heading"] h2') or (module.find('h2') if module else None)
    if standfirst:
        lead = ' '.join(standfirst.get_text().split())
        if lead and lead not in paragraphs:
            paragraphs.insert(0, lead)
    return {'id': iid, 'date': dates[0], 'title': title,
            'byline': author.get_text(' ', strip=True) if author else '',
            'paragraphs': paragraphs, 'images': images,
            'url': live_url(url), 'retrieved_url': retrieved,
            'source_sha256': hashlib.sha256(data).hexdigest(), 'parser_version': VERSION}


class Client:
    def __init__(self, root, stop, delay, wayback=True, refresh=False):
        self.root, self.stop = Path(root), stop
        self.root.mkdir(parents=True, exist_ok=True)
        self.delay, self.wayback, self.refresh = delay, wayback, refresh
        self.last, self.live_blocked = 0, False

    def request(self, url, *, asset=False):
        allowed_url(url, asset=asset)
        interval = max(self.delay, 6) if 'web.archive.org' in url else self.delay
        if self.stop.wait(max(0, interval - (time.monotonic() - self.last))):
            raise InterruptedError('Stopped by user')
        self.last = time.monotonic()
        request = urllib.request.Request(url, headers={'User-Agent': f'mttd-dl/{__version__} (article preservation)'})
        with urllib.request.build_opener(Redirects(asset)).open(request, timeout=35) as response:
            allowed_url(response.url, asset=asset)
            result = bytearray()
            while chunk := response.read(65536):
                if self.stop.is_set():
                    raise InterruptedError('Stopped by user')
                result.extend(chunk)
                if len(result) > (15 if asset else 3) * 1024 * 1024:
                    raise ValueError('CMS response exceeded size limit')
            return bytes(result), response.url

    def cached(self, item):
        path = self.root / 'articles' / (item['id'] + '.json')
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            raw = self.root / 'articles' / (item['id'] + '.html')
            if record.get('parser_version') == VERSION and record['id'] == item['id'] and raw.is_file() and sha(raw) == record['source_sha256']:
                return record
        except (OSError, ValueError, KeyError):
            pass
        return None

    def get(self, item):
        if not self.refresh:
            record = self.cached(item)
            if record:
                return record
        candidates = [] if self.live_blocked and self.wayback else [live_url(item['url'])]
        if self.wayback:
            candidates.extend(dict.fromkeys(x for x in (item.get('verified_capture'), item['capture']) if x))
        errors = []
        for candidate in candidates:
            try:
                data, retrieved = self.request(candidate)
                record = parse_article(data, item['url'], retrieved)
                raw = self.root / 'articles' / (item['id'] + '.html')
                raw.parent.mkdir(exist_ok=True)
                temp = raw.with_suffix('.tmp')
                temp.write_bytes(data)
                os.replace(temp, raw)
                atomic_json(raw.with_suffix('.json'), record)
                return record
            except (OSError, ValueError) as exc:
                if isinstance(exc, InterruptedError):
                    raise
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403) and 'web.archive.org' not in candidate:
                    self.live_blocked = True
                errors.append(str(exc))
        raise ValueError('; '.join(errors))

    def image(self, image, article):
        from PIL import Image
        url = asset_url(image['url'], article['url'])
        token = hashlib.sha256(url.encode()).hexdigest()
        dest = self.root / 'images' / (token + '.png')
        metadata = dest.with_suffix('.json')
        try:
            if not self.refresh and dest.exists() and json.loads(metadata.read_text())['sha256'] == sha(dest):
                return str(dest)
        except (OSError, ValueError, KeyError):
            pass
        candidates = [url]
        match = re.match(r'https://web.archive.org/web/(\d{14})id_/', article['retrieved_url'])
        if self.wayback and match:
            candidates.append(f'https://web.archive.org/web/{match[1]}id_/{url}')
        errors = []
        for candidate in candidates:
            try:
                data, retrieved = self.request(candidate, asset=True)
                if asset_url(retrieved, url) != url:
                    raise ValueError('Image redirect changed asset')
                with Image.open(io.BytesIO(data)) as pic:
                    pic.load()
                    dest.parent.mkdir(exist_ok=True)
                    temp = dest.with_suffix('.tmp')
                    pic.convert('RGB').save(temp, format='PNG')
                    os.replace(temp, dest)
                atomic_json(metadata, {'url': url, 'retrieved_url': retrieved, 'sha256': sha(dest)})
                return str(dest)
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                if isinstance(exc, InterruptedError):
                    raise
                errors.append(str(exc))
        raise ValueError('Image unavailable: ' + '; '.join(errors))


def run_gap(library, years, months, only_date, delay, stop, emit, *, wayback=True):
    """Resolve all unknown dates once, then assemble selected days using verified dates."""
    shared = cache_path().parent / 'gap'
    shared.mkdir(parents=True, exist_ok=True)
    # Cross-library cache writes are serialized as well as destination state writes.
    with library_lock(library.state_dir / 'run.lock'), library_lock(shared / 'run.lock'):
        library.rows = json.loads(library.state_file.read_text()) if library.state_file.exists() else {}
        catalog = load_catalog()
        client = Client(shared, stop, delay, wayback, library.refresh)
        groups = defaultdict(list)
        unresolved, known_failures = [], defaultdict(list)
        library.selected_keys = getattr(library, 'selected_keys', set())
        emit(f'Gap HTML: checking {len(catalog)} known article candidates. Unknown dates are resolved from pages, not URL hints.')
        emit('First run may take hours; successful articles are cached across folders and resume automatically.')
        report_path = library.state_dir / 'gap' / 'discovery.json'
        processed = 0
        try:
            for item in catalog:
                if stop.is_set():
                    raise InterruptedError('Stopped by user')
                cached = client.cached(item)
                date = cached['date'] if cached else item.get('verified_date')
                if date and not selected(date, years, months, only_date) and not library.refresh:
                    processed += 1
                    continue
                try:
                    article = client.get(item)
                    date = article['date']
                    if selected(date, years, months, only_date):
                        groups[date].append(article)
                except (OSError, ValueError) as exc:
                    if isinstance(exc, InterruptedError):
                        raise
                    failure = {'id': item['id'], 'url': live_url(item['url']), 'error': str(exc)}
                    if date:
                        if selected(date, years, months, only_date):
                            known_failures[date].append(failure)
                    else:
                        unresolved.append(failure)
                    emit(f'Gap article {item["id"]}: unavailable; recorded for retry.')
                processed += 1
                if processed % 25 == 0:
                    emit(f'Gap HTML: {processed}/{len(catalog)} candidates checked, {sum(map(len, groups.values()))} selected articles.')
        finally:
            atomic_json(report_path, {'catalog_size': len(catalog), 'processed': processed,
                'years': years, 'months': months, 'date': only_date, 'interrupted': stop.is_set(),
                'unresolved_dates': unresolved, 'known_failures': dict(known_failures),
                'coverage': 'Known public article inventory; not exhaustive printed-issue coverage'})
        incomplete = bool(unresolved or known_failures)
        for date in sorted(set(groups) | set(known_failures)):
            if stop.is_set():
                raise InterruptedError('Stopped by user')
            iid = 'cms-' + hashlib.sha256(('MaltaToday:' + date).encode()).hexdigest()[:12]
            key = date + '@' + iid
            library.selected_keys.add(key)
            library.publications[iid] = 'MaltaToday'
            old = library.rows.get(key, {})
            articles = sorted(groups[date], key=lambda a: int(a['id']))
            warnings = list(known_failures[date])
            if unresolved:
                warnings.append({'url': 'Gap inventory', 'error': f'{len(unresolved)} candidate publication dates remain unresolved; additional articles may be missing. See discovery.json.'})
            folder = library.state_dir / 'gap' / iid
            folder.mkdir(parents=True, exist_ok=True)
            if not articles:
                # Do not lose a previously generated PDF during a failed refresh.
                if old.get('path') and Path(old['path']).is_file():
                    old['last_attempt_error'] = 'No articles retrieved; previous PDF retained'
                    atomic_json(folder / 'last-attempt.json', {'date': date, 'warnings': warnings})
                else:
                    library.rows[key] = dict(status='failed', path='', publication='MaltaToday',
                        source_type='cms_html', error='No articles retrieved for this day', warnings=warnings,
                        detected_dates=[], manifest=str(folder / 'manifest.json'))
                    atomic_json(folder / 'manifest.json', {'date': date, 'articles': [], 'warnings': warnings})
                library.save()
                continue
            for article in articles:
                for image in article['images']:
                    try:
                        image['path'] = client.image(image, article)
                    except (OSError, ValueError) as exc:
                        if isinstance(exc, InterruptedError):
                            raise
                        image.pop('path', None)
                        warnings.append({'url': image['url'], 'error': str(exc)})
            manifest = {'date': date, 'date_basis': 'website publication date',
                        'source_type': 'cms_html', 'articles': articles, 'warnings': warnings,
                        'discovery_report': str(report_path)}
            signature = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            oldpath = Path(old.get('path') or '/nonexistent')
            if not library.refresh and old.get('content_signature') == signature and oldpath.is_file() and sha(oldpath) == old.get('sha256'):
                emit(f'{key}: skipped, daily article collection unchanged.')
                incomplete |= bool(warnings)
                continue
            part = folder / 'edition.part'
            try:
                render_pdf(part, date, 'MaltaToday', articles, warnings,
                           collection_title='Web articles by publication date',
                           collection_note='Articles published online on this day, reconstructed from surviving CMS pages. This is not a complete printed newspaper issue. Dates are taken from article publication metadata.')
                check = library._inspect(part, None)
                if not check['pages']:
                    raise ValueError('Daily PDF failed structural verification')
                status = 'legacy_partial' if warnings else 'reconstructed'
                if warnings:
                    pending = library.state_dir / 'pending'
                    pending.mkdir(exist_ok=True)
                    dest = pending / library.final(key).name
                else:
                    dest = library.final(key)
                dest.parent.mkdir(parents=True, exist_ok=True)
                replaced = library.state_dir / 'replaced'
                if dest.exists():
                    replaced.mkdir(exist_ok=True)
                    dest.rename(replaced / f'{iid}-{time.time_ns()}.pdf')
                os.replace(part, dest)
                if oldpath != dest and oldpath.is_file() and sha(oldpath) == old.get('sha256'):
                    replaced.mkdir(exist_ok=True)
                    oldpath.rename(replaced / f'{iid}-{time.time_ns()}.pdf')
                atomic_json(folder / 'manifest.json', manifest)
                library.record(key, dest, dict(check, status=status, source_type='cms_html',
                    date_basis='website publication date', date_label=date, selection_year=date[:4],
                    method='CMS HTML text preserved; not a verified printed issue', article_count=len(articles),
                    warnings=warnings, content_signature=signature, manifest=str(folder / 'manifest.json')), articles[0]['url'])
                if library.rows.get(date, {}).get('status') == 'unavailable':
                    del library.rows[date]
                    library.save()
                emit(f'{key}: {status}, {len(articles)} articles, {check["pages"]} pages.')
                incomplete |= bool(warnings)
            finally:
                part.unlink(missing_ok=True)
        if not groups:
            emit('No articles retrieved for the selected dates; see .mttd-dl/gap/discovery.json.')
        return 3 if incomplete else 4 if not groups else 0
