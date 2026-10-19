import asyncio
import hashlib
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit

import portalocker
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .config import ROOT, local_profile_path
from pathlib import Path
from datetime import datetime, timezone


class LoginRequired(Exception):
    pass


class BrowserUnavailable(Exception):
    pass


SERVICE_URLS = {'teachcenter': 'https://tc.tugraz.at/main/my/',
                'tugrazonline': 'https://online.tugraz.at/tug_online/',
                'mail': 'https://webmail.tugraz.at/imp/dynamic.php?page=mailbox'}


def linux_system_chrome():
    """Use only an existing regular browser; its OS sandbox policy remains intact."""
    if sys.platform!='linux':return None
    for name in ('google-chrome-stable','google-chrome','chromium','chromium-browser'):
        path=shutil.which(name)
        if path and Path(path).is_file() and os.access(path,os.X_OK):return Path(path)
    return None


def session_signal(service, html, url, status=200):
    """A positive service-specific read or an actual login, never cookie age."""
    soup = BeautifulSoup(html, 'html.parser')
    address = urlsplit(url)
    if status >= 400 or address.scheme in ('chrome-error', 'edge-error'):
        return 'unreachable'
    login = soup.select_one('input[type=password], form#login')
    if login or address.hostname not in (urlsplit(SERVICE_URLS[service]).hostname,):
        return 'login_required' if login or address.hostname in ('sso.tugraz.at', 'idp.tugraz.at') else 'unknown'
    if any(p in address.path.lower() for p in ('/login', '/auth/', '/saml/', '/my.policy', '/vdesk/')):
        return 'login_required'
    if service == 'teachcenter':
        if authenticated_html(html, url) and not soup.select_one('a[href*="login/index.php"]'):
            return 'authenticated'
        if soup.select_one('a[href*="login/index.php"]') or 'Sie sind als Gast' in html:
            return 'login_required'
    elif service == 'tugrazonline':
        if soup.select_one('#id-ca-sidenav-logout, .profile-username') and any(
                text in soup.get_text(' ', strip=True) for text in ('Meine Lehrveranstaltungen', 'Mein Terminkalender')):
            return 'authenticated'
    elif service == 'mail' and 'DimpCore' in html:
        return 'authenticated'  # Additionally validate the real Horde read below.
    return 'unknown'


def readonly_mail_request(url, method='GET', body=None):
    """Allow Horde list reads, remove preview/preference commands, block mutations."""
    address = urlsplit(url)
    if address.hostname != 'webmail.tugraz.at':
        return True, body
    if '/services/ajax.php/' in address.path:
        if address.path.endswith('/listMailboxes'):
            values = dict(parse_qsl(body or '',keep_blank_values=True))
            return (method=='POST' and not address.query and set(values)<= {'token','all','unsub'} and values.get('all')=='1'),body
        if address.path.rsplit('/', 1)[-1] not in ('dynamicInit', 'viewPort'):
            return False, body
        if method != 'POST' or address.query:
            return False, body
        values = dict(parse_qsl(body or '', keep_blank_values=True))
        for key in ('msgload', 'sortby', 'sortdir', 'delhide', 'applyfilter', 'qsearchfilter'):
            values.pop(key, None)
        if 'viewport' in values:
            try:
                viewport = json.loads(values['viewport'])
                if not isinstance(viewport, dict):
                    return False, body
                for key in ('sortby', 'sortdir', 'delhide', 'msgload', 'applyfilter', 'qsearchfilter'):
                    viewport.pop(key, None)
                values['viewport'] = json.dumps(viewport)
            except (ValueError, TypeError):
                return False, body
        return True, urlencode(values)
    if address.path.endswith(('/imp/message.php', '/imp/basic.php', '/imp/compose.php', '/imp/view.php')):
        return False, body
    # TU Webmail's official F5 login posts to /my.policy and its SAML ACS.
    # This is the user's authentication, not an IMP mailbox operation.
    if method not in ('GET', 'HEAD') and not (address.path in ('/my.policy', '/saml/sp/profile/post/acs') or address.path.endswith('/login.php')):
        return False, body
    return True, body


