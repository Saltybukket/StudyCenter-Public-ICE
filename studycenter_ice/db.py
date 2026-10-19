import hashlib
import json
import re
import sqlite3
from urllib.parse import urlsplit
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def temporal_claim(value):
    """An explicitly quoted open window is not a personally scheduled exam start.

    Interpret the extracted metadata without rewriting the original evidence.
    A duration alone or an ordinary exam start/end must not trigger this repair.
    """
    if (value.get('kind') == 'exam' and value.get('date_meaning') == 'start'
            and value.get('date') and value.get('end_date')
            and re.search(r'\b(?:offen\s+von|open\s+from)\b', value.get('quote', ''), re.I)):
        return value | {'date_meaning': 'window'}
    return value


CLAIM_FIELDS = ('date', 'time', 'end_date', 'end_time', 'obligation', 'source_state', 'date_meaning')
INTERNAL_CONFLICT = re.compile(
    r'\b(?:Widerspruch\s+zwischen|widerspr[üu]chliche?\s+(?:Dat|Termin|Frist|Schlie[ßs]|Anmelde)|widersprechen\s+sich|abweichend(?:e|er|en)?\s+(?:Frist|Termin|Schlie[ßs]|Datums)|conflicting\s+(?:dat|deadline)|contradictory\s+(?:dat|deadline))', re.I)


def claims_conflict(claims):
    facts = [claim_facts(c) for c in claims]
    return any(len({encode(c[f]) for c in facts if f in c}) > 1
               for f in {key for c in facts for key in c})


def claim_facts(value):
    """Compare dates serving the same purpose, including closing-only copies."""
    value = temporal_claim(value)
    facts = {f:value[f] for f in ('obligation','source_state')
             if value.get(f) not in (None,'unclear','none')}
    kind, meaning = value.get('kind'), value.get('date_meaning')
    closing = (kind == 'task' and meaning in ('deadline','window')
               or kind in ('registration','exam') and meaning == 'window')
    if closing:
        if value.get('end_date'):
            roles = [('opening', 'date', 'time'), ('closing', 'end_date', 'end_time')]
        else:
            roles = [('closing', 'date', 'time')]
    else:
        roles = [('start', 'date', 'time'), ('end', 'end_date', 'end_time')]
    for role, day, clock in roles:
        if value.get(day):
            facts[role+'_date'] = value[day]
            if value.get(clock):
                facts[role+'_time'] = (value[day], value[clock])
    return facts


def governing_reference(value):
    text = ' '.join(value.get(f) or '' for f in ('quote','description','uncertainty'))
    authority = r'(?:maßgeblich|massgeblich|verbindlich(?:en|e|er)?|authoritative|governed\s+by)'
    source = r'(?:kalender|calendar|FAQ|Kursseite|course\s+page|https?://)'
    reference = r'(?:Termine|Fristen|dates|deadlines).{0,80}(?:siehe|entnehmen|laut|according\s+to).{0,80}'+source
    return text if re.search(authority+r'.{0,160}'+source+'|'+source+r'.{0,160}'+authority+'|'+reference, text, re.I) else ''


