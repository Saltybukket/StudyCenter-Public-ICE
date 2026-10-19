"""Reconstructible local FTS5 cache; the study database remains authoritative."""
import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing
from datetime import date
from pathlib import Path

from markupsafe import Markup, escape

TYPES = {'': 'Alle Inhalte', 'item': 'Aufgaben & Termine', 'note': 'Persönliche Notizen',
         'pdf': 'PDFs', 'mail': 'Mails & Anhänge', 'web': 'Kursseiten', 'gitlab': 'GitLab-Dokumente'}
CODE = re.compile(r'\b([A-Za-z]{1,4})[-_ ]?(\d{1,3})\b')
DATE = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b')


def aliases(text):
    result = [''.join(match.groups()) for match in CODE.finditer(text)]
    for match in DATE.finditer(text):
        y, m, d, dd, mm, yy = match.groups()
        try:
            parsed = date(int(y or yy), int(m or mm), int(d or dd))
            result.append(parsed.strftime('%Y%m%d'))
        except ValueError:
            pass
    return ' '.join(result)


def expression(query):
    query = DATE.sub(lambda m: aliases(m.group()).strip() or m.group(), query)
    query = CODE.sub(lambda m: ''.join(m.groups()), query)
    tokens = re.findall(r'[^\W_]+', query, re.UNICODE)[:16]
    # No user-provided FTS operators or SQL. Codes/numbers are exact; words allow prefixes.
    return ' AND '.join('"' + token + '"' + ('' if any(c.isdigit() for c in token) else '*') for token in tokens)


def special_pattern(query):
    if CODE.fullmatch(query):
        letters, number = CODE.fullmatch(query).groups()
        return re.compile(r'\b' + re.escape(letters) + r'[-_ ]?' + re.escape(number) + r'\b', re.I)
    if DATE.fullmatch(query) or re.fullmatch(r'\d{8}', query):
        compact = aliases(query).strip() or query
        try:
            parsed = date.fromisoformat(compact)
            forms = [parsed.isoformat(), f'{parsed.day:02}.{parsed.month:02}.{parsed.year}', f'{parsed.day}.{parsed.month}.{parsed.year}',
                     f'{parsed.day:02}/{parsed.month:02}/{parsed.year}', compact]
            return re.compile('|'.join(re.escape(v) for v in forms))
        except ValueError:
            pass
    return None


def marked(text, pattern=None, full_text=None):
    if pattern:
        if full_text and (match := pattern.search(full_text)):
            text = ('… ' if match.start()>100 else '') + full_text[max(0, match.start()-100):match.end()+160] + (' …' if match.end()+160<len(full_text) else '')
        text = pattern.sub(lambda m: '\x01' + m.group() + '\x02', text.replace('\x01', '').replace('\x02', ''))
    return Markup(str(escape(text)).replace('\x01', '<mark>').replace('\x02', '</mark>'))


