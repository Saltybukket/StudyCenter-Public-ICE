"""TU IMAP intake: EXAMINE and BODY.PEEK only; no mailbox mutations."""
import asyncio
import hashlib
import imaplib
import json
import re
import ssl
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime, parseaddr
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlencode

from .browser import LoginRequired
from .chatgpt import SecureJSONStore
from .config import local_profile_path
from .db import encode, now
from .documents import html_content
from bs4 import BeautifulSoup

MAX_BYTES = 32 * 1024 * 1024


class MailError(RuntimeError):
    pass


class MailCredentials:
    def __init__(self, path=None, *, data_dir=None):
        self.path = (Path(path) if path else local_profile_path('auth/mail.dpapi', data_dir=data_dir)).resolve()
        self.store = SecureJSONStore(self.path, service='mail')

    def read(self):
        try:
            return self.store.read()
        except Exception:
            raise MailError('Lokaler Mail-Zugang nicht lesbar; in StudyCenter-ICE erneut konfigurieren.') from None

    def write(self, values):
        self.store.write(values)

    def status(self):
        values = self.read()
        return {k: values.get(k, '') for k in ('host', 'username', 'address')} | {'configured': bool(values.get('password')), 'credential_storage': self.store.storage_status()}


def connect(values):
    host = str(values.get('host', '')).strip().lower()
    if not host or not (host == 'tugraz.at' or host.endswith('.tugraz.at')) or not re.fullmatch(r'[a-z0-9.-]+', host):
        raise MailError('IMAP-Host muss aus deiner TU-Kontokonfiguration stammen und zu tugraz.at gehören.')
    if not values.get('username') or not values.get('password'):
        raise LoginRequired('TU-Mailzugang fehlt; unter TU-Mail lokal konfigurieren. Eine Webmail-Sitzung ist kein IMAP-Zugang.')
    client = None
    try:
        client = imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context(), timeout=25)
        client.login(values['username'], values['password'])
        return client
    except imaplib.IMAP4.abort:
        raise MailError('TU-IMAP-Sitzung während der Anmeldung unterbrochen; keine Mailboxänderung.') from None
    except imaplib.IMAP4.error:
        if client:
            try:
                client.logout()
            except Exception:
                pass
        raise LoginRequired('TU-IMAP-Anmeldung abgewiesen; vorhandenen TU-Benutzernamen und Mailzugang lokal prüfen. Kein Postfachinhalt verändert.') from None
    except (OSError, ssl.SSLError):
        raise MailError('TU-IMAP über TLS/993 nicht erreichbar oder Sitzung unterbrochen. Vorhandene Daten bleiben erhalten.') from None


def folder_name(value):
    if not value or any(x in value for x in ('\r', '\n', '\x00')):
        raise MailError('Mailordner fehlt oder ist ungültig.')
    # IMAP modified UTF-7, including names of existing non-ASCII folders.
    import base64
    result, pending = [], []
    def flush():
        if pending:
            result.append('&' + base64.b64encode(''.join(pending).encode('utf-16-be')).decode().rstrip('=').replace('/', ',') + '-')
            pending.clear()
    for character in value:
        if ' ' <= character <= '~':
            flush()
            result.append('&-' if character == '&' else character)
        else:
            pending.append(character)
    flush()
    return '"' + ''.join(result).replace('\\', '\\\\').replace('"', '\\"') + '"'


def examine(client, folder):
    kind, _ = client.select(folder_name(folder), readonly=True)
    if kind != 'OK':
        raise MailError('Konfigurierter Mailordner nicht lesbar. Keine Ordner werden angelegt oder verändert.')
    _, result = client.response('UIDVALIDITY')
    if not result or not result[0] or not result[0].isdigit():
        raise MailError('UIDVALIDITY fehlt; kein unsicherer Mailcursor fortgeschrieben.')
    return result[0].decode()


def probe(values, folder):
    client = connect(values)
    try:
        examine(client, folder)
    finally:
        client.logout()  # Never CLOSE (which could expunge), STORE, MOVE or SMTP.


