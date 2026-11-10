"""Public legacy HTML archives, with source-preserving searchable reconstructions.

No authentication, script execution, or inferred article URLs. Follow dated links
from publisher indexes. Wayback is an optional public preservation fallback.
"""
import datetime as dt
import hashlib
import http.client
import io
import json
import os
import re
import time
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup

from .core import sha
from .platform_support import library_lock

HOSTS = {'archive.maltatoday.com.mt': 'MaltaToday',
         'archive.businesstoday.com.mt': 'BusinessToday'}
ROOTS = ['https://archive.maltatoday.com.mt/archive.html',
         'https://archive.businesstoday.com.mt/archives_new/archives.html']
DATE_PATH = re.compile(r'^/(?:midweek/)?(20\d{2})/(\d{2})/?(\d{2})/')
INDEX_NAMES = {'index.html', 'index.htm', 'top_news.html', 'top.html', 'news.html',
               'opinion.html', 'letters.html', 'culture.html', 'sports.html'}
RECONSTRUCTION_VERSION = 1


def canonical(url, base=None):
    url = urljoin(base or '', url)
    # Undo Wayback link rewriting; never follow an arbitrary archived host.
    match = re.match(r'https?://web\.archive\.org/web/\d+[a-z_]*/(https?://.+)', url)
    if match:
        url = match[1]
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or p.username or p.password or p.port not in (None, 80, 443):
        raise ValueError('Not a public legacy publisher URL')
    host = p.hostname
    aliases = {'www.maltatoday.com.mt': 'archive.maltatoday.com.mt',
               'maltatoday.com.mt': 'archive.maltatoday.com.mt',
               'www.businesstoday.com.mt': 'archive.businesstoday.com.mt'}
    host = aliases.get(host, host)
    if host not in HOSTS or '..' in unquote(p.path).split('/'):
        raise ValueError('Not a supported legacy archive host')
    return urlunsplit(('https', host, quote(p.path or '/', safe="/%:@!$&'()*+,;=-._~"), '', ''))


def issue_identity(url):
    url = canonical(url)
    match = DATE_PATH.match(urlsplit(url).path)
    if not match:
        return None
    date = dt.date(*map(int, match.groups())).isoformat()
    if date >= '2013-09-01':
        return None
    base = urlunsplit(('https', urlsplit(url).hostname, match[0], '', ''))
    return base, date, HOSTS[urlsplit(url).hostname]