def effective_evidence(proofs, fallback, claims=None, context_lookup=None):
    """Resolve source facts without changing evidence, status or manual corrections.

    Publication establishes cross-source order. Retrieval time does not. Unknown
    publication dates, ties and conflicting statements in one source stay open.
    """
    claims = claims if claims is not None else [temporal_claim(json.loads(p['payload'])) for p in proofs]
    normalized = temporal_claim(fallback)
    facts = [claim_facts(c) for c in claims]
    if (len(claims) == 1 and claims[0] == normalized
            and not INTERNAL_CONFLICT.search(normalized.get('uncertainty') or '')):
        # Most saved entries have one unchanged assertion. Keep the same complete
        # role provenance without rebuilding and sorting a resolution graph.
        payload = dict(normalized)
        if 'closing_date' in facts[0]:
            payload['date_meaning'] = 'window' if payload.get('kind') in ('exam','registration') or 'opening_date' in facts[0] else 'deadline'
        p = proofs[0]
        citation = dict(document_id=p['document_id'],page=p['page'],quote=p['quote'],published_at=p.get('published_at'))
        return payload, False, False, {'superseded_fields':[], 'unresolved_fields':[],
            'internal_conflict':False, 'superseded_internal_conflicts':[],
            'extraction_ambiguities':[], 'facts':{field:[citation] for field in facts[0]}}
    ambiguous_policy = set()
    if context_lookup is not None:
        def sentence(index):
            p = proofs[index]
            text = ' '.join(context_lookup(p['document_id'],p['page']).split()).casefold()
            quote = ' '.join(p['quote'].split()).casefold()
            position = text.find(quote)
            if position < 0 or text.count(quote) != 1:return None
            start = text.rfind('.',0,position)+1
            previous = text.rfind('.',0,max(0,start-1))+1
            # Moodle places heading/navigation text before the first prose sentence
            # without punctuation. Retain the setup-policy sentence itself.
            prose = text.rfind('to ensure ',previous,start)
            start = prose if prose >= 0 else previous
            end = text.find('.',position+len(quote))
            return text[start:end+1 if end >= 0 else None].strip()
        for i,left in enumerate(proofs):
            for j in range(i+1,len(proofs)):
                right = proofs[j]
                if {facts[i].get('obligation'),facts[j].get('obligation')} != {'required','recommended'}:continue
                a,b = urlsplit(left.get('url') or ''),urlsplit(right.get('url') or '')
                if (left.get('kind') != 'teachcenter' or right.get('kind') != 'teachcenter'
                        or left.get('source_id') is None or left.get('source_id') != right.get('source_id')
                        or a.hostname != b.hostname or {a.path,b.path} != {'/main/course/view.php','/main/course/section.php'}
                        or len(left['quote']) < 30 or ' '.join(left['quote'].split()).casefold() != ' '.join(right['quote'].split()).casefold()):continue
                context = sentence(i)
                if context and context == sentence(j):ambiguous_policy.update((i,j))
    winners, unresolved, provenance, superseded = {}, [], {}, []
    for field in sorted({key for f in facts for key in f}, key=lambda f:f.endswith('_time')):
        asserted = [(i,f[field]) for i,f in enumerate(facts) if field in f]
        if field == 'obligation' and ambiguous_policy:
            remaining = [(i,v) for i,v in asserted if i not in ambiguous_policy]
            if len({v for _,v in remaining}) <= 1:
                # Identical native course/section text received two model labels.
                # Preserve the uncertainty instead of inventing a source verdict.
                winners[field] = 'unclear'
                provenance[field] = [dict(document_id=proofs[i]['document_id'],page=proofs[i]['page'],quote=proofs[i]['quote'],
                                          published_at=proofs[i].get('published_at')) for i,_ in asserted]
                continue
            asserted = remaining
        if field.endswith('_time') and field.replace('_time','_date') in winners:
            asserted = [(i,v) for i,v in asserted if v[0] == winners[field.replace('_time','_date')]]
        if not asserted:continue
        values = {encode(v) for _,v in asserted}
        selected = asserted
        if len(values) > 1:
            newest = [entry for entry in asserted if all(
                entry[0] == other[0] or proofs[entry[0]].get('document_id') == proofs[other[0]].get('document_id')
                or newer_publication(proofs[entry[0]].get('published_at'), proofs[other[0]].get('published_at'))
                for other in asserted)]
            if newest:
                references = [governing_reference(claims[i]) for i,_ in newest]
                if not any(references):
                    selected = newest
                else:
                    # Resolve only an exact retained URL/title reference. A vague
                    # "see calendar" cannot manufacture an authority relationship.
                    referenced = [(i,v) for i,v in asserted for p in [proofs[i]]
                                  if i not in {n for n,_ in newest} and any(
                        p.get('url') and p['url'] in ref
                        or len(p.get('title') or '') > 8 and p['title'].casefold() in ref.casefold()
                        for ref in references if ref)]
                    if len({proofs[i]['document_id'] for i,_ in referenced}) == 1:
                        selected = referenced
                if selected != asserted:superseded.append(field)
            if len({encode(v) for _,v in selected}) > 1:
                unresolved.append(field)
        if field not in unresolved:
            winners[field] = selected[-1][1]
            provenance[field] = [dict(document_id=proofs[i]['document_id'],page=proofs[i]['page'],
                                      quote=proofs[i]['quote'],published_at=proofs[i].get('published_at'))
                                 for i,_ in selected]
    # Prefer a compatible complete span so an opening is not lost when another
    # source gives only its closing deadline. Preserve the selected source text.
    compatible = [c for c,f in zip(claims,facts) if all(
        key not in winners or val == winners[key] for key,val in f.items())]
    if all(key not in winners or val == winners[key] for key,val in claim_facts(normalized).items()):
        compatible.insert(0,normalized)
    base = max(compatible, key=lambda c:sum(c.get(f) is not None for f in ('date','time','end_date','end_time')),
               default=normalized)
    payload = dict(base)
    for field in ('obligation','source_state'):
        if field in winners:payload[field] = winners[field]
    closing = 'closing_date' in winners
    if closing:
        opening = winners.get('opening_date')
        payload.update(date=opening or winners['closing_date'], end_date=winners['closing_date'] if opening else None,
                       end_time=None,
                       date_meaning='window' if payload.get('kind') in ('exam','registration') or opening else 'deadline')
        roles = [('opening' if opening else 'closing','date','time'),('closing','end_date','end_time')]
    else:
        roles = [('start','date','time'),('end','end_date','end_time')]
    for role, day, clock in roles:
        if role+'_date' in winners and (not closing or payload.get(day)):
            payload[day] = winners[role+'_date']
            timed = winners.get(role+'_time')
            payload[clock] = timed[1] if timed and timed[0] == payload[day] else None
    conflict_text = INTERNAL_CONFLICT
    internal_texts, clarified_texts, clarified_proofs = [], [], []
    internal_fields = set()
    for index,claim in enumerate(claims):
        text = claim.get('uncertainty') or ''
        if not conflict_text.search(text):continue
        temporal_ambiguity = re.search(r'Datum|Datums|Termin|Frist|Schlie[ßs]|Wochentag|\bKW\b|date|deadline',text,re.I)
        affected = {key for key in facts[index] if key.endswith('_date')} if temporal_ambiguity else set()
        if re.search(r'Schlie[ßs]|Frist|deadline|closing',text,re.I) and 'closing_date' in affected:
            affected = {'closing_date'}
        if len(set(re.findall(r'\d{1,2}:\d{2}',text))) > 1 or re.search(r'Uhrzeit|Schlie[ßs]zeit|closing\s+time',text,re.I):
            affected |= {key.replace('_date','_time') for key in affected}
        # Only a precisely ordered source asserting every affected fact can clear
        # an older source's internal ambiguity. Undated or partial replies cannot.
        clarified = affected and all(key in provenance and any(
            newer_publication(p.get('published_at'),proofs[index].get('published_at'))
            and not any(conflict_text.search(c.get('uncertainty') or '') for c,q in zip(claims,proofs)
                        if q['document_id'] == p['document_id'])
            for p in provenance[key]) for key in affected)
        if not clarified:
            internal_texts.append(text)
            internal_fields.update(affected or {'obligation' if not temporal_ambiguity else 'start_date'})
        else:
            clarified_texts.append(text)
            clarified_proofs.append(dict(document_id=proofs[index]['document_id'],page=proofs[index]['page'],quote=proofs[index]['quote']))
    if payload.get('uncertainty') in clarified_texts and not internal_texts:
        payload['uncertainty'] = None
    ambiguities = []
    if ambiguous_policy:
        reason = 'Pflichtgrad unklar: Derselbe TeachCenter-Text wurde als verpflichtend und empfohlen ausgewertet.'
        if reason not in (payload.get('uncertainty') or ''):
            payload['uncertainty'] = ' '.join(filter(None,[payload.get('uncertainty'),reason]))
        ambiguities.append({'field':'obligation','reason':reason,'document_ids':sorted({proofs[i]['document_id'] for i in ambiguous_policy})})
    internal = bool(internal_texts)
    internal_date = any(re.search(r'Datum|Datums|Termin|Frist|Schlie[ßs]|Wochentag|\bKW\b|date|deadline',t,re.I) for t in internal_texts)
    return payload, bool(unresolved) or internal, any(f.endswith(('_date','_time')) for f in unresolved) or internal_date, {
        'superseded_fields':sorted(superseded), 'unresolved_fields':sorted(unresolved),
        'internal_conflict':internal, 'internal_unresolved_fields':sorted(internal_fields), 'superseded_internal_conflicts':clarified_proofs, 'extraction_ambiguities':ambiguities, 'facts':provenance}