def parse(raw):
    return BytesParser(policy=policy.default).parsebytes(raw)


def message_pages(raw):
    message = parse(raw)
    body = []
    def read_part(part):
        if part.get_content_disposition() == 'attachment' or part.get_filename():
            return
        if part.get_content_type() == 'multipart/alternative':
            children = list(part.iter_parts())
            if not children:
                return
            plain = next((p for p in children if p.get_content_type() == 'text/plain'), None)
            read_part(plain or next((p for p in children if p.get_content_type() == 'text/html'), children[0]))
        elif part.is_multipart():
            for child in part.iter_parts():
                read_part(child)
        elif part.get_content_type() in ('text/plain', 'text/html'):
            content = part.get_content()
            if part.get_content_type() == 'text/html':
                content = html_content(content.encode('utf-8'), 'mail')[2][0]['text']
            body.append(content)
    read_part(message)
    headers = '\n'.join(f'{field}: {message[field]}' for field in ('Subject', 'From', 'Date', 'Message-ID', 'In-Reply-To') if message[field])
    return [{'page': 1, 'anchor': 'message', 'text': headers + '\n\n' + '\n\n'.join(body),
             'gaps': [] if any(s.strip() for s in body) else ['Nachricht enthält keinen lesbaren Text; Anhänge separat prüfen.']}]


def matches(text, terms):
    return any(re.search(r'(?<!\w)' + re.escape(term.strip()) + r'(?!\w)', text, re.I) for term in terms if term.strip())


def literal(result):
    return b''.join(part[1] for part in result if isinstance(part, tuple) and isinstance(part[1], bytes))


def save_mail(source, db, store, raw, host, folder, uid, validity, state, source_url=""):
    message = parse(raw)
    identity = str(message.get('Message-ID', '')).strip()
    if json.loads(source['config']).get('include_all'):
        existing = db.one("""SELECT d.* FROM documents d JOIN sources s ON s.id=d.source_id
            WHERE s.kind='mail' AND d.mime='message/rfc822' AND d.hash=?
            AND json_extract(d.mail_metadata,'$.message_id')=? ORDER BY d.id LIMIT 1""",
            (hashlib.sha256(raw).hexdigest(), identity)) if identity else None
        if existing:
            return existing['id']
        # Use an existing course source only when the full known course name is
        # explicit in the subject. Abbreviations/sender guesses do not assign a course.
        for known in db.sources():
            if known['kind']=='mail' and known['course_title'] and known['semester'] and matches(str(message.get('Subject','')), [known['course_title']]):
                source = db.source(known['id'])
                break
    references = re.findall(r'<[^<>]+>', str(message.get('References', '')))
    parent_id = str(message.get('In-Reply-To', '')).strip()
    parent = db.one("SELECT * FROM documents WHERE source_id=? AND mime='message/rfc822' AND json_extract(mail_metadata,'$.message_id')=? ORDER BY id DESC LIMIT 1", (source['id'], parent_id)) if parent_id else None
    root = (json.loads(parent['mail_metadata']).get('thread') if parent else None) or (references[0] if references else parent_id or identity) or hashlib.sha256(raw).hexdigest()
    thread = hashlib.sha256(root.encode()).hexdigest()
    # Same thread/role can retain identity across replies, folders and UID resets.
    # Publication order, not UID order, determines the latest claim.
    url = f"imap://{host}/course-{source['course_id']}/thread/{thread}"
    metadata = {'source_url':source_url, 'message_id': identity, 'thread': root, 'folder': folder, 'uid': uid,
                'uidvalidity': validity, 'subject': str(message.get('Subject', '')),
                'sender': str(message.get('From', '')), 'parents': [parent['id']] if parent else []}
    try:
        published = parsedate_to_datetime(str(message['Date'])).isoformat() if message['Date'] else None
    except (ValueError, TypeError):
        published = None
    doc_id = store.save(source['id'], url, str(message.get('Subject', 'TU-Kursmail')), 'message/rfc822', raw, published, mail_metadata=metadata)
    names = {}
    for index, part in enumerate(message.walk()):
        if part.is_multipart() or not (part.get_filename() or part.get_content_disposition() == 'attachment'):
            continue
        filename = part.get_filename() or f'Anhang-{index}'
        mime = part.get_content_type()
        if mime in ('application/pkcs7-signature','application/pgp-signature'):
            continue  # Cryptographic signatures contain no course document text.
        if filename.lower().endswith('.pdf'):
            mime = 'application/pdf'
        allowed = mime in ('application/pdf', 'text/plain', 'text/html', 'text/calendar', 'image/png', 'image/jpeg')
        if not allowed:
            label = f'{identity or thread}: {filename} ({mime})'
            if label not in state['unsupported']:
                state['unsupported'].append(label)
            continue
        attachment = part.get_payload(decode=True)
        if attachment is None:
            raise MailError('Mailanhang nicht dekodierbar; Cursor nicht übersprungen.')
        if mime == 'application/pdf' and not attachment.lstrip().startswith(b'%PDF-'):
            raise MailError('Anhang als PDF bezeichnet, enthält aber keine PDF; Cursor nicht übersprungen.')
        proof = metadata | {'parent_document': doc_id, 'filename': filename, 'charset':part.get_content_charset() or 'utf-8'}
        occurrence = names.get(filename, 0)
        names[filename] = occurrence + 1
        store.save(source['id'], url + '/attachment/' + quote(filename, safe='') + (f'/{occurrence}' if occurrence else ''), filename,
                   mime, attachment, published, mail_metadata=proof)
    return doc_id


