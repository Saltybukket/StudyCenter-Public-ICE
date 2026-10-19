import asyncio
import base64
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .browser import LoginRequired, authenticated_html
from .db import encode, now
from .documents import content_root

MAX_BYTES = 32 * 1024 * 1024
SKIP = ("logout", "login", "delete", "remove", "enrol", "unenrol", "edit.php", "sesskey=", "calendar/", "message/", "grade/", "user/", "completion")


def safe_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Nur HTTP(S)-URLs ohne Zugangsdaten sind erlaubt")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def mime_type(content_type, raw, url):
    if raw.startswith(b"%PDF-"):
        return "application/pdf"
    if "html" in content_type or raw.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return "text/html"
    if "text/plain" in content_type:
        return "text/plain"
    raise ValueError(f"Dateityp wird noch nicht gelesen: {content_type or urlsplit(url).path}")


def in_scope(url, source):
    target, root = urlsplit(url), urlsplit(source["url"])
    config = json.loads(source["config"])
    if url in config.get('excluded_urls', []):
        return False
    if target.netloc != root.netloc:
        return False
    if any(word in url.lower() for word in SKIP):
        return False
    if source["kind"] == "public":
        prefix = config.get("scope") or root.path.rsplit("/", 2)[0] + "/"
        if root.hostname == 'cloud.tugraz.at':
            share = re.fullmatch(r'/index\.php/s/([A-Za-z0-9_-]+)/?', root.path)
            if share and target.path.rstrip('/') == '/public.php/dav/files/' + share[1]:
                return True  # The exact same file share's server-advertised download.
        # Explicitly linked same-origin PDFs may live in a shared CMS upload directory.
        return target.path.startswith(prefix) or target.path.lower().endswith('.pdf')
    if target.path.endswith("/course/view.php"):
        return parse_qs(target.query).get("id") == parse_qs(root.query).get("id")
    if target.path.endswith("/course/section.php"):
        return True  # Only section links reached from this course's content enter the queue.
    if "/pluginfile.php/" in target.path or "/mod/" in target.path:
        # Edges originate in the selected course; links to other courses are excluded above.
        return bool(re.search(r"/(view|discuss)\.php$", target.path) or "/pluginfile.php/" in target.path)
    return False


def links_from(html, url, source, priority_urls=None):
    soup = BeautifulSoup(html, "html.parser")
    main = content_root(soup)
    config = json.loads(source['config'])
    if source['kind'] == 'teachcenter' and config.get('forum_since'):
        for row in list(main.select('[data-region="discussion-list-item"]')):
            dates = [node['datetime'][:10] for node in row.select('time[datetime]')]
            if dates and max(dates) < config['forum_since']:
                row.decompose()
    if source['kind'] == 'teachcenter' and config.get('exclude_archives'):
        for heading in list(main.select('.sectionname')):
            if re.match(r'(?i)^Archiv\b', heading.get_text(' ', strip=True)):
                section = heading.find_parent(class_='course-section')
                if section:
                    section.decompose()
    result, preferred = [], []
    nodes = list(main.select('a[href], iframe[src], object[data], embed[src]'))
    if source['kind'] == 'public' and urlsplit(source['url']).hostname == 'cloud.tugraz.at':
        state = soup.select_one('#initial-state-core-public-page-menu')
        if state:
            try:
                menu = json.loads(base64.b64decode(state.get('value',''),validate=True))
                nodes += [entry for entry in menu if entry.get('id') == 'download' and entry.get('href')]
            except (ValueError, TypeError, KeyError):
                pass
    for node in nodes:
        if source['kind'] == 'teachcenter' and config.get('exclude_archives') and re.search(
                r'(?i)^[\s►◄→←]*Archiv\b|\bolder version\b', node.get_text(' ', strip=True)):
            continue
        locator = node.get("href") or node.get("src") or node.get("data")
        try:
            target = safe_url(urljoin(url, locator))
            if source['kind'] == 'teachcenter' and '/mod/' in urlsplit(target).path:
                parts = urlsplit(target)
                query = parse_qs(parts.query, keep_blank_values=True)
                query.pop('forceview', None)  # Activity navigation aliases, not different documents.
                if parts.path.endswith('/mod/forum/view.php'):
                    query.pop('o', None)  # Sort controls add no content; retain real pagination.
                if parts.path.endswith('/mod/forum/discuss.php') and query.get('d'):
                    query.pop('parent', None)  # Read the complete discussion once, not a last-post alias.
                target = urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(query,doseq=True),''))
        except ValueError:
            continue
        if target != url and in_scope(target, source) and target not in result:
            suffix = urlsplit(target).path.lower()
            if not suffix.endswith((".zip", ".tar", ".gz", ".c", ".cpp", ".jpg", ".png", ".mp4")):
                result.append(target)
                label = node.get_text(' ', strip=True) if hasattr(node, 'get_text') else ''
                if source['kind'] == 'teachcenter' and re.search(
                        r'(?i)\b(?:intro(?:duction|ductory)?|einführung|einfuehrung|syllabus|organisation|organization|organisational|organizational|kursinformation)\b', label):
                    preferred.append(target)
    if priority_urls is not None:
        priority_urls.extend(preferred)
    return sorted(result, key=lambda target: target not in preferred)