def newer_publication(new, old):
    if not new or not old:
        return False
    try:
        left, right = datetime.fromisoformat(new), datetime.fromisoformat(old)
        if 'T' in new and 'T' in old and left.tzinfo and right.tzinfo:
            return left > right
        # Unknown time on the same day cannot establish ordering.
        return left.date() > right.date()
    except ValueError:
        return False


def newer_observation(new, old):
    """A precise new fetch can establish the current hash of this exact URL."""
    if not new or not old or 'T' not in new or 'T' not in old:return False
    try:
        left,right = datetime.fromisoformat(new),datetime.fromisoformat(old)
        return bool(left.tzinfo and right.tzinfo and left > right)
    except ValueError:
        return False


SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, semester TEXT NOT NULL DEFAULT '',
 group_name TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
 external_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS sources (
 id INTEGER PRIMARY KEY, course_id INTEGER REFERENCES courses(id), kind TEXT NOT NULL,
 title TEXT NOT NULL, url TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',
 active INTEGER NOT NULL DEFAULT 1, last_attempt TEXT, last_success TEXT,
 state TEXT NOT NULL DEFAULT 'new', message TEXT NOT NULL DEFAULT '',
 crawl_state TEXT NOT NULL DEFAULT '{}', UNIQUE(kind,url));
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
 url TEXT NOT NULL, title TEXT NOT NULL, mime TEXT NOT NULL, hash TEXT NOT NULL,
 published_at TEXT, fetched_at TEXT NOT NULL, original_path TEXT NOT NULL,
 pages TEXT NOT NULL DEFAULT '[]', processing_state TEXT NOT NULL DEFAULT 'pending',
 analysis_state TEXT NOT NULL DEFAULT 'pending', analysis_at TEXT,
 error TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '{}',
 UNIQUE(source_id,url,hash));