def links(html, base):
    result = []
    for node in BeautifulSoup(html, 'html.parser').find_all(['a', 'area'], href=True):
        if not node['href'].strip() or node['href'].startswith('#'):
            continue
        try:
            url = canonical(node['href'], base)
        except ValueError:
            continue
        if url not in result:
            result.append(url)
    return result


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = quote(newurl, safe=":/%?=&+#@!$'()*+,;~-._")
        allowed_request(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def allowed_request(url):
    p = urlsplit(url)
    if p.hostname == 'web.archive.org' and p.scheme == 'https' and p.port in (None, 443) and not p.username:
        match = re.fullmatch(r'/web/\d+[a-z_]*/(https?://.+)', p.path)
        if match:
            canonical(match[1])
            return
    canonical(url)


class Client:
    def __init__(self, cache, stop, delay=2, wayback=True, refresh=False):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.stop, self.delay, self.wayback, self.refresh = stop, delay, wayback, refresh
        self.last = 0

    def request(self, url):
        url = quote(url, safe=":/%?=&+#@!$'()*+,;~-._")
        allowed_request(url)
        if self.stop.wait(max(0, self.delay - (time.monotonic() - self.last))):
            raise InterruptedError('Stopped by user')
        self.last = time.monotonic()
        req = urllib.request.Request(url, headers={'User-Agent': 'mttd-dl/0.8.1 (legacy preservation)'})
        with urllib.request.build_opener(Redirects()).open(req, timeout=35) as response:
            allowed_request(response.url)
            data = bytearray()
            while chunk := response.read(65536):
                if self.stop.is_set():
                    raise InterruptedError('Stopped by user')
                data.extend(chunk)
                if len(data) > 40 * 1024 * 1024:
                    raise ValueError('Legacy response exceeds 40 MB')
            if not data:
                raise ValueError('Empty legacy response')
            return bytes(data), response.url

    def get(self, url):
        url = canonical(url)
        token = hashlib.sha256(url.encode()).hexdigest()
        path = self.cache / (token + '.data')
        meta = self.cache / (token + '.json')
        if not self.refresh and path.exists() and meta.exists():
            record = json.loads(meta.read_text())
            if sha(path) == record['sha256']:
                return path.read_bytes(), record['retrieved_url']
        candidates = [url]
        if self.wayback:
            # Historical snapshot nearest the end of the HTML archive era.
            candidates.append('https://web.archive.org/web/20130101000000id_/' + url.replace('https://', 'http://', 1))
            # Old publisher URLs predate the archive subdomain; use the same observed path.
            candidates.append('https://web.archive.org/web/20130101000000id_/' + url.replace('https://archive.', 'http://www.', 1))
        errors = []
        for candidate in candidates:
            try:
                data, retrieved = self.request(candidate)
                if canonical(retrieved) != url:
                    raise ValueError('Redirect changed the requested legacy page')
                if any(s in data[:8000].lower() for s in
                       (b'<title>just a moment', b'<title>access denied', b'wayback machine has not archived')):
                    raise ValueError('Archive returned an error/challenge page')
                temp = path.with_suffix('.tmp')
                temp.write_bytes(data)
                os.replace(temp, path)
                meta.write_text(json.dumps({'url': url, 'retrieved_url': retrieved,
                                            'sha256': sha(path)}, indent=2))
                return data, retrieved
            except (OSError, ValueError, http.client.HTTPException) as exc:
                if isinstance(exc, InterruptedError):
                    raise
                errors.append(str(exc))
        raise ValueError(f'{url}: ' + '; '.join(errors))


def discover(client, years, months, only_date, emit):
    """Traverse linked month indexes, including older month-name filenames."""
    wanted_years = set(years) | {m[:4] for m in months}
    if only_date:
        wanted_years.add(only_date[:4])
    result, errors = {}, []
    for root in ROOTS:
        try:
            html, _ = client.get(root)
            pages = []
            root_links = links(html, root)
            for url in root_links:
                ident = issue_identity(url)
                if ident:
                    base, date, publication = ident
                    selected = date == only_date if only_date else date[:4] in years or date[:7] in months
                    if selected:
                        result.setdefault(base, (date, publication, url))
            for url in root_links:
                p = urlsplit(url).path
                if re.search(r'/archives?(?:_new)?/', p) and any(y in p for y in wanted_years):
                    match = re.search(r'/(20\d{2})/(\d{2})\.html$', p)
                    if match:
                        month = '-'.join(match.groups())
                        if only_date and month != only_date[:7]:
                            continue
                        if not only_date and month[:4] not in years and month not in months:
                            continue
                    pages.append(url)
            if not pages and not any(issue_identity(u) and (issue_identity(u)[1][:4] in wanted_years) for u in root_links):
                errors.append(f'{root}: no linked indexes for requested years')
            for page in pages:
                try:
                    body, _ = client.get(page)
                    for url in links(body, page):
                        ident = issue_identity(url)
                        if not ident:
                            continue
                        base, date, publication = ident
                        selected = date == only_date if only_date else date[:4] in years or date[:7] in months
                        if selected:
                            result.setdefault(base, (date, publication, url))
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    if isinstance(exc, InterruptedError):
                        raise
                    errors.append(str(exc))
        except (OSError, ValueError, http.client.HTTPException) as exc:
            if isinstance(exc, InterruptedError):
                raise
            errors.append(str(exc))
    for error in errors:
        emit('Legacy discovery warning: ' + error)
    return result, errors


def extract_article(html, url):
    """Prefer publisher body classes; conservative table-cell fallback for older pages."""
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'form', 'iframe', 'noscript']):
        tag.decompose()
    bodies = soup.select('.Body_inside, .body_inside, .bodytext, .body_new, article, .article-body')
    bodies = [n for n in bodies if len(n.get_text(' ', strip=True)) > 150
              and 'PRINT THIS ARTICLE' not in n.get_text(' ', strip=True)[:30]]
    # Body classes also occur on whole layout cells: keep leaf content blocks.
    bodies = [n for n in bodies if n.name not in ('td', 'table') and
              not any(child is not n and n in child.parents for child in bodies)]
    confidence = 'publisher-body-class'
    if bodies:
        region = bodies[0].find_parent('td') or bodies[0].parent
    else:
        candidates = []
        for node in soup.find_all(['td', 'article', 'main']):
            if node.find(['td', 'article', 'main']):
                continue
            text = node.get_text(' ', strip=True)
            linked = sum(len(a.get_text(' ', strip=True)) for a in node.find_all('a'))
            if len(text) >= 350 and linked / len(text) < .18:
                candidates.append((len(text) - linked * 4, node))
        if not candidates:
            return None
        region = max(candidates, key=lambda pair: pair[0])[1]
        bodies = [region]
        confidence = 'table-cell-heuristic'
    title_node = next((n for n in region.select('.headers_front234, .headline, .header_new, h1, h2')
                       if n.get_text(' ', strip=True)), None)
    if not title_node:
        headings = region.find_all(['strong', 'b'])
        title_node = max(headings, key=lambda n: len(n.get_text(' ', strip=True)), default=None)
    title = ' '.join(title_node.get_text().split()) if title_node else ''
    if not title or len(title) > 300:
        return None
    byline_node = next((n for n in region.select('.Byline, .byline')
                        if n.get_text(' ', strip=True) and not n.get_text(' ', strip=True).startswith(('PRINT', 'More on'))), None)
    byline = byline_node.get_text(' ', strip=True) if byline_node else ''
    standfirst = region.select_one('.Standfirst, .standfirst')
    paragraphs = []
    for body in bodies:
        # Parse a copy, preserving paragraph/BR boundaries but not layout markup.
        copy = BeautifulSoup(str(body), 'html.parser')
        for tag in copy.select('.Byline, .byline, .headers_front234, .headline, .header_new, h1, h2'):
            tag.decompose()
        for text in list(copy.find_all(string=True)):
            text.replace_with(re.sub(r'\s+', ' ', str(text)))
        for tag in copy.find_all(['br', 'p', 'div', 'li']):
            if tag.name == 'br':
                tag.replace_with('\n')
            else:
                tag.insert_before('\n')
                tag.append('\n')
        for line in copy.get_text().splitlines():
            line = ' '.join(line.split())
            if line and line != title and not re.match(r'^(PRINT THIS ARTICLE|Any comments\?|Bookmark|Copyright)', line, re.I):
                paragraphs.append(line)
    if standfirst:
        lead = standfirst.get_text(' ', strip=True)
        if lead and lead not in paragraphs and not lead.startswith('PRINT'):
            paragraphs.insert(0, lead)
    if len(' '.join(paragraphs)) < 150:
        return None
    images = []
    for image in region.find_all('img', src=True):
        try:
            src = canonical(image['src'], url)
            if issue_identity(src) == issue_identity(url) and '/pix/' in src and not re.search(r'cover|logo|banner|advert', src, re.I):
                images.append({'url': src, 'caption': image.get('alt', '').strip()})
        except ValueError:
            pass
    return {'title': title, 'byline': byline, 'paragraphs': paragraphs,
            'url': url, 'images': images, 'extraction': confidence}