def collect(source, db, store, credentials=None, client_factory=connect):
    values = (credentials or MailCredentials()).read()
    if not values:
        raise LoginRequired('TU-Mail noch nicht verbunden; Zugang direkt unter TU-Mail konfigurieren.')
    config = json.loads(source['config'])
    folder = config.get('folder', 'INBOX')
    terms = config.get('keywords', [])
    senders = {s.casefold().strip() for s in config.get('senders', []) if s.strip()}
    if not terms and not senders:
        raise MailError('Kursfilter fehlt; das ganze Postfach wird nicht an das Modell geschickt.')
    since = date.fromisoformat(config['since'])
    months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
    state = json.loads(source['crawl_state'])
    client = client_factory(values)
    downloaded = scanned = skipped = 0
    try:
        validity = examine(client, folder)
        reset = bool(state.get('uidvalidity') and state['uidvalidity'] != validity)
        if state.get('uidvalidity') != validity:
            state = {'uidvalidity': validity, 'last_uid': 0, 'unsupported': []}
        last = state.get('last_uid', 0)
        kind, found = client.uid('SEARCH', None, 'SINCE', f'{since.day:02}-{months[since.month-1]}-{since.year}', 'UID', f'{last+1}:*')
        if kind != 'OK':
            raise MailError('IMAP-Suche fehlgeschlagen; Mailcursor bleibt erhalten.')
        uids = sorted(int(uid) for uid in b' '.join(x for x in found if isinstance(x, bytes)).split() if int(uid) > last)
        budget = max(1, min(200, int(config.get('max_pages', 30))))
        batch = uids[:budget]
        for uid in batch:
            kind, envelope = client.uid('FETCH', str(uid), '(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID REFERENCES IN-REPLY-TO)])')
            if kind != 'OK' or not literal(envelope):
                raise MailError('Nachrichtenkopf nicht vollständig gelesen; Cursor nicht übersprungen.')
            header = parse(literal(envelope))
            subject_match = matches(str(header.get('Subject', '')), terms)
            sender_match = parseaddr(str(header.get('From', '')))[1].casefold() in senders
            if subject_match or sender_match:
                size = re.search(rb'RFC822.SIZE\s+(\d+)', b' '.join(p[0] for p in envelope if isinstance(p, tuple)))
                if not size or int(size[1]) > MAX_BYTES:
                    raise MailError('Relevante Nachricht fehlt in der Größenangabe oder überschreitet 32 MiB; UID nicht übersprungen.')
                kind, content = client.uid('FETCH', str(uid), '(BODY.PEEK[])')
                raw = literal(content)
                if kind != 'OK' or not raw:
                    raise MailError('Relevante Nachricht nicht vollständig geladen; nächster Sync versucht dieselbe UID.')
                if len(raw) != int(size[1]):
                    raise MailError('Nachrichtengröße stimmt nicht mit dem vollständigen Abruf überein; UID nicht übersprungen.')
                if len(raw) > MAX_BYTES:
                    raise MailError('Nachricht über 32 MiB; nicht ausgewertet.')
                save_mail(source, db, store, raw, values['host'], folder, uid, validity, state)
                downloaded += 1
            else:
                skipped += 1
            scanned += 1
            state['last_uid'] = uid
            db.execute('UPDATE sources SET crawl_state=? WHERE id=?', (encode(state), source['id']))
        remaining = len(uids) - scanned
        complete = remaining == 0 and not state['unsupported']
        message = f'{scanned} Nachrichtenköpfe geprüft · {downloaded} Kursmails vollständig geladen · {skipped} durch Kursfilter ausgeschlossen · {remaining} weitere ausstehend.'
        if reset:
            message += ' UIDVALIDITY geändert; Message-ID/Inhaltsversionen verhindern erneute Einträge.'
        if state['unsupported']:
            message += ' Nicht ausgewertete Anhänge: ' + '; '.join(state['unsupported'])
        db.execute('UPDATE sources SET crawl_state=?,state=?,message=?,last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE id=?',
                   (encode(state), 'success' if complete else 'partial', message, complete, now(), source['id']))
        return {'source': source['id'], 'complete': complete, 'headers_scanned': scanned,
                'messages_downloaded': downloaded, 'filtered_out': skipped, 'remaining_messages': remaining,
                'unsupported_attachments': state['unsupported'], 'uidvalidity_reset': reset, 'message': message}
    except (imaplib.IMAP4.error, OSError):
        raise MailError('TU-Mailabruf unterbrochen; zuletzt gespeicherte UID und bisherige Einträge bleiben erhalten.') from None
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