class SearchIndex:
    def __init__(self, db):
        self.db = db
        self.path = Path(db.path).with_name('search.sqlite3')
        self.lock = threading.Lock()
        self.dirty = threading.Event()
        self.dirty.set()
        self.message = 'Suchindex wird vorbereitet …'
        self.error = ''
        self.count = 0
        self.updates = 0
        db.on_change = self.dirty.set

    def records(self):
        # A single read snapshot keeps item claims, document pages and source metadata consistent.
        with self.db.connection() as con:
            con.execute('BEGIN')
            courses = {r['id']: dict(r) for r in con.execute('SELECT * FROM courses')}
            sources = {r['id']: dict(r) for r in con.execute('SELECT * FROM sources')}
            documents = [dict(r) for r in con.execute('SELECT * FROM documents ORDER BY fetched_at,id')]
            evidence = {}
            for row in con.execute('SELECT * FROM item_evidence'):
                evidence.setdefault(row['item_id'], []).append(dict(row))
            latest = {(d['source_id'], d['url']): d['id'] for d in documents}
            document_map = {d['id']: d for d in documents}
            for row in con.execute('SELECT * FROM items ORDER BY id'):
                value = json.loads(row['payload']) | json.loads(row['overrides'])
                course = courses.get(row['course_id'], {})
                proofs = [p for p in evidence.get(row['id'], []) if p['current']]
                own = row['identity'].startswith('note:')
                active = not row['ignored'] and course.get('active', True)
                if course.get('semester') and value.get('semester') and value['semester'] != course['semester']:
                    active = False
                when = value.get('end_date') or value.get('date')
                semester = course.get('semester', '')
                if re.fullmatch(r'\d{4}W', semester) and when and when < semester[:4] + '-08-01' and not {'date', 'end_date'} & json.loads(row['overrides']).keys():
                    active = False
                if not own and proofs and not any(sources[document_map[p['document_id']]['source_id']]['active'] for p in proofs):
                    active = False
                body = '\n'.join([value.get('description', ''), *(p['quote'] for p in proofs)])
                details = ' '.join(str(value.get(k) or '') for k in ('identity_key', 'date', 'end_date', 'time', 'end_time', 'group'))
                details += ' ' + ' '.join(document_map[p['document_id']]['title'] + ' ' + p['anchor'] for p in proofs)
                meta = {'course': course.get('title', 'Eigene Notiz' if own else 'TU-Mail / Dokument'),
                        'label': 'Persönliche Notiz' if own else {'task': 'Aufgabe', 'exam': 'Prüfung', 'event': 'Termin', 'registration': 'Anmeldung', 'notice': 'Hinweis'}[value['kind']],
                        'date': when, 'date_label': 'Datum', 'status': row['status'], 'ignored': bool(row['ignored']),
                        'source_state': value['source_state'], 'proofs': len(proofs)}
                yield 'item:' + str(row['id']), [('item', row['id'], 0, 'note' if own else 'item', row['course_id'], int(active), value['title'], body, details + ' ' + meta['course'], meta)]
            page_proofs = {}
            for proofs in evidence.values():
                for proof in proofs:
                    page_proofs.setdefault((proof['document_id'], proof['page']), []).append(proof)
            for document in documents:
                source = sources[document['source_id']]
                course = courses.get(source['course_id'], {})
                current = document['mime'] == 'message/rfc822' or latest[(document['source_id'], document['url'])] == document['id']
                active = source['active'] and course.get('active', True) and current and document['analysis_state'] != 'archived'
                mail = json.loads(document['mail_metadata'])
                category = 'pdf' if document['mime'] == 'application/pdf' else 'mail' if document['mime'] == 'message/rfc822' else 'gitlab' if source['kind'] == 'gitlab' else 'web'
                meta = {'course': course.get('title', source['title']), 'label': {'pdf': 'PDF', 'mail': 'TU-Mail', 'gitlab': 'GitLab', 'web': 'Kursseite / Text'}[category],
                        'date': document['published_at'] or document['fetched_at'], 'date_label': 'Publiziert' if document['published_at'] else 'Gespeichert',
                        'sender': mail.get('sender', ''), 'mime': document['mime'], 'source_kind': source['kind'],
                        'current': current, 'archived': document['analysis_state'] == 'archived',
                        'hash': document['hash'], 'revision': document['revision'], 'repository_path': document['repository_path']}
                details = ' '.join([document['repository_path'], document['url'], mail.get('subject', ''), mail.get('sender', ''),
                                    mail.get('filename', ''), source['title'], meta['course'], meta['date'] or ''])
                pages = json.loads(document['pages']) or [{'page': 1, 'anchor': '', 'text': ''}]
                rows = []
                for page in pages:
                    body = page['text']
                    proofs = page_proofs.get((document['id'], page['page']), [])
                    body += '\n' + '\n'.join(p['quote'] for p in proofs if p['quote'] not in body)
                    rows.append(('document', document['id'], page['page'], category, source['course_id'], int(bool(active)), document['title'], body, details,
                                 {**meta, 'anchor': page.get('anchor', ''), 'gaps': bool(page.get('gaps')), 'has_text': bool(body.strip())}))
                yield 'document:' + str(document['id']), rows

    def refresh(self):
        with self.lock:
            if not self.dirty.is_set():
                return
            self.dirty.clear()  # A change during this snapshot keeps the next refresh pending.
            self.error = ''
            self.message = 'Suchindex wird aufgebaut / aktualisiert …'
            try:
                with closing(sqlite3.connect(self.path, timeout=30)) as con, con:
                    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(kind UNINDEXED,entity_id UNINDEXED,page UNINDEXED,category UNINDEXED,course_id UNINDEXED,active UNINDEXED,title,body,details,meta UNINDEXED,tokenize='unicode61 remove_diacritics 2')")
                    con.execute('CREATE TABLE IF NOT EXISTS entities(key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)')
                    old = dict(con.execute('SELECT key,fingerprint FROM entities'))
                    seen = set()
                    self.updates = 0
                    for key, rows in self.records():
                        seen.add(key)
                        fingerprint = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                        if old.get(key) == fingerprint:
                            continue
                        kind, identifier = key.split(':')
                        con.execute('DELETE FROM search WHERE kind=? AND entity_id=?', (kind, int(identifier)))
                        for row in rows:
                            clean = [str(v).replace('\x01', '').replace('\x02', '') for v in row[6:9]]
                            row = (*row[:6], clean[0], clean[1], clean[2] + ' ' + aliases(' '.join(clean)), json.dumps(row[9], ensure_ascii=False))
                            con.execute('INSERT INTO search VALUES(?,?,?,?,?,?,?,?,?,?)', row)
                        con.execute('INSERT OR REPLACE INTO entities VALUES(?,?)', (key, fingerprint))
                        self.updates += 1
                        self.message = f'Suchindex: {len(seen)} Einträge / Dokumente geprüft …'
                    for key in old.keys() - seen:
                        kind, identifier = key.split(':')
                        con.execute('DELETE FROM search WHERE kind=? AND entity_id=?', (kind, int(identifier)))
                        con.execute('DELETE FROM entities WHERE key=?', (key,))
                    self.count = con.execute('SELECT count(*) FROM search').fetchone()[0]
                    self.message = f'{self.count} Einträge / Seiten indexiert'
            except Exception as error:
                self.dirty.set()
                self.error = f'Suchindex nicht verfügbar ({type(error).__name__}): {error}'
                self.message = self.error
                raise

    def search(self, query, course='', content='', history=False, page=1, documents_only=False):
        self.refresh()
        match = expression(query)
        if not match:
            return [], 0, 1
        conditions, args = ['search MATCH ?'], [match]
        if documents_only:
            conditions.append("kind='document'")
        if not history:
            conditions.append('active=1')
        if course:
            conditions.append('course_id=?')
            args.append(int(course))
        if content in ('mail', 'gitlab'):
            conditions.append("json_extract(meta,'$.source_kind')=?")
            args.append(content)
        elif content:
            conditions.append('category=?')
            args.append(content)
        with self.lock, closing(sqlite3.connect(self.path)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("""SELECT *,snippet(search,7,char(1),char(2),' … ',40) excerpt,
                       highlight(search,6,char(1),char(2)) heading,bm25(search,0,0,0,0,0,0,8,2,0.5,0) score
                       FROM search WHERE """ + ' AND '.join(conditions) + " ORDER BY CASE kind WHEN 'item' THEN 0 ELSE 1 END,active DESC,score,entity_id,page", args).fetchall()
        groups = {}
        pattern = special_pattern(query)
        for row in rows:
            result = dict(row)
            meta = json.loads(result.pop('meta'))
            key = ('item', row['entity_id']) if row['kind'] == 'item' else ('document', meta['hash'], row['course_id'], row['active'])
            if key not in groups:
                result.update(meta=meta, excerpt=marked(row['excerpt'], pattern, row['body'] if pattern else None),
                              heading=marked(row['heading'], pattern), pages=[], matches=0)
                result['url'] = f"/items/{row['entity_id']}" if row['kind'] == 'item' else f"/documents/{row['entity_id']}?page={row['page']}"
                groups[key] = result
            result = groups[key]
            result['matches'] += 1
            if row['kind'] == 'document' and len(result['pages']) < 3:
                link = {'page': row['page'], 'url': f"/documents/{row['entity_id']}?page={row['page']}"}
                if link not in result['pages']:
                    result['pages'].append(link)
        found = list(groups.values())
        page = max(1, min(page, max(1, (len(found)+19)//20)))
        return found[(page-1)*20:page*20], len(found), page