def collect(client, index, emit):
    base, date, publication = issue_identity(index)
    queue, seen, articles, failures, pdfs = [index], set(), [], [], []
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        if len(seen) >= 300:
            failures.append({'url': url, 'error': 'Issue crawl exceeded 300 pages'})
            break
        seen.add(url)
        emit(f'Legacy {date}: reading {url.rsplit("/", 1)[-1]}')
        try:
            html, retrieved = client.get(url)
            page_links = links(html, url)
            for target in page_links:
                if issue_identity(target) != (base, date, publication):
                    continue
                path = urlsplit(target).path
                if path.endswith(('.html', '.htm')) and target not in seen and target not in queue:
                    queue.append(target)
                elif re.search(r'/(?:full_edition|edition_\d+|issue_\d+|election_special_\d+)\.pdf$', path):
                    if target not in pdfs:
                        pdfs.append(target)
            article = extract_article(html, url)
            filename = urlsplit(url).path.rsplit('/', 1)[-1]
            if article and filename not in {'index.html', 'index.htm', 'top_news.html', 'top.html', 'news.html'}:
                article['retrieved_url'] = retrieved
                article['source_sha256'] = hashlib.sha256(html).hexdigest()
                if not any(a['paragraphs'] == article['paragraphs'] for a in articles):
                    articles.append(article)
            elif filename not in INDEX_NAMES:
                failures.append({'url': url, 'error': 'No reliable article body extracted'})
        except (OSError, ValueError, http.client.HTTPException) as exc:
            if isinstance(exc, InterruptedError):
                raise
            failures.append({'url': url, 'error': str(exc)})
    return articles, failures, pdfs, sorted(seen)