async def collect_mail(source, db, store, credentials=None):
    return await asyncio.to_thread(collect, source, db, store, credentials)


async def webmail_bootstrap(context, browser=None):
    response = await context.request.get('https://webmail.tugraz.at/imp/dynamic.php?page=mailbox', max_redirects=0, timeout=30000)
    text = await response.text()
    cache = browser.session_path.with_name(browser.session_path.stem+'-webmail.dpapi') if browser else None
    if response.status == 302 and urlsplit(response.headers.get('location','')).path == '/imp/basic.php' and cache and cache.exists():
        # View selection is session state, not authentication evidence. The next
        # AJAX request must still pass Horde's real token/session validation.
        try:
            return json.loads(dpapi(cache.read_bytes(),decrypt=True))
        except Exception:
            pass
    if response.status != 200 or 'DimpCore' not in text:
        raise LoginRequired('TU-Webmail-Sitzung fehlt oder ist abgelaufen; im eigenen TU-Browser erneut anmelden. Bestehende Einträge bleiben erhalten.')
    soup = BeautifulSoup(text, 'html.parser')
    javascript = '\n'.join(script.get_text() for script in soup.select('script:not([src])'))
    configs = {}
    for match in re.finditer(r'([A-Za-z_$][\w$.]*)\s*=\s*(?=\{)', javascript):
        try:
            value, _ = json.JSONDecoder().raw_decode(javascript[match.end():])
            if isinstance(value, dict):
                configs[match[1]] = value
        except ValueError:
            pass
    if cache:
        cache.parent.mkdir(parents=True,exist_ok=True)
        cache.write_bytes(dpapi(encode(configs).encode()))
    return configs