def archived_discussion(html, url, since):
    if not since or not urlsplit(url).path.endswith('/mod/forum/discuss.php'):
        return False
    main = content_root(BeautifulSoup(html, 'html.parser'))
    dates = [node['datetime'][:10] for node in main.select('time[datetime]')]
    return bool(dates and max(dates) < since)


@dataclass
class Fetched:
    url: str
    raw: bytes
    mime: str
    title: str


class PublicCollector:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=35, follow_redirects=False, headers={"User-Agent": "StudyCenter-ICE/0.1 (personal course reader)"})

    async def fetch(self, url, source):
        for _ in range(6):
            async with self.client.stream("GET", url) as response:
                if response.is_redirect:
                    redirect = safe_url(urljoin(url, response.headers["location"]))
                    if not in_scope(redirect, source):
                        raise ValueError("Weiterleitung verlässt den konfigurierten Kursbereich")
                    url = redirect
                    continue
                response.raise_for_status()
                raw = bytearray()
                async for block in response.aiter_bytes():
                    raw.extend(block)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Dateigrenze 32 MiB erreicht")
                data = bytes(raw)
                return Fetched(url, data, mime_type(response.headers.get("content-type", ""), data, url), urlsplit(url).path.rsplit("/", 1)[-1] or "Kursseite")
        raise ValueError("Zu viele Weiterleitungen")

    async def close(self):
        await self.client.aclose()


class TeachCenterCollector:
    def __init__(self, context):
        self.context = context

    async def fetch(self, url, source):
        # Downloads through the same authenticated context. HTML navigation uses a separate crawler tab.
        response = await self.context.request.get(url, timeout=35000)
        if not response.ok:
            raise ValueError(f"HTTP {response.status}")
        raw = await response.body()
        if len(raw) > MAX_BYTES:
            raise ValueError("Dateigrenze 32 MiB erreicht")
        mime = mime_type(response.headers.get("content-type", ""), raw, response.url)
        if urlsplit(response.url).netloc != urlsplit(source['url']).netloc:
            if '/mod/url/view.php' in urlsplit(url).path and authenticated_html(raw, response.url) is False and not any(p in urlsplit(response.url).path.lower() for p in ('/login', '/auth', '/sso')):
                # An explicitly linked external material becomes its own visible public source.
                return Fetched(safe_url(response.url), raw, mime, 'Verlinktes Kursmaterial')
            raise LoginRequired('Download wurde zur Anmeldung umgeleitet')
        if mime == "application/pdf":
            if urlsplit(response.url).netloc != urlsplit(source["url"]).netloc:
                raise LoginRequired("PDF-Download wurde zur Anmeldung umgeleitet")
            return Fetched(response.url, raw, mime, urlsplit(url).path.rsplit("/", 1)[-1])
        if mime == "text/plain":
            return Fetched(response.url, raw, mime, urlsplit(url).path.rsplit("/", 1)[-1])
        page = await self.context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            # Read collapsed DOM content and ordinary lazy loading. Do not click
            # section controls: Moodle can persist their state as TU preferences.
            await page.wait_for_timeout(600)
            previous = 0
            for _ in range(12):
                height = await page.evaluate("document.body.scrollHeight")
                await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                await page.wait_for_timeout(350)
                if height == previous:
                    break
                previous = height
            html = await page.content()
            if not authenticated_html(html, page.url):
                raise LoginRequired("Anmeldung nötig oder angemeldeter Kursinhalt nicht erkennbar")
            return Fetched(page.url, html.encode(), "text/html", await page.title())
        finally:
            await page.close()