async def readonly_mail_route(route, blocked=None):
    request = route.request
    allowed, body = readonly_mail_request(request.url, request.method, request.post_data)
    if not allowed:
        if blocked is not None:
            address = urlsplit(request.url)
            blocked.append({'path':address.path, 'method':request.method})
            del blocked[:-10]
        await route.abort()
    elif body != request.post_data:
        await route.continue_(post_data=body)
    else:
        await route.continue_()


def authenticated_html(html, url):
    soup = BeautifulSoup(html, "html.parser")
    if any(word in urlsplit(url).path.lower() for word in ("/login", "/sso", "/auth/")):
        return False
    if soup.select_one('input[type="password"],form#login'):
        return False
    # Actual Moodle user and course/dashboard content, never a cookie-only check.
    user = soup.select_one('.usermenu [data-userid], .usermenu .userbutton, a[href*="logout.php"], [data-region="user-menu"]')
    content = soup.select_one('#region-main, [data-region="course-content"], .course-content, [data-region="myoverview"]')
    return bool(user and content)


class BrowserService:
    def __init__(self, settings):
        self.settings = settings
        self.context = None
        self.playwright = None
        self.visible = False
        self.login_mode = False
        self.lock = asyncio.Lock()
        self.profile_lock = None
        self.last_error = ""
        self.login_pages = {}
        self.cleanup_task = None
        self.blocked_mail_requests = []
        self.sessions = {}
        self.profile_dir = Path(settings.data_dir).resolve()
        self.profile_dir.mkdir(parents=True,exist_ok=True)
        profile_key = hashlib.sha256(str(self.profile_dir).encode()).hexdigest()[:16]
        self.session_path = local_profile_path(f'auth/tu-session-{profile_key}.dpapi',data_dir=settings.data_dir)
        from .chatgpt import SecureJSONStore
        self.session_store = SecureJSONStore(self.session_path,service='browser-session')

    async def _ensure(self, visible=False):
        if self.context:
            return self.context
        if self.playwright or self.profile_lock:
            await self._close()
        components=Path(os.getenv('STUDYCENTER_ICE_COMPONENT_ROOT',str(ROOT/'.local'/'components'))).resolve()
        os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(components/'browsers')
        try:
            self.profile_lock = portalocker.Lock(str(self.profile_dir / "browser.owner.lock"), timeout=0)
            self.profile_lock.acquire()
            self.playwright = await async_playwright().start()
            options = {}
            # Crawls use Playwright's separate Headless Shell (no channel).
            # Branded browsers use full browser headless mode; Edge left stale
            # Windows Alt+Tab entries here even after all owned processes exited.
            # BROWSER_CHANNEL only selects the visible manual-login browser.
            channel = os.getenv('STUDYCENTER_ICE_BROWSER_CHANNEL', '') if visible and sys.platform=='win32' else ''
            if channel not in ('','chrome','msedge'):raise ValueError('Unbekannter sichtbarer Browserkanal.')
            if visible and sys.platform=='win32' and not channel and not Path(self.playwright.chromium.executable_path).exists():
                chrome = Path(os.environ.get('PROGRAMFILES', 'C:/Program Files') + '/Google/Chrome/Application/chrome.exe')
                if chrome.exists():
                    channel = 'chrome'
                elif Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)') + '/Microsoft/Edge/Application/msedge.exe').exists():
                    channel = 'msedge'
            if channel:
                options['channel'] = channel
            if sys.platform=='linux':
                regular=linux_system_chrome()
                if regular:options['executable_path']=str(regular)
            profile = self.profile_dir / 'browser-profile'
            if not channel or channel == 'chrome':
                profile = self.profile_dir / ('browser-chrome-profile' if channel else 'browser-chromium-profile')
            if profile.is_symlink() or not profile.resolve().is_relative_to(self.profile_dir):
                raise ValueError('Browserprofil muss im eigenen ausgewählten Datenverzeichnis bleiben.')
            profile.mkdir(parents=True,exist_ok=True)
            try:
                session=self.session_store.read()
                if isinstance(session,dict) and session.get('user_agent'):
                    options['user_agent']=session['user_agent']
            except Exception:
                session=None
                self.last_error='Lokale TU-Sitzungskopie nicht lesbar; im eigenen Browser erneut anmelden.'
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(profile), headless=not visible,
                chromium_sandbox=True,locale="de-AT", timezone_id="Europe/Vienna", accept_downloads=True, **options)
            self.context.set_default_timeout(15000)
            if session:
                try:
                    cookies = session['cookies'] if isinstance(session, dict) else session
                    await self.context.add_cookies(cookies)
                except Exception:
                    self.last_error = 'Lokale TU-Sitzungskopie nicht lesbar; im eigenen Browser erneut anmelden.'
            self.visible = visible
            context = self.context
            self.context.on("close", lambda *_: self._closed(context))
            self.last_error = ""
            return self.context
        except Exception:
            await self._close()
            self.last_error = "Browserprofil belegt oder Chromium nicht verfügbar. Kein fremder Prozess wurde beendet."
            self.last_error += ' Browserkomponente unter Einstellungen / Betrieb ausdrücklich herunterladen.'
            if sys.platform=='linux':
                self.last_error += ' Ein vorhandener Chrome/Chromium kann genutzt werden, sofern die aktuelle Linux-Sandbox und AppArmor-Regeln ihn zulassen; die App verändert diese Regeln nicht.'
            raise BrowserUnavailable(self.last_error) from None

    def _closed(self, context):
        if self.context is context:
            self.context = None
            self.visible = False
            self.login_mode = False
            self.cleanup_task = asyncio.create_task(self._release_closed())

    async def _release_closed(self):
        # A window closed by the user must also release its driver and owner lock.
        async with self.lock:
            if self.context is None:
                await self._close()

    async def _close(self):
        context, self.context = self.context, None
        self.visible = False
        self.login_mode = False
        self.login_pages.clear()
        try:
            if context:
                await self.save_session(context)
                await context.close()
        finally:
            try:
                if self.playwright:
                    driver, self.playwright = self.playwright, None
                    await driver.stop()
            finally:
                if self.profile_lock:
                    self.profile_lock.release()
                    self.profile_lock = None

    async def save_session(self, context=None):
        context = context or self.context
        if not context:
            return
        try:
            cookies = await context.cookies()
            # Only this app's TU profile; no normal browser or Codex auth files.
            cookies = [cookie for cookie in cookies if cookie['domain'].lstrip('.') == 'tugraz.at' or cookie['domain'].endswith('.tugraz.at')]
            if not cookies:
                return
            agent = await context.pages[0].evaluate('navigator.userAgent') if context.pages else ''
            self.session_store.write({'cookies':cookies,'user_agent':agent})
        except Exception:
            self.last_error = 'TU-Sitzungskopie nicht gespeichert; erneute manuelle Anmeldung kann nötig sein.'

    async def open_login(self, url=None):
        services = [key for key, address in SERVICE_URLS.items()
                    if not url and key != 'mail' or url and urlsplit(url).hostname == urlsplit(address).hostname]
        checked = await self.probe(services)
        needed = [key for key in services if checked[key]['state'] == 'login_required']
        if not needed:
            return checked
        async with self.lock:
            if self.context and not self.visible:
                await self._close()
            context = await self._ensure(visible=True)
            self.login_mode = True
            # Repeated login clicks reuse our visible tabs, never crawler pages.
            for url in (SERVICE_URLS[key] for key in needed):
                key = urlsplit(url).hostname
                previous = self.login_pages.get(key)
                if previous and not previous.is_closed():
                    await previous.bring_to_front()
                    continue
                page = await context.new_page()
                self.login_pages[key] = page
                if urlsplit(url).hostname == 'webmail.tugraz.at':
                    async def mail_route(route):
                        await readonly_mail_route(route, self.blocked_mail_requests)
                    await page.route('**/*', mail_route)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    self.last_error = "Anmeldeseite nicht vollständig geladen; im sichtbaren TU-Browser erneut versuchen."
            # Chromium's initial empty page is ours, not a user's normal browser tab.
            for page in list(context.pages):
                if page.url == 'about:blank' and page not in self.login_pages.values():
                    await page.close()
        return checked

    async def probe(self, services=None):
        services = list(services or SERVICE_URLS)
        if any(s not in SERVICE_URLS for s in services):
            raise ValueError('Unbekannter TU-Dienst.')
        checked = datetime.now(timezone.utc).isoformat()
        if self.lock.locked():
            return {s: {'state': 'busy', 'checked_at': checked, 'reason': 'App-Browser ist belegt.'} for s in services}
        async with self.lock:
            try:
                context = await self._ensure()
            except BrowserUnavailable:
                return {s: {'state': 'busy', 'checked_at': checked, 'reason': self.last_error} for s in services}
            results = {}
            for service in services:
                page = None
                state, reason = 'unknown', 'Angemeldeter Inhalt nicht eindeutig erkennbar.'
                try:
                    # One temporary read tab. Never navigate or close an unfinished login tab.
                    page = await context.new_page()
                    if service == 'mail':
                        await page.route('**/*', readonly_mail_route)
                    response = await page.goto(SERVICE_URLS[service], wait_until='domcontentloaded', timeout=18000)
                    if service == 'tugrazonline':
                        try:
                            await page.locator('#id-ca-sidenav-logout, .profile-username, input[type=password]').first.wait_for(timeout=8000)
                        except Exception:
                            pass
                    state = session_signal(service, await page.content(), page.url, response.status if response else 0)
                    if state == 'authenticated' and service == 'mail':
                        from .mail import webmail_bootstrap, webmail_search
                        config = await webmail_bootstrap(context, self)
                        await webmail_search(context, config, 'subject', '__studycenter_session_probe__', size=1)
                    reason = {'authenticated': 'Aktueller persönlicher Inhalt gelesen.', 'login_required': 'Tatsächliche Anmeldung angezeigt.',
                              'unreachable': 'Dienst nicht erreichbar.'}.get(state, reason)
                except LoginRequired:
                    state, reason = 'login_required', 'Dienst hat die lesende Sitzungsprüfung als abgelaufen abgewiesen.'
                except Exception as error:
                    state, reason = 'unreachable', 'Leseprobe nicht erreichbar (' + type(error).__name__ + ').'
                finally:
                    if page and not page.is_closed():
                        await page.close()
                results[service] = {'state': state, 'checked_at': checked, 'reason': reason}
            self.sessions.update(results)
            await self.save_session()
            # Hand off only if every visible login tab's service has been checked positively.
            visible_services = [key for key, value in SERVICE_URLS.items() if urlsplit(value).hostname in self.login_pages]
            if self.login_mode and visible_services and all(self.sessions.get(key, {}).get('state') == 'authenticated'
                    and key in results for key in visible_services):
                await self._close()
            elif not self.visible:
                await self._close()
            return results

    async def finish_login(self):
        async with self.lock:
            self.login_mode = False
            # Hand off the saved session to a headless crawler, rather than
            # opening every temporary course page in the user's login window.
            if self.visible:
                await self._close()

    @asynccontextmanager
    async def crawl_context(self):
        async with self.lock:
            # Session probes gate the service. Keep other, unfinished login tabs alive.
            context = await self._ensure()
            previous = set(context.pages)
            try:
                yield context
            finally:
                # Also remove popups and pages left behind by a failed collector.
                await asyncio.gather(*(page.close() for page in list(context.pages)
                                       if page not in previous and not page.is_closed()),
                                     return_exceptions=True)

    async def close(self):
        async with self.lock:
            await self._close()

    async def close_idle(self):
        async with self.lock:
            if not self.login_mode:
                await self._close()