def viewport_result(result):
    value = result.get('tasks', {}).get('imp:viewport')
    if not isinstance(value, dict) or not isinstance(value.get('data', {}), (dict, list)):
        raise MailError('Webmail-Nachrichtenliste nicht unterstützt; keine Einträge oder Cursor verworfen.')
    data = value.get('data', {})
    if isinstance(data, list):
        if data:
            raise MailError('Webmail-Liste ohne eindeutige Nachrichtenkennung.')
        data = {}
    rows = value.get('rowlist', {})
    ordered = sorted(data, key=lambda key: int(rows.get(key, 0))) if isinstance(rows,dict) else list(data)
    return value, [(key, data[key]) for key in ordered]


async def webmail_folders(context, configs):
    import base64
    core = configs['HordeCore.conf']
    endpoint = urljoin('https://webmail.tugraz.at/',core['URI_AJAX'])+'listMailboxes'
    if urlsplit(endpoint).hostname!='webmail.tugraz.at' or not urlsplit(endpoint).path.endswith('/services/ajax.php/imp/listMailboxes'):
        raise MailError('Webmail-Ordnerliste nicht unterstützt.')
    # all + unsub only lists the tree. expall/toggleMailboxes would persist
    # expansion preferences and is deliberately absent (Horde Dynamic handler).
    response = await context.request.post(endpoint,form={'token':core['TOKEN'],'all':'1','unsub':'1'},
        headers={'X-Requested-With':'XMLHttpRequest'},max_redirects=0,timeout=30000)
    text = (await response.text()).strip()
    if response.status!=200:
        raise LoginRequired('Webmail-Ordnerliste nicht erreichbar; erneut manuell anmelden.')
    if text.startswith('/*') and text.endswith('*/'):
        text=text[2:-2].removeprefix('-secure-').strip()
    try:
        result=json.loads(text)
        rows=result['tasks']['imp:mailbox']['a']
        folders=[]
        for row in rows:
            if row.get('v') or row.get('n') or row.get('co'):
                continue
            encoded=row.get('m','')
            name=row.get('t') or base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)).decode('utf-8')
            if name and '\x00' not in name:
                folders.append(name)
        if not folders:
            raise ValueError('Keine Ordner')
        return sorted(set(folders),key=lambda f:(f!='INBOX',f))
    except (ValueError,KeyError,TypeError):
        raise MailError('Webmail-Ordnerliste unvollständig; keine vollständige Abdeckung behauptet.') from None