def personal_course_links(html, page_url, teachcenter_url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('[data-region="coursesync"], [data-region="myoverview"]')
    if not main:
        return []
    courses = {}
    for link in main.select('a[href*="/course/view.php"]'):
        url = safe_url(urljoin(page_url, link['href']))
        external = parse_qs(urlsplit(url).query).get('id', [None])[0]
        if not external or urlsplit(url).netloc != urlsplit(teachcenter_url).netloc:
            continue
        heading = link.select_one('.card-title, .coursename') or link
        # Screen-reader favourite state and metadata are not part of the course title.
        heading = BeautifulSoup(str(heading), 'html.parser')
        for node in heading.select('[data-region="favourite-icon"], .sr-only, .card-subtitle'):
            node.decompose()
        title = heading.get_text(' ', strip=True)
        if title:
            courses[external] = {'external': external, 'title': title, 'url': url}
    return list(courses.values())


async def discover_courses(browser, db, settings):
    async with browser.crawl_context() as context:
        page = await context.new_page()
        discovered = set()
        try:
            await page.goto(urljoin(settings.teachcenter_url, "my/courses.php"), wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(1000)
            if not authenticated_html(await page.content(), page.url):
                raise LoginRequired("TeachCenter: Anmeldung nötig")
            if not await page.locator('[data-region="coursesync"], [data-region="myoverview"]').count():
                # TC Main puts the personal coursesync block on the dashboard; courses.php is empty.
                await page.goto(urljoin(settings.teachcenter_url, 'my/'), wait_until='domcontentloaded', timeout=35000)
            await page.locator('[data-region="coursesync"] a[href*="/course/view.php"], [data-region="myoverview"] a[href*="/course/view.php"], [data-region="myoverview"] [data-region="empty-message"]').first.wait_for(timeout=15000)
            if not authenticated_html(await page.content(), page.url):
                raise LoginRequired('TeachCenter: Anmeldung nötig')
            # Read the user's existing view. Moodle grouping controls can persist
            # remote preferences; only the user may change them in the TU browser.
            seen_pages = set()
            limit_reached = True
            for _ in range(20):
                html = await page.content()
                soup = BeautifulSoup(html, "html.parser")
                courses = personal_course_links(html, page.url, settings.teachcenter_url)
                signature = tuple(c['external'] for c in courses)
                if signature in seen_pages:
                    limit_reached = False
                    break
                seen_pages.add(signature)
                for course in courses:
                    external, title, url = course['external'], course['title'], course['url']
                    discovered.add(external)
                    existing = db.one("SELECT id FROM courses WHERE external_id=?", ("tc:" + external,))
                    if existing:
                        course_id = existing["id"]
                    else:
                        course_id = db.execute("INSERT INTO courses(title,external_id,active) VALUES(?,?,0)", (title, "tc:" + external))
                    db.execute("INSERT OR IGNORE INTO sources(course_id,kind,title,url) VALUES(?,?,?,?)", (course_id, "teachcenter", title, url))
                next_button = page.locator('[data-action="next"]:not([disabled]), a[rel="next"], .pagination .page-item:not(.disabled) a[aria-label="Nächste Seite"], .pagination .page-item:not(.disabled) a[aria-label="Next"]')
                if not await next_button.count() or not await next_button.first.is_visible():
                    limit_reached = False
                    break
                await next_button.first.click()
                await page.wait_for_timeout(500)
            if limit_reached:
                raise ValueError("Kursliste nach 20 Seiten unvollständig; URLs können manuell ergänzt werden")
            if not discovered:
                raise ValueError('Keine persönlichen Kurskarten erkannt; Kursliste prüfen statt Vollständigkeit anzunehmen')
            return len(discovered)
        finally:
            await page.close()


async def crawl(source, collector, db, store):
    config = json.loads(source["config"])
    state = json.loads(source["crawl_state"])
    queue = state.get("queue") or [source["url"]]
    visited = set(state.get("visited", [])) if state.get("queue") else set()
    errors, fetched, deferred = [], 0, []
    limit = max(1, min(200, int(config.get("max_pages", 30))))
    pause = max(0.3, min(10, float(config.get("pause", 1))))
    login_error = None
    while queue and fetched < limit:
        queue.sort(key=lambda target: target not in config.get('priority_urls', []))
        url = queue.pop(0)
        if url in visited or not in_scope(url, source):
            continue
        try:
            document = await collector.fetch(url, source)
            if urlsplit(document.url).netloc != urlsplit(source['url']).netloc and source['kind'] == 'teachcenter':
                prefix = urlsplit(document.url).path
                prefix = prefix if prefix.endswith('/') else prefix
                db.execute('INSERT OR IGNORE INTO sources(course_id,kind,title,url,config) VALUES(?,?,?,?,?)',
                           (source['course_id'], 'public', source['title'] + ' · verlinkte Materialien', document.url,
                            encode({'scope': prefix, 'max_pages': 15, 'max_analysis_documents': 3})))
                visited.add(url)
                fetched += 1
                continue  # The external original and its descendants use their own public collector next run.
            store.save(source["id"], url, document.title, document.mime, document.raw)
            if document.mime == "text/html":
                if source['course_id'] is not None and urlsplit(source['url']).hostname != 'cloud.tugraz.at':
                    main = content_root(BeautifulSoup(document.raw, 'html.parser'))
                    for link in main.select('a[href]'):
                        try:
                            external = safe_url(urljoin(document.url,link['href']))
                        except ValueError:
                            continue
                        parts = urlsplit(external)
                        cloud_share = parts.hostname == 'cloud.tugraz.at' and re.fullmatch(r'/index\.php/s/[A-Za-z0-9_-]+/?',parts.path)
                        if parts.netloc != urlsplit(source['url']).netloc and (parts.path.lower().endswith('.pdf') or cloud_share):
                            prefix = parts.path.rstrip('/') if cloud_share else parts.path.rsplit('/',1)[0]+'/'
                            db.execute('INSERT OR IGNORE INTO sources(course_id,kind,title,url,config) VALUES(?,?,?,?,?)',
                                (source['course_id'],'public',link.get_text(' ',strip=True) or 'Verlinkte PDF',external,
                                 encode({'scope':prefix,'max_pages':3,'max_analysis_documents':1})))
                if source['kind'] == 'teachcenter' and '/mod/url/view.php' in urlsplit(url).path:
                    main = content_root(BeautifulSoup(document.raw, 'html.parser'))
                    for link in main.select('a[href]'):
                        try:
                            external = safe_url(urljoin(document.url,link['href']))
                        except ValueError:
                            continue
                        parts = urlsplit(external)
                        if parts.netloc == urlsplit(source['url']).netloc or any(word in external.lower() for word in SKIP):
                            continue
                        prefix = parts.path
                        db.execute('INSERT OR IGNORE INTO sources(course_id,kind,title,url,config) VALUES(?,?,?,?,?)',
                            (source['course_id'],'public',source['title']+' · '+link.get_text(' ',strip=True),external,
                             encode({'scope':prefix,'max_pages':15,'max_analysis_documents':3})))
                preferred = []
                links = links_from(document.raw, document.url, source, priority_urls=preferred)
                if preferred:
                    config['priority_urls'] = list(dict.fromkeys(preferred + config.get('priority_urls', [])))
                    db.execute('UPDATE sources SET config=? WHERE id=?', (encode(config), source['id']))
                for link in links:
                    if link not in visited and link not in queue:
                        queue.append(link)
            visited.add(url)
        except LoginRequired as error:
            deferred.append(url)
            login_error = str(error)
            break
        except Exception as error:
            deferred.append(url)
            errors.append(f"{url}: {type(error).__name__}: {str(error)[:180]}")
        fetched += 1
        # Durable cursor before the next external fetch, including during an interrupted process.
        db.execute("UPDATE sources SET crawl_state=? WHERE id=?", (encode({"queue": queue + deferred, "visited": sorted(visited)}), source["id"]))
        await asyncio.sleep(pause)
    remaining = list(dict.fromkeys(queue + deferred))
    complete = not remaining and not errors and not login_error
    message = login_error or ("Abrufgrenze erreicht; nächster Sync setzt fort." if queue else "\n".join(errors)) or f"{fetched} Dokumente abgerufen."
    db.execute("UPDATE sources SET state=?,message=?,crawl_state=? WHERE id=?", ("login_required" if login_error else "success" if complete else "partial", message, encode({"queue": remaining, "visited": sorted(visited)}) if remaining else "{}", source["id"]))
    if complete:
        db.execute("UPDATE sources SET last_success=? WHERE id=?", (now(), source["id"]))
    return {"source": source["id"], "complete": complete, "fetched": fetched, "remaining": len(remaining), "message": message}