def render_pdf(destination, date, publication, articles, failures, *, collection_title="Legacy article collection", collection_note=None):
    """Native Unicode text, embedded fonts, linked contents, images and source URLs."""
    from importlib.resources import as_file, files
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Image
    fonts = files('mttd_dl').joinpath('fonts')
    for name, filename in [('Legacy', 'NotoSans-Regular.ttf'), ('LegacyBold', 'NotoSans-Bold.ttf')]:
        with as_file(fonts.joinpath(filename)) as path:
            pdfmetrics.registerFont(TTFont(name, str(path)))
    body = ParagraphStyle('LegacyBody', fontName='Legacy', fontSize=10, leading=15, spaceAfter=8)
    title = ParagraphStyle('LegacyTitle', parent=body, fontName='LegacyBold', fontSize=21, leading=27, spaceAfter=18)
    small = ParagraphStyle('LegacySmall', parent=body, fontSize=8, leading=11, textColor=colors.HexColor('#526070'), splitLongWords=True)
    heading = ParagraphStyle('LegacyHeading', parent=title, fontSize=16, leading=22)
    story = [Paragraph(escape(publication), title), Paragraph(date, heading),
             Paragraph(escape(collection_title), heading),
             Paragraph(escape(collection_note or 'Reconstructed from surviving HTML articles. This is not a facsimile of the printed newspaper; advertisements, print-only material and missing web pages may be absent.'), body),
             Paragraph(f'{len(articles)} articles. {len(failures)} collection warnings. Searchable text is preserved directly from HTML; photographs are not OCR-transcribed.', body)]
    if failures:
        story.append(Paragraph('Incomplete collection - review the source manifest.', heading))
    story.extend([Spacer(1, 12), Paragraph('Contents', heading)])
    for i, article in enumerate(articles):
        story.append(Paragraph(f'<link href="#article-{i}" color="#164b70">{i+1}. {escape(article["title"])}</link>', body))
    for i, article in enumerate(articles):
        story.extend([PageBreak(), Paragraph(f'<a name="article-{i}"/>{escape(article["title"])}', heading)])
        if article['byline']:
            story.append(Paragraph(escape(article['byline']), small))
        for image in article.get('images', []):
            if image.get('path'):
                reader = ImageReader(image['path'])
                width, height = reader.getSize()
                scale = min(1, 470 / width, 270 / height)
                story.append(Image(image['path'], width=width*scale, height=height*scale))
                if image['caption']:
                    story.append(Paragraph(escape(image['caption']), small))
                story.append(Spacer(1, 8))
        story.extend(Paragraph(escape(p), body) for p in article['paragraphs'])
        story.extend([Spacer(1, 12), Paragraph('Source: ' + escape(article['url']), small)])
        if article['retrieved_url'] != article['url']:
            story.append(Paragraph('Archived copy: ' + escape(article['retrieved_url']), small))
    if failures:
        story.extend([PageBreak(), Paragraph('Collection warnings', heading)])
        for failure in failures:
            story.append(Paragraph(escape(failure['url'] + ': ' + failure['error']), small))
    def footer(canvas, doc):
        canvas.setFont('Legacy', 8)
        canvas.setFillColor(colors.HexColor('#526070'))
        canvas.drawString(48, 28, f'{publication} | {date} | Reconstructed HTML collection')
        canvas.drawRightString(547, 28, str(doc.page))
    doc = SimpleDocTemplate(str(destination), pagesize=(595,842), rightMargin=48,
                            leftMargin=48, topMargin=48, bottomMargin=48,
                            title=f'{publication} {date} - {collection_title}', author=publication)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def run_legacy(library, years, months, only_date, delay, stop, emit, *, wayback=True, seeds=()):
    """Use the library lock and naming/state model, without calling reconstructed dates verified."""
    from .core import inspect_pdf
    client = Client(library.state_dir / 'legacy' / 'cache', stop, delay, wayback, library.refresh)
    with library_lock(library.state_dir / 'run.lock'):
        library.rows = json.loads(library.state_file.read_text()) if library.state_file.exists() else {}
        issues, discovery_errors = ({}, []) if seeds else discover(client, years, months, only_date, emit)
        for seed in seeds:
            ident = issue_identity(seed)
            if not ident:
                raise ValueError('Legacy URL must contain a supported issue date before September 2013')
            base, date, publication = ident
            issues[base] = (date, publication, canonical(seed))
        (library.state_dir / 'legacy').mkdir(parents=True, exist_ok=True)
        run_report = {'issues': len(issues), 'discovery_errors': discovery_errors}
        (library.state_dir / 'legacy' / 'discovery.json').write_text(json.dumps(run_report, indent=2))
        emit(f'Legacy: selected {len(issues)} dated HTML issue indexes.')
        library.selected_keys = getattr(library, 'selected_keys', set())
        incomplete = bool(discovery_errors)
        for base, (date, publication, index) in sorted(issues.items()):
            if stop.is_set():
                raise InterruptedError('Stopped by user')
            iid = 'legacy-' + hashlib.sha256(base.encode()).hexdigest()[:12]
            key = f'{date}@{iid}'
            library.selected_keys.add(key)
            library.publications[iid] = publication
            # An existing original PDF for this publication/date takes precedence.
            if any(k[:10] == date and r.get('publication') == publication and
                   r.get('source_type') != 'legacy_html' and
                   r.get('status') in ('verified', 'verified_pdf', 'accepted_by_user') and
                   Path(r.get('path') or '/nonexistent').is_file() and sha(r['path']) == r.get('sha256')
                   for k, r in library.rows.items()):
                emit(f'{date} {publication}: original PDF already saved; skipped reconstruction.')
                continue
            old = library.rows.get(key, {})
            if not library.refresh and old.get('status') in ('reconstructed', 'accepted_by_user') and old.get('path') and Path(old['path']).is_file() and sha(old['path']) == old.get('sha256'):
                emit(f'{key}: skipped, searchable reconstruction already saved.')
                continue
            articles, failures, pdfs, visited = collect(client, index, emit)
            folder = library.state_dir / 'legacy' / iid
            folder.mkdir(parents=True, exist_ok=True)
            part = folder / 'edition.part'
            # Explicitly linked full-edition PDFs outrank reconstruction, never cover PDFs.
            native = None
            for pdf_url in pdfs:
                try:
                    data, retrieved = client.get(pdf_url)
                    part.write_bytes(data)
                    check = inspect_pdf(part, date)
                    if check['pages'] > 1 and check['status'] == 'verified':
                        native = (pdf_url, check)
                        break
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    if isinstance(exc, InterruptedError):
                        raise
            manifest = {'version': RECONSTRUCTION_VERSION, 'date': date, 'publication': publication,
                        'index': index, 'visited': visited, 'articles': articles, 'failures': failures,
                        'full_pdf_links': pdfs}
            if native:
                path = library.promote(key, part)
                library.record(key, path, dict(native[1], source_type='publisher_pdf'), native[0])
            elif articles:
                for article in articles:
                    if article['extraction'] != 'publisher-body-class':
                        failures.append({'url': article['url'], 'error': 'Older layout extracted heuristically; review required'})
                    for image in article['images']:
                        try:
                            from PIL import Image
                            data, _ = client.get(image['url'])
                            with Image.open(io.BytesIO(data)) as pic:
                                pic.load()
                                path = folder / (hashlib.sha256(image['url'].encode()).hexdigest()[:16] + '.png')
                                pic.convert('RGB').save(path)
                            image['path'] = str(path)
                        except (OSError, ValueError, Image.DecompressionBombError) as exc:
                            if isinstance(exc, InterruptedError):
                                raise
                            failures.append({'url': image['url'], 'error': 'Image unavailable: ' + str(exc)})
                render_pdf(part, date, publication, articles, failures)
                check = library._inspect(part, None)
                if not check['pages']:
                    raise ValueError('Generated legacy PDF failed structural validation')
                status = 'legacy_partial' if failures else 'reconstructed'
                if failures:
                    pending = library.state_dir / 'pending'
                    pending.mkdir(exist_ok=True)
                    path = pending / library.final(key).name
                    if path.exists():
                        replaced = library.state_dir / 'replaced'
                        replaced.mkdir(exist_ok=True)
                        path.rename(replaced / f'{iid}-{time.time_ns()}.pdf')
                    os.replace(part, path)
                else:
                    path = library.promote(key, part)
                library.record(key, path, dict(check, status=status, source_type='legacy_html',
                    method='HTML text preserved; not a verified original newspaper',
                    article_count=len(articles), warnings=failures, manifest=str(folder / 'manifest.json')), index)
                incomplete |= bool(failures)
                emit(f'{key}: {status}, {len(articles)} articles, {check["pages"]} pages.')
            else:
                incomplete = True
                if old.get('path') and Path(old['path']).is_file():
                    old['last_attempt_error'] = 'No extractable legacy articles; previous PDF retained'
                    library.save()
                    emit(f'{key}: could not refresh; previous PDF retained.')
                    part.unlink(missing_ok=True)
                    continue
                library.rows[key] = dict(status='failed', source=index, publication=publication,
                                         path='', detected_dates=[], error='No extractable legacy articles', warnings=failures)
                library.save()
            if library.rows.get(key, {}).get('path') and library.rows.get(date, {}).get('status') == 'unavailable':
                del library.rows[date]
                library.save()
            current_path = library.rows.get(key, {}).get('path')
            old_path = Path(old.get('path') or '/nonexistent')
            if current_path and str(old_path) != current_path and old.get('status') == 'legacy_partial' and old_path.is_file() and sha(old_path) == old.get('sha256'):
                replaced = library.state_dir / 'replaced'
                replaced.mkdir(exist_ok=True)
                old_path.rename(replaced / f'{iid}-{time.time_ns()}.pdf')
            manifest_path = folder / 'manifest.json'
            temp = manifest_path.with_suffix('.tmp')
            temp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
            os.replace(temp, manifest_path)
            part.unlink(missing_ok=True)
        return 3 if incomplete else 0