async def collect_webmail(source, context, db, store, browser=None):
    config = json.loads(source['config'])
    all_mail = bool(config.get('include_all'))
    queries = [('subject', t.strip()) for t in config.get('keywords', []) if t.strip()]
    queries += [('from', t.strip()) for t in config.get('senders', []) if t.strip()]
    if all_mail:
        # Empty quicksearch retains Horde's previous search. '@' instead selects
        # all actual email senders in a virtual search, without INBOX filterOnDisplay.
        queries = [('from','@')]
    if not queries or len(queries)>20:
        raise MailError('Ein bis zwanzig konkrete Kursfilter benötigt.')
    since = date.fromisoformat(config['since'])
    folders = config.get('folders') or [config.get('folder', 'INBOX')]
    configs = await webmail_bootstrap(context, browser)
    if all_mail and config.get('discover_folders'):
        folders = await webmail_folders(context,configs)
        if config.get('folders')!=folders:
            config['folders']=folders
            db.execute('UPDATE sources SET config=?,crawl_state=? WHERE id=?',(encode(config),'{}',source['id']))
            source=dict(source)|{'crawl_state':'{}'}
    state = json.loads(source['crawl_state'])
    state.setdefault('unsupported', [])
    state['unsupported'] = [v for v in state['unsupported'] if not v.endswith(('(application/pkcs7-signature)','(application/pgp-signature)'))]
    if all_mail:
        state.setdefault('document_ids', [])
    folder_index = state.get('folder_index',0)
    query_index, offset = state.get('query_index', 0), state.get('offset', 0)
    budget = max(1, min(100, int(config.get('max_pages', 20))))
    scanned = downloaded = filtered = 0
    unseen = checked = 0
    while folder_index<len(folders) and scanned<budget:
        folder = folders[folder_index]
        field, term = queries[query_index]
        size = min(20, budget-scanned)
        result = await webmail_search(context, configs, field, term, offset, size, folder)
        viewport, records = viewport_result(result)
        total = int(viewport.get('totalrows', 0))
        if not records and offset<total:
            raise MailError('Webmail-Suche hat eine Leselücke; Cursor bleibt für erneuten Abruf erhalten.')
        before = {key: record.get('flag', []) for key,record in records}
        for buid, record in records:
            if all_mail:
                try:
                    listed_date = datetime.strptime(record.get('date',''),'%d.%m.%Y').date()
                except (ValueError, TypeError):
                    listed_date = None
                if listed_date and listed_date < since:
                    # Only exclude a dated pre-cutoff envelope; unknown list dates
                    # still require the original. Flags are checked for the whole slice.
                    scanned += 1
                    filtered += 1
                    unseen += int(configs['DimpCore.conf'].get('FLAG_SEEN') not in before[buid])
                    continue
            if not str(buid).isdigit():
                raise MailError('Ungültige Webmail-Nachrichtenkennung.')
            query = urlencode({'actionID':'view_source', 'mailbox':configs['DimpCore.conf']['qsearchid'], 'buid':buid})
            url = 'https://webmail.tugraz.at/imp/view.php?' + query
            response = await context.request.get(url, max_redirects=0, timeout=30000)
            if response.status != 200:
                raise LoginRequired('Originalnachricht nicht verfügbar; TU-Sitzung erneut prüfen. Cursor bleibt erhalten.')
            raw = await response.body()
            if not raw or len(raw)>MAX_BYTES or 'text/html' in response.headers.get('content-type',''):
                raise MailError('Originalnachricht unvollständig oder über 32 MiB; nicht übersprungen.')
            mail = parse(raw)
            if not mail['From'] or not mail['Date']:
                raise MailError('Nachricht ohne überprüfbaren Absender/Zeitpunkt; Cursor nicht übersprungen.')
            try:
                published = parsedate_to_datetime(str(mail['Date'])).date()
            except (ValueError, TypeError):
                raise MailError('Nachrichtenzeitpunkt nicht überprüfbar.') from None
            sender = parseaddr(str(mail['From']))[1].casefold()
            relevant = all_mail or matches(str(mail.get('Subject','')),config.get('keywords',[])) or sender in {s.casefold() for s in config.get('senders',[])}
            if published>=since and relevant:
                # Webmail BUID is session scoped, never claimed as an IMAP UIDVALIDITY.
                doc_id = save_mail(source, db, store, raw, 'webmail.tugraz.at', folder, None, None, state, url)
                if all_mail:
                    linked = [doc_id] + [d['id'] for d in db.rows("SELECT id FROM documents WHERE json_extract(mail_metadata,'$.parent_document')=?",(doc_id,))]
                    state['document_ids'] = sorted(set(state['document_ids'] + linked))
                downloaded += 1
            else:
                filtered += 1
            scanned += 1
            unseen += int(configs['DimpCore.conf'].get('FLAG_SEEN') not in before[buid])
        # Practical countercheck: same temporary search, same slice, flags unchanged.
        after_view, after_records = viewport_result(await webmail_search(context,configs,field,term,offset,size,folder))
        after = {key:record.get('flag',[]) for key,record in after_records}
        if before != after:
            raise MailError('Nachrichtenliste/Flags während des Abrufs geändert; Cursor nicht fortgeschrieben, erneut prüfen.')
        checked += len(records)
        offset += len(records)
        if offset>=total:
            query_index += 1
            offset = 0
        if query_index>=len(queries):
            folder_index += 1
            query_index = 0
        state.update(query_index=query_index, offset=offset, folder_index=folder_index)
        db.execute('UPDATE sources SET crawl_state=? WHERE id=?',(encode(state),source['id']))
    finished = folder_index>=len(folders)
    if finished:
        state.update(query_index=0,offset=0,folder_index=0)
        if all_mail:
            state['collection_complete'] = True
    complete = finished and not state['unsupported']
    message = f'{scanned} Nachrichten geprüft · {downloaded} Mails ab {since} geladen · {filtered} ältere/unpassende ausgeschlossen · Lesestatus bei {checked} Nachrichten unverändert ({unseen} ungelesen). '
    message += 'Alle konfigurierten Ordner/Suchen gelesen.' if finished else f'Weitere Suchtreffer ausstehend ({len(folders)-folder_index} Ordner); nächster Sync setzt fort.'
    message += ' Nicht konfigurierte Unterordner und nicht abgerufene Thread-Vorgänger sind nicht enthalten.'
    if state['unsupported']:
        message += ' Nicht ausgewertete Anhänge: ' + '; '.join(state['unsupported'])
    db.execute('UPDATE sources SET crawl_state=?,state=?,message=?,last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE id=?',
        (encode(state),'success' if complete else 'partial',message,complete,now(),source['id']))
    if browser:
        await browser.save_session()
    return {'source':source['id'],'complete':complete,'messages_scanned':scanned,'messages_downloaded':downloaded,
            'filtered_out':filtered,'flags_checked':checked,'unseen_checked':unseen,'searches_pending':(len(folders)-folder_index)*len(queries)-query_index if not finished else 0,
            'unsupported_attachments':state['unsupported'],'message':message}