CREATE TABLE IF NOT EXISTS items (
 id INTEGER PRIMARY KEY, course_id INTEGER REFERENCES courses(id), identity TEXT NOT NULL UNIQUE,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', ignored INTEGER NOT NULL DEFAULT 0,
 pinned INTEGER NOT NULL DEFAULT 0, overrides TEXT NOT NULL DEFAULT '{}',
 conflict INTEGER NOT NULL DEFAULT 0, history TEXT NOT NULL DEFAULT '[]',
 created_at TEXT NOT NULL, changed_at TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS item_evidence (
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id),
 document_id INTEGER NOT NULL REFERENCES documents(id), page INTEGER NOT NULL,
 anchor TEXT NOT NULL, quote TEXT NOT NULL, payload TEXT NOT NULL,
 current INTEGER NOT NULL DEFAULT 1, UNIQUE(item_id,document_id,page,anchor,quote));
CREATE TABLE IF NOT EXISTS sync_runs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, auto_day TEXT UNIQUE,
 started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL DEFAULT 'running',
 results TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS assistant_requests (
 id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, input TEXT NOT NULL,
 state TEXT NOT NULL, action TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS course_aliases (
 alias TEXT NOT NULL,course_id INTEGER NOT NULL REFERENCES courses(id),
 PRIMARY KEY(alias,course_id));
CREATE TABLE IF NOT EXISTS personal_reminders (
 item_id INTEGER PRIMARY KEY REFERENCES items(id),due_at TEXT NOT NULL,
 request_id TEXT NOT NULL,delivered_at TEXT);
CREATE TABLE IF NOT EXISTS app_preferences (name TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS item_preferences (item_id INTEGER PRIMARY KEY REFERENCES items(id),value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminder_jobs (
 id TEXT PRIMARY KEY,item_id INTEGER NOT NULL REFERENCES items(id),target_at TEXT NOT NULL,
 due_at TEXT NOT NULL,rule TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',updated_at TEXT NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS notification_reads (
 job_id TEXT PRIMARY KEY REFERENCES reminder_jobs(id),read_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS google_events (
 calendar_id TEXT NOT NULL,identity TEXT NOT NULL,role TEXT NOT NULL,event_id TEXT NOT NULL,
 payload TEXT NOT NULL,hash TEXT NOT NULL,etag TEXT,state TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(calendar_id,identity,role));
PRAGMA user_version=1;
"""


class Database:
    def __init__(self, path):
        self.path = path
        self.initialize()

    def initialize(self):
        with self.connection() as con:
            con.executescript(SCHEMA)
            columns = {r['name'] for r in con.execute('PRAGMA table_info(documents)')}
            for name in ('revision', 'repository_path', 'source_url'):
                if name not in columns:
                    con.execute(f"ALTER TABLE documents ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            if 'mail_metadata' not in columns:
                con.execute("ALTER TABLE documents ADD COLUMN mail_metadata TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        try:
            with con:
                yield con
            if con.total_changes and getattr(self, 'on_change', None):
                self.on_change()
        finally:
            con.close()

    def rows(self, sql, args=(), connection=None):
        if connection is not None:
            return [dict(row) for row in connection.execute(sql, args)]
        with self.connection() as con:
            return [dict(row) for row in con.execute(sql, args)]

    def one(self, sql, args=(), connection=None):
        rows = self.rows(sql, args, connection=connection)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connection() as con:
            return con.execute(sql, args).lastrowid

    def reserve_run(self, kind, day=None):
        with self.connection() as con:
            try:
                return con.execute("INSERT INTO sync_runs(kind,auto_day,started_at) VALUES(?,?,?)", (kind, day, now())).lastrowid
            except sqlite3.IntegrityError:
                return None

    def recover_runs(self):
        self.execute("UPDATE sync_runs SET state='interrupted',finished_at=? WHERE state='running'", (now(),))
        self.execute("UPDATE sources SET state='partial',message='Abruf unterbrochen; nächster Sync setzt fort.' WHERE state='running'")

    def source(self, source_id):
        return self.one("SELECT s.*,c.title course_title,c.semester,c.group_name FROM sources s LEFT JOIN courses c ON c.id=s.course_id WHERE s.id=?", (source_id,))

    def sources(self):
        rows = self.rows("""WITH current_docs AS (
          SELECT d.* FROM documents d WHERE d.analysis_state!='archived' AND
          (d.mime='message/rfc822' OR d.id=(SELECT d2.id FROM documents d2 WHERE d2.source_id=d.source_id AND d2.url=d.url ORDER BY fetched_at DESC,id DESC LIMIT 1))
        ), stats AS (
          SELECT s.id,
            sum(CASE WHEN d.analysis_state!='success' OR d.processing_state!='success' THEN 1 ELSE 0 END) pending,
            sum(CASE WHEN d.analysis_state IN ('pending','failed') OR d.processing_state IN ('pending','failed') THEN 1 ELSE 0 END) analysis_pending,
            sum(CASE WHEN d.analysis_state='partial' OR d.processing_state='partial' THEN 1 ELSE 0 END) reading_gaps
          FROM sources s LEFT JOIN current_docs d ON d.source_id=s.id OR d.id IN (SELECT value FROM json_each(s.crawl_state,'$.document_ids')) GROUP BY s.id
        ) SELECT s.*,c.title course_title,c.semester,c.group_name,c.active course_active,
          stats.pending,stats.analysis_pending,stats.reading_gaps
          FROM sources s JOIN stats ON stats.id=s.id LEFT JOIN courses c ON c.id=s.course_id ORDER BY s.id""")
        for source in rows:
            media = source['kind']=='public' and urlsplit(source['url']).hostname in ('apkpure.com','apkmirror.com','tube.tugraz.at','youtube.com','www.youtube.com','youtu.be')
            source['deferred_analysis']=(source['analysis_pending'] or 0) if media else 0
        return rows

    def _evidence_context(self, con):
        pages = {}
        def read(document_id, page):
            if document_id not in pages:
                document = con.execute('SELECT pages FROM documents WHERE id=?',(document_id,)).fetchone()
                pages[document_id] = json.loads(document['pages']) if document else []
            return next((p.get('text','') for p in pages[document_id] if p['page'] == page),'')
        return read

    def ingest(self, document, candidate):
        value = candidate.model_dump(mode="json")
        source = self.source(document["source_id"])
        context = [source["course_id"], value.get("semester") or source["semester"], value.get("group") or source["group_name"], value["kind"]]
        value["semester"], value["group"] = context[1:3]
        if candidate.identity_scope == "course" and source['course_id'] is not None:
            context += [candidate.identity_key.casefold().strip()]
        else:
            context += [document["url"], candidate.identity_key or candidate.anchor]
        identity = hashlib.sha256(encode(context).encode()).hexdigest()
        with self.connection() as con:
            row = con.execute("SELECT * FROM items WHERE identity=?", (identity,)).fetchone()
            if row and row['ignored']:
                # A reviewed duplicate retains its original ID for history. Its copied evidence
                # identifies the retained entry on later versions without reviving the duplicate.
                retained = con.execute('''SELECT DISTINCT i.* FROM items i
                    JOIN item_evidence e ON e.item_id=i.id JOIN documents d ON d.id=e.document_id
                    WHERE i.course_id IS ? AND i.ignored=0 AND d.source_id=? AND d.url=? AND e.anchor=?
                    AND json_extract(e.payload,'$.kind')=?''',
                    (source['course_id'],document['source_id'],document['url'],candidate.anchor,candidate.kind)).fetchall()
                if len(retained) == 1:
                    row = retained[0]
            if not row and (candidate.identity_scope == 'document' or candidate.kind == 'event') and source['kind'] == 'teachcenter' and len(candidate.quote) >= 30:
                from urllib.parse import urlsplit
                from .llm import normalized
                path = urlsplit(document['url']).path
                if path.endswith(('/course/view.php', '/course/section.php', '/mod/page/view.php')):
                    copies = con.execute('''SELECT i.*,e.quote,e.payload evidence_payload,d.url evidence_url
                        FROM items i JOIN item_evidence e ON e.item_id=i.id JOIN documents d ON d.id=e.document_id
                        WHERE i.course_id=? AND d.source_id=? AND e.current=1 AND i.ignored=0''',
                        (source['course_id'],document['source_id'])).fetchall()
                    matches = {}
                    for proof in copies:
                        other_path = urlsplit(proof['evidence_url']).path
                        if not other_path.endswith(('/course/view.php','/course/section.php','/mod/page/view.php')):
                            continue
                        if path == other_path:
                            continue
                        old = json.loads(proof['evidence_payload'])
                        if normalized(proof['quote']) == normalized(candidate.quote) and all(
                            old.get(k) == value.get(k) for k in ('kind','date','time','end_date','end_time','date_meaning','semester','group')):
                            matches[proof['id']] = proof
                    if len(matches) == 1:
                        row = next(iter(matches.values()))
            if not row:
                item_id = con.execute("INSERT INTO items(course_id,identity,payload,created_at,changed_at) VALUES(?,?,?,?,?)", (source["course_id"], identity, encode(value), now(), now())).lastrowid
            else:
                item_id = row["id"]
            current = con.execute('''SELECT d.id,d.published_at,d.fetched_at FROM documents d JOIN item_evidence e ON e.document_id=d.id
                    WHERE e.item_id=? AND e.current=1 AND d.source_id=? AND d.url=? AND d.id!=?''',
                    (item_id,document['source_id'],document['url'],document['id'])).fetchall()
            if any(newer_publication(p['published_at'], document.get('published_at'))
                   or (not newer_publication(document.get('published_at'),p['published_at']) and p['id'] > document['id']
                       and not (not p['published_at'] and not document.get('published_at')
                                and newer_observation(document.get('fetched_at'),p['fetched_at'])))
                   for p in current):
                # A delayed old version/hash remains evidence, never a reversal.
                con.execute('''INSERT OR IGNORE INTO item_evidence(item_id,document_id,page,anchor,quote,payload,current)
                    VALUES(?,?,?,?,?,?,0)''', (item_id,document['id'],candidate.page,candidate.anchor,candidate.quote,encode(value)))
                con.execute('UPDATE item_evidence SET current=0 WHERE item_id=? AND document_id=?', (item_id,document['id']))
                return item_id
            # Old versions of this exact document remain inspectable but cease to be current assertions.
            con.execute("""UPDATE item_evidence SET current=0 WHERE item_id=? AND document_id IN
              (SELECT id FROM documents WHERE source_id=? AND url=? AND id!=?)""", (item_id, document["source_id"], document["url"], document["id"]))
            con.execute("INSERT OR IGNORE INTO item_evidence(item_id,document_id,page,anchor,quote,payload) VALUES(?,?,?,?,?,?)", (item_id, document["id"], candidate.page, candidate.anchor, candidate.quote, encode(value)))
            con.execute("UPDATE item_evidence SET current=1,payload=? WHERE item_id=? AND document_id=? AND page=? AND anchor=? AND quote=?", (encode(value), item_id, document['id'], candidate.page, candidate.anchor, candidate.quote))
            proofs = [dict(r) for r in con.execute('''SELECT e.*,d.published_at,d.url,d.title,d.source_id,s.kind FROM item_evidence e
                       JOIN documents d ON d.id=e.document_id JOIN sources s ON s.id=d.source_id
                       WHERE e.item_id=? AND e.current=1 ORDER BY e.id''',(item_id,))]
            fields = CLAIM_FIELDS
            selected, conflicts, _, _ = effective_evidence(proofs, value,context_lookup=self._evidence_context(con))
            if row:
                old = json.loads(row["payload"])
                meaningful = ("title", "description", *fields, "uncertainty")
                if any(old.get(f) != selected.get(f) for f in meaningful) or bool(row["conflict"]) != conflicts:
                    history = json.loads(row["history"])
                    history.append({"at": now(), "before": old, "after": selected, "conflict": conflicts})
                    con.execute("UPDATE items SET history=?,changed_at=?,seen=0 WHERE id=?", (encode(history), now(), item_id))
            con.execute("UPDATE items SET payload=?,conflict=? WHERE id=?", (encode(selected), conflicts, item_id))
        return item_id

    def items(self, active_only=False):
        # One short-lived snapshot, also for booking/group proofs. No stale item cache
        # or open reader left behind when restoring a backup.
        with self.connection() as con:
            con.execute('BEGIN')
            return self._items(active_only, con)

    def _items(self, active_only, con):
        result = self.rows("SELECT i.*,c.title course_title,c.active course_active,c.semester course_semester,c.group_name course_group FROM items i LEFT JOIN courses c ON c.id=i.course_id ORDER BY i.changed_at DESC", connection=con)
        profiles = {p['item_id']:json.loads(p['value']) for p in self.rows('SELECT * FROM item_preferences', connection=con)}
        proofs = {}
        for proof in self.rows("""SELECT e.item_id,e.payload,e.anchor,e.document_id,e.page,e.quote,s.kind,d.mime,d.url,d.title,d.published_at,d.source_id,d.hash,d.revision,d.fetched_at FROM item_evidence e
            JOIN documents d ON d.id=e.document_id JOIN sources s ON s.id=d.source_id WHERE e.current=1""", connection=con):
            proofs.setdefault(proof['item_id'],[]).append(proof)
        from .personal import context, resolve, group_context
        catalog = context(self, con)
        groups = group_context(self, con)
        context_lookup = self._evidence_context(con)
        for row in result:
            row["source_payload"] = json.loads(row["payload"])
            overrides = json.loads(row['overrides'])
            row["data"] = row["source_payload"] | overrides
            row["history"] = json.loads(row["history"])
            row['reminder_profile'] = profiles.get(row['id'],{})
            own_group=groups.get(row['course_id'],{})
            row['confirmed_groups']=own_group.get('groups',[])
            row['group_evidence']=own_group.get('evidence',[])
            if not row['course_group'] and len(row['confirmed_groups'])==1:row['course_group']=row['confirmed_groups'][0]
            current = proofs.get(row['id'],[])
            row['third_party_message']=any(p['kind']=='manual' and urlsplit(p['url']).hostname in ('discord.com','discordapp.com') and urlsplit(p['url']).path.startswith('/channels/') for p in current)
            claims = [temporal_claim(json.loads(p['payload'])) for p in current]
            if current and not row['identity'].startswith(('relative:','note:')):
                effective, row['conflict'], row['date_conflict'], row['evidence_resolution'] = effective_evidence(current,row['source_payload'],claims,context_lookup)
                row['stored_source_payload'] = row['source_payload']
                row['source_payload'] = effective
                row['data'] = effective | overrides
            else:
                row['date_conflict'] = False
            row['personal_event'] = any(
                p['kind']=='tugrazonline' and p['mime']=='text/calendar' and (v.get('identity_key') or '').startswith('tugrazonline:')
                for p,v in zip(current,claims))
            resolve(row,current,catalog)
            from .comparison import project
            project(row,current,context_lookup,profiles.get(row['id'],{}))
            if row['identity'].startswith('relative:'):
                row['date_conflict'] = False  # Rule and unit have distinct temporal roles.
        by_id={i['id']:i for i in result}
        for derived in result:
            relation=(derived['source_payload'].get('related_key') or '').split(':')
            if not derived['identity'].startswith('relative:') or len(relation)!=3 or not relation[2].isdigit():continue
            calendar=by_id.get(int(relation[2]))
            if not calendar or not (calendar['source_payload'].get('identity_key') or '').startswith('tugrazonline:'):continue
            if calendar['data'].get('group') and calendar['data']['group'] not in calendar.get('confirmed_groups',[]):continue
            legacy=next((i for i in result if derived['identity'].endswith(':'+i['identity']) and i['id']!=calendar['id']
                         and i.get('personal_resolution',{}).get('units') and not (i['source_payload'].get('identity_key') or '').startswith('tugrazonline:')),None)
            if not legacy:continue
            # Preserve the earlier personally edited entry and its ICS identity,
            # while showing the concrete calendar UID's current time/cancellation.
            overrides=json.loads(legacy['overrides'])
            for field in ('date','time','end_date','end_time','group','source_state'):
                if field not in overrides:legacy['data'][field]=calendar['data'].get(field)
            legacy['date_conflict']=calendar['date_conflict']
            for p in proofs.get(calendar['id'],[]):
                if p['mime']=='text/calendar':legacy['personal_resolution']['units'].append({'document_id':p['document_id'],'page':p['page'],
                    'quote':p['quote'],'title':p['title'],'url':f"/documents/{p['document_id']}?page={p['page']}",'role':'Aktueller eigener Kalendertermin · verknüpfte UID'})
            calendar['calendar_duplicate_of']=legacy['id']
        if active_only:
            result = [i for i in result if i['course_id'] is None or (i['course_active'] and
                      (not i['course_semester'] or not i['data'].get('semester') or i['course_semester'] == i['data']['semester']))]
            # Reused course pages can contain old dates even when a model calls them active.
            # Keep those records accessible in history, without mixing them into this winter.
            import re
            current = []
            for item in result:
                semester = item['course_semester'] or ''
                value = item['data']
                claim_date = value.get('end_date') or value.get('date')
                if (re.fullmatch(r'\d{4}W', semester) and claim_date and
                        claim_date < semester[:4] + '-08-01' and
                        not {'date','end_date'} & json.loads(item['overrides']).keys()):
                    continue
                current.append(item)
            result = current
        return result