async def webmail_search(context, configs, field, term, offset=0, size=20, folder='INBOX'):
    import base64
    core, imp = configs.get('HordeCore.conf', {}), configs.get('DimpCore.conf', {})
    endpoint = urljoin('https://webmail.tugraz.at/', core.get('URI_AJAX', '')) + 'viewPort'
    if urlsplit(endpoint).hostname != 'webmail.tugraz.at' or not urlsplit(endpoint).path.endswith('/services/ajax.php/imp/viewPort') or not core.get('TOKEN') or not imp.get('qsearchid'):
        raise MailError('Webmail-Konfiguration nicht unterstützt; keine unbekannten Endpunkte verwendet.')
    viewport = {'view': imp['qsearchid'], 'qsearchmbox': base64.urlsafe_b64encode(folder.encode()).decode().rstrip('='),
                'qsearchfield': field, 'qsearch': term, 'slice': f'{offset+1}:{offset+size}'}
    # Virtual quicksearch bypasses INBOX filterOnDisplay. No applyfilter, sort or preview.
    response = await context.request.post(endpoint, form={'view':imp['qsearchid'], 'viewport':json.dumps(viewport), 'token':core['TOKEN']},
        headers={'X-Requested-With':'XMLHttpRequest'}, max_redirects=0, timeout=30000)
    if response.status != 200:
        if response.status in (301,302,303,307,308,401,403):
            raise LoginRequired('TU-Webmail-Suche zur Anmeldung umgeleitet oder Zugang abgewiesen. Vorhandene Einträge bleiben erhalten.')
        raise MailError(f'TU-Webmail-Leseprobe HTTP {response.status}; keine erneute Anmeldung behauptet.')
    text = await response.text()
    # Horde wraps JSON in a JavaScript comment to prevent JSON hijacking.
    text = text.strip()
    if text.startswith('/*') and text.endswith('*/'):
        text = text[2:-2].strip()
        if text.startswith('-secure-'):
            text = text[len('-secure-'):].strip()
    try:
        result = json.loads(text)
    except ValueError:
        raise MailError('Webmail-Suche liefert keine unterstützte JSON-Antwort.') from None
    if result.get('reload'):
        raise LoginRequired('TU-Webmail-Sitzung abgelaufen; erneut manuell anmelden.')
    if result.get('response') is False:
        raise LoginRequired('TU-Webmail hat die Suchanfrage abgewiesen; Sitzung/Anmeldung im TU-Browser prüfen. Keine Mailboxänderung.')
    return result
