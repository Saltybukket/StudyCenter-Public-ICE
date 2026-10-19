"""Small shared, source-bound projection of personal booking and attendance evidence."""
import json
import re
from datetime import date, timedelta
from urllib.parse import urlsplit, parse_qs


def activity_url(url):
    p = urlsplit(url)
    return (p.hostname,p.path,parse_qs(p.query).get('id',[''])[0]) if '/mod/' in p.path else url


def activity(text):
    if re.search(r'setup[ -]?session', text, re.I): return 'setup'
    match = re.search(r'\blab\s*(\d+)\b', text, re.I)
    if match: return 'lab:' + match[1]
    if re.search(r'\b(?:Übung|Übungseinheit|exercise)\b', text, re.I): return 'exercise'
    if re.search(r'\b(?:Vorlesung|lecture)\b', text, re.I): return 'lecture'
    return ''


def proof(document, page, quote, role):
    return {'document_id': document['id'], 'page': page['page'], 'quote': quote,
            'title': document['title'], 'role': role, 'current': True,
            'url': f"/documents/{document['id']}?page={page['page']}"}


def booking_rows(text):
    if not re.search(r'Ihre Eintragung war erfolgreich|your booking (?:was|is) successful', text, re.I):
        return []
    # Success applies to the current appointments, not the offered slots below.
    section = re.split(r'Aktuelle Zeitfenster|Current (?:appointments|slots)', text, flags=re.I)
    if len(section) != 2: return []
    section = re.split(r'Verfügbare Zeitfenster|Available (?:appointments|slots)', section[1], flags=re.I)[0]
    return [line for line in section.splitlines() if re.search(r'\d{1,2}:\d{2}\s*[–—-]\s*\d{1,2}:\d{2}', line)]


def slot_matches(line, value):
    if not value.get('date') or not value.get('time'): return False
    if value.get('end_date') not in (None,value['date']):return False
    day = date.fromisoformat(value['date'])
    months = ['Januar|Jänner|January', 'Februar|February', 'März|March', 'April', 'Mai|May', 'Juni|June',
              'Juli|July', 'August', 'September', 'Oktober|October', 'November', 'Dezember|December']
    dated = re.search(rf'(?<!\d){day.day}\.\s*(?:{months[day.month-1]})\s+{day.year}\b', line, re.I)
    dated = dated or re.search(rf'(?<!\d){day.day:02}\.{day.month:02}\.{day.year}\b', line) or value['date'] in line
    times = re.search(r'(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})', line)
    return bool(dated and times and times[1].zfill(5) == value['time'][:5]
                and (not value.get('end_time') or times[2].zfill(5) == value['end_time'][:5]))


def context(db, connection=None):
    documents = db.rows("""SELECT d.id,d.hash,d.title,d.url,d.source_id,d.fetched_at,d.analysis_at,d.processing_state,length(d.pages) page_size,s.course_id,s.kind,c.semester
        FROM documents d JOIN sources s ON s.id=d.source_id JOIN courses c ON c.id=s.course_id
        WHERE d.analysis_state!='archived' AND d.id=(SELECT d2.id FROM documents d2
        WHERE d2.source_id=d.source_id AND d2.url=d.url ORDER BY d2.fetched_at DESC,d2.id DESC LIMIT 1)""", connection=connection)
    signature = tuple(tuple(d.values()) for d in documents)
    cached = getattr(db, '_personal_context', None)
    if cached and cached[0] == signature: return cached[1]
    latest={}
    for d in documents:
        key=(d['source_id'],activity_url(d['url']))
        if key not in latest or (d['fetched_at'],d['id'])>(latest[key]['fetched_at'],latest[key]['id']):latest[key]=d
    documents=list(latest.values())
    bookings, duties, units = {}, {}, {}
    page_map={r['id']:r['pages'] for r in db.rows('SELECT id,pages FROM documents WHERE id IN (SELECT value FROM json_each(?))',(json.dumps([d['id'] for d in documents]),),connection=connection)}
    for document in documents:
        pages = json.loads(page_map[document['id']])
        for page in pages:
            text = page['text']
            if document['kind']=='teachcenter' and 'erste Übungseinheit' in text:
                units.setdefault(document['course_id'],[]).append((document,page))
            if document['kind'] == 'teachcenter' and '/mod/scheduler/' in document['url']:
                for line in booking_rows(text):
                    bookings.setdefault(document['course_id'], []).append((document, page, line))
            # Named activity in the actual sentence/section; never transfer lab rules to lectures.
            compact = ' '.join(text.split())
            for match in re.finditer(r'(?:Mandatory Setup Session\b.{0,350}?require you to participate[^.!?]{0,180}|'
                                     r'(?:Setup[ -]?Session|Vorlesung|lecture|Übung|exercise|Lab\s*\d+)'
                                     r'[^.!?]{0,180}(?:Anwesenheitspflicht|attendance (?:is )?(?:mandatory|required|optional)|'
                                     r'Teilnahme (?:ist )?(?:verpflichtend|erforderlich|freiwillig))[^.!?]{0,100})', compact, re.I):
                quote = match[0]
                # A reused syllabus explicitly dated to an older year is not a current rule.
                years = re.findall(r'\b20\d{2}\b', compact)
                if years and document['semester'][:4] not in years: continue
                scope = activity(quote)
                negative = bool(re.search(r'keine?\s+Anwesenheitspflicht|optional|freiwillig|do not require|not (?:required|mandatory)', quote, re.I))
                if scope: duties.setdefault((document['course_id'], scope), []).append((proof(document,page,quote,'Teilnahmeregel'),not negative))
    value = (bookings, duties, units)
    db._personal_context = signature, value
    return value


def resolve(item, evidence, catalog):
    bookings, duties, units = catalog
    source = item['source_payload']
    result = {'booking': [], 'attendance': [], 'units': [], 'changes': [], 'issues': []}
    item['personal_resolution']=result
    item['booked_event']=False
    if source.get('semester') and item.get('course_semester') and source['semester']!=item['course_semester']:return
    row_urls = {activity_url(p['url']) for p in evidence}
    for document, page, line in bookings.get(item['course_id'], []):
        if activity_url(document['url']) in row_urls and slot_matches(line, source):
            if re.search(r'DURCHGESTRICHEN|abgesagt|cancelled',line,re.I):
                if 'source_state' not in json.loads(item['overrides']):item['data']['source_state']='cancelled'
                result['changes'].append(proof(document,page,line,'Absage dieses konkreten Slots'))
                result['issues'].append('Dieser konkrete Slot wurde im aktuellen Original ausdrücklich abgesagt.')
                continue
            confirmation = re.search(r'Ihre Eintragung war erfolgreich|your booking (?:was|is) successful',page['text'],re.I)[0]
            result['booking'].append(proof(document, page, line + '\n' + confirmation, 'Persönliche Buchung'))
    if source.get('kind')=='exam' and (source.get('identity_key') or '').startswith('tugrazonline:exam:'):
        for p in evidence:
            claim=json.loads(p['payload']);quote=p['quote']
            if (p['kind']=='tugrazonline' and p['mime']=='text/html' and claim.get('identity_key')==source['identity_key']
                    and re.search(r'\bBereits angemeldet\b',quote) and not re.search(r'\bNicht angemeldet\b',quote)
                    and all(claim.get(k)==source.get(k) for k in ('date','time','end_date','end_time'))):
                result['booking'].append({'document_id':p['document_id'],'page':p['page'],'quote':quote,'title':p['title'],
                    'url':f"/documents/{p['document_id']}?page={p['page']}",'role':'Persönliche Prüfungsanmeldung','current':True})
    item['booked_event'] = bool(result['booking']) and all(item['data'].get(k)==source.get(k) for k in ('date','time','end_date','end_time'))
    item['personal_event'] = item['personal_event'] or item['booked_event']
    scope = activity(source.get('title',''))
    if scope=='exercise' and source.get('source_state')!='cancelled' and source.get('date') and item.get('confirmed_groups') and re.search(r'Erste Übungseinheit',source['title'],re.I):
        day=date.fromisoformat(source['date'])
        months=['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember']
        for document,page in units.get(item['course_id'],[]):
            text=' '.join(page['text'].split())
            if activity_url(document['url']) not in row_urls or not re.search(rf'erste Übungseinheit findet am\s+{day.day}\.\s*{months[day.month-1]}\s+statt',text,re.I):continue
            groups=item['confirmed_groups']
            if len(groups)!=1:continue
            line=next((line for line in page['text'].splitlines() if line.startswith(groups[0]+' |')),None)
            clocks=re.search(r'(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})',line or '')
            if not clocks:continue
            item['personal_event']=True
            for k,v in {'time':clocks[1]+':00','end_date':source['date'],'end_time':clocks[2]+':00','group':groups[0],'source_state':'active','uncertainty':None}.items():
                if k not in json.loads(item['overrides']):item['data'][k]=v
            result['units']=[proof(document,page,line,'Eigene erste Einheit: Gruppenplan + Fixplatz')]+item.get('group_evidence',[])
    if (source.get('kind')=='event' and (source.get('identity_key') or '').startswith('tugrazonline:')
            and source.get('group') in item.get('confirmed_groups',[]) and source.get('group')!='Standardgruppe'):
        for document,page in units.get(item['course_id'],[]):
            if not re.search(r'Übungsgruppen',document['title'],re.I):continue
            line=next((line for line in page['text'].splitlines() if line.startswith(source['group']+' |')),None)
            if not line:continue
            result['unit_type']='exercise'
            result['units']=[proof(document,page,line,'Übungsgruppe des konkreten eigenen Kalendertermins')]+item.get('group_evidence',[])
            if 'title' not in json.loads(item['overrides']):item['data']['title']=item['course_title']+' · Übungseinheit'
            break
    rules = duties.get((item['course_id'],scope), []) if scope else []
    if rules and (item['booked_event'] or item['personal_event']):
        result['attendance'] = [p for p,_ in rules]
        opinions = {required for _,required in rules}
        if len(opinions)>1 or (True in opinions and source.get('obligation') in ('optional','recommended')):
            result['issues'].append('Widersprüchliche Teilnahmebedingungen für diese Aktivität.')
            item['conflict'] = True
        elif True in opinions:
            item['official_attendance'] = 'lecture' if scope=='lecture' else 'exercise'
            if 'obligation' not in json.loads(item['overrides']): item['data']['obligation'] = 'required'
    item['personal_resolution'] = result


def group_context(db, connection=None):
    result={}
    for source in db.rows("SELECT * FROM sources WHERE kind='tugrazonline' AND active=1", connection=connection):
        config=json.loads(source['config']);groups=config.get('assigned_groups',[])
        if not groups:continue
        doc=db.one('SELECT id,title,pages FROM documents WHERE id=?',(config.get('registration_document'),),connection=connection)
        if doc is None:
            doc=db.one("SELECT id,title,pages FROM documents WHERE source_id=? AND title='Eigene LV-Anmeldung' ORDER BY fetched_at DESC,id DESC LIMIT 1",(source['id'],),connection=connection)
        if not doc:continue
        pages=json.loads(doc['pages'])
        proofs=[proof(doc,p,p['text'][:1500],'Eigene LV-Gruppe / Fixplatz') for p in pages if re.search(r'FIXPLATZ|Fixplatz zugewiesen',p['text'])]
        if proofs:
            existing=result.setdefault(source['course_id'],{'groups':[],'evidence':[],'course_type':config.get('course_type','')})
            existing['groups']=sorted(set(existing['groups']+groups));existing['evidence'].extend(proofs)
    return result


def coverage(db, course_id=None):
    """Coverage from saved facts only; fetch health and personal assignment are separate."""
    from .presentation import personal_state
    groups=group_context(db);items=db.items(active_only=True);sources=db.sources();result=[]
    def deferred_source(s):
        return s['kind']=='public' and urlsplit(s['url']).hostname in ('apkpure.com','apkmirror.com','tube.tugraz.at','youtube.com','www.youtube.com','youtu.be')
    for course in db.rows('SELECT * FROM courses WHERE active=1 ORDER BY title'):
        if course_id and course['id']!=course_id:continue
        owned=groups.get(course['id'],{});own=[i for i in items if i['course_id']==course['id']]
        src=[s for s in sources if s['course_id']==course['id'] and s['active']]
        kind=next((json.loads(s['config']).get('course_type') for s in src if json.loads(s['config']).get('course_type')),'')
        personal=[i for i in own if i['personal_event'] and not i['ignored'] and not i.get('calendar_duplicate_of')]
        gaps=[]
        if kind in ('UE','VU','LU') and not owned.get('groups'):gaps.append('Eigene Übungsgruppe nicht bestätigt.')
        if not any(i['personal_event'] and (i['source_payload'].get('identity_key') or '').startswith('tugrazonline:') for i in own):gaps.append('Persönlicher LV-Kalender fehlt; gespeicherte Angebote sind keine Buchungen.')
        for rule in own:
            if relative_rule(rule) and not any(activity(i['data']['title'])=='exercise' and i['data'].get('date') and i['data'].get('end_date') in (None,i['data']['date']) for i in personal):
                gaps.append('Ankreuzregel vorhanden; konkrete eigene Übungseinheit fehlt.')
        if not any(i['data']['kind'] in ('task','registration','notice') and not i['identity'].startswith('note:') for i in own):gaps.append('Organisatorische Belege fehlen.')
        if any(s['kind']=='tugrazonline' and s['state']=='login_required' for s in src):gaps.append('Frischer TU-Abgleich benötigt eine erneute manuelle Anmeldung.')
        checks=[json.loads(s['config']) for s in src if s['kind']=='tugrazonline' and json.loads(s['config']).get('exam_checked_at')]
        latest=max(checks,key=lambda c:c['exam_checked_at'],default={})
        exams=[i for i in own if i['source_payload'].get('kind')=='exam' and (i['source_payload'].get('identity_key') or '').startswith('tugrazonline:exam:')]
        booked=sum(bool(i.get('booked_event')) for i in exams)
        issues=latest.get('exam_issues',[])
        exam_status=(f"Zuletzt geprüft: {latest['exam_checked_at']}; {latest.get('exam_entries',0)} Angebote, {booked} belegte Anmeldungen."
                     if latest else 'Persönlicher Prüfungsstatus noch nicht geprüft.')
        if issues:gaps.extend('Prüfungsabgleich: '+str(issue) for issue in issues)
        if not latest:gaps.append('Persönlicher Prüfungsstatus noch nicht geprüft.')
        # Keep legacy MCP fields, but separate student questions from processing counts.
        access_issues=[{'source_id':s['id'],'title':s['title'],'state':s['state'],
                        'last_attempt':s['last_attempt'],'last_success':s['last_success'],'kind':s['kind']}
                       for s in src if not deferred_source(s) and s['kind']!='manual'
                       and (s['state'] in ('login_required','failed','interrupted') or not s['last_success'])]
        relevant=[{'title':gap,'detail':'','items':[]} for gap in gaps if not gap.startswith('Frischer TU-Abgleich')]
        grouped={}
        for item in own:
            value=item['data'];choice=personal_state(item)
            if item['ignored'] or item['status']=='done' or value['source_state']=='cancelled' or choice['offered']:continue
            if value['kind'] not in ('task','registration','exam','event'):continue
            if value['obligation']!='required' and not choice['personal'] and not item['pinned']:continue
            policy_conflict=bool({'obligation','source_state'} & set(item.get('evidence_resolution',{}).get('unresolved_fields',[])))
            described_conflict=bool(re.search(r'widerspr|abweichende\s+(?:frist|termin)',value.get('uncertainty') or '',re.I))
            stale_decision=any(d.get('stale') for d in item.get('personal_decisions',[]))
            # A window's opening and its closing are not competing deadlines.
            conflict=item.get('date_conflict') or policy_conflict or described_conflict or stale_decision
            # General course rules, preparations and conditional registration advice
            # need not name a date. Only a personally evidenced commitment makes this
            # an actual missing personal date, rather than another generic warning.
            missing_date=(value['kind'] in ('task','registration','event','exam') and not value.get('date')
                          and (choice['booking_proven'] or item['personal_event']) and not relative_rule(item))
            if not conflict and not missing_date:continue
            title=value['title']
            signature=('group-registration' if value['kind']=='registration' and re.search(r'anmeld.*(?:übungsgruppe|uebungsgruppe)',title,re.I)
                       else title.casefold(), 'conflict' if conflict else 'date')
            gap=grouped.setdefault(signature,{'title':'Anmeldung zur Übungsgruppe' if signature[0]=='group-registration' else title,
                  'detail':'Quellenangaben widersprüchlich.' if conflict else 'Konkreter Termin oder Abgabeschluss nicht belegt.','items':[]})
            if described_conflict and (not gap['items'] or re.search(r'\d{1,2}:\d{2}',value['uncertainty']) and not re.search(r'\d{1,2}:\d{2}',gap['detail'])):
                gap['detail']=value['uncertainty']
            if stale_decision:gap['detail']='Neue Quelle – Entscheidung prüfen. Die bisherige persönliche Entscheidung bleibt wirksam.'
            gap['items'].append({'id':item['id'],'title':title,'url':f"/items/{item['id']}#evidence",'compare_url':f"/items/{item['id']}/compare"})
        relevant.extend(grouped.values())
        pending=sum((s['analysis_pending'] or 0)-s['deferred_analysis'] for s in src)
        deferred=sum(s['deferred_analysis'] for s in src)
        reading=sum(s['reading_gaps'] or 0 for s in src)
        links=sum(len(json.loads(s['crawl_state']).get('queue',[])) for s in src)
        if pending:gaps.append(f'{pending} gespeicherte Dokumente noch auszuwerten.')
        if reading:gaps.append(f'{reading} Dokument-Leselückenmeldungen; keine Aussage über fehlende Aufgaben.')
        if links:gaps.append(f'{links} Links noch nicht gelesen; Dateiformat/Relevanz im Quellenstatus prüfen.')
        result.append({'course_id':course['id'],'title':course['title'],'course_type':kind,'groups':owned.get('groups',[]),
                       'group_evidence':owned.get('evidence',[]),'personal_events':len(personal),
                       'semester':course['semester'],'configured_group':course['group_name'],
                       'relevant_gaps':relevant,'access_issues':access_issues,
                       'needs_clarification':bool(relevant),'refresh_needed':bool(access_issues),
                       'source_checks':[{'id':s['id'],'title':s['title'],'kind':s['kind'],'last_success':s['last_success'],
                                         'last_attempt':s['last_attempt'],'state':s['state'],'deferred':deferred_source(s)} for s in src],
                       'last_source_success':max((s['last_success'] for s in src if s['last_success']),default=None),
                       'pending':pending,'deferred_analysis':deferred,'reading_gaps':reading,'unread_links':links,
                       'gaps':list(dict.fromkeys(gaps)),
                       'exam_registration':exam_status+' Angebote und erledigte TODOs sind keine Buchungen.',
                       'exam_coverage':{'checked_at':latest.get('exam_checked_at'),'offers':latest.get('exam_entries'),
                                        'booked':booked,'issues':issues}})
    return result


def relative_rule(item):
    value = item['source_payload']
    if item.get('identity','').startswith('relative:') or value.get('date') or item.get('conflict'):return None
    text = value.get('quote','')
    # The real source has the typo "Vortrag"; "wie gewohnt" confirms the normal
    # checkbox rule, not the conditional 08:00 replacement submission next to it.
    if value['kind']!='task' or value.get('obligation')!='required' or not re.search(r'kreuz',value['title'],re.I) or not re.search(r'ankreuzen|kreuzen.+an', text, re.I): return None
    match = re.search(r'bis\s+(\d{1,2}):(\d{2})(?:\s+Uhr)?\s+am\s+(Vortag|Vortrag)',text,re.I)
    if not match or match[3].lower()=='vortrag' and 'wie gewohnt' not in text.lower(): return None
    return f'{int(match[1]):02}:{match[2]}:00'


def relative_unit(rule, unit, by_id):
    primary=by_id.get(unit.get('calendar_duplicate_of'))
    value=primary['data'] if primary else unit['data']
    if unit['course_id']!=rule['course_id'] or value['kind']!='event' or not (activity(value['title'])=='exercise' or unit.get('personal_resolution',{}).get('unit_type')=='exercise'):return None
    if not unit['personal_event'] or not value.get('date') or value.get('end_date') not in (None,value['date']) or unit['date_conflict']:return None
    if value.get('group') and value['group'] not in unit.get('confirmed_groups',[]):return None
    return value


def relative_current(item, by_id):
    """Validate a stored derivation against parents read while dispatch excludes writers."""
    if not item['identity'].startswith('relative:'):return True
    payload=item['source_payload']
    parts=(payload.get('related_key') or '').split(':')
    if len(parts)!=3 or parts[0]!='relative' or not all(p.isdigit() for p in parts[1:]):return False
    rule,unit=(by_id.get(int(p)) for p in parts[1:])
    if not rule or not unit:return False
    clock=relative_rule(rule)
    value=relative_unit(rule,unit,by_id) if clock else None
    if not value or rule['ignored'] or unit['ignored'] or rule['data']['source_state']!='active' or value['source_state']!='active':return False
    due=(date.fromisoformat(value['date'])-timedelta(days=1)).isoformat()
    return payload.get('date')==due and payload.get('time')==clock and payload.get('source_state')=='active'


def reconcile_relative(db):
    """Called after a manual sync/local reconciliation; no fetching and no scheduler."""
    from .db import encode, now
    rows = db.items(active_only=True)
    generated = 0
    expected=set()
    for rule in rows:
        clock = relative_rule(rule)
        if not clock: continue
        prior=[i for i in rows if i['identity'].startswith('relative:'+rule['identity']+':')]
        by_id={i['id']:i for i in rows}
        units=sorted(rows,key=lambda i:not (i['source_payload'].get('identity_key') or '').startswith('tugrazonline:'))
        seen_units=set()
        for unit in units:
            value=relative_unit(rule,unit,by_id)
            if value is None:continue
            identity = 'relative:' + rule['identity'] + ':' + unit['identity']
            target_key='relative:'+str(rule['id'])+':'+str(unit['id'])
            # An earlier first-unit proof can now be tied to its actual calendar UID.
            # Keep the derived entry's identity/status; later UID shifts reuse it.
            if any(p['identity']==identity and p['source_payload'].get('related_key')!=target_key
                   and int((p['source_payload'].get('related_key') or '::0').split(':')[-1]) in by_id for p in prior):continue
            existing=next((p for p in prior if p['source_payload'].get('related_key')==target_key),None)
            if existing is None and unit.get('personal_resolution',{}).get('unit_type')=='exercise':
                matches=[]
                for p in prior:
                    parent=by_id.get(int((p['source_payload'].get('related_key') or '::0').split(':')[-1]))
                    if (parent and parent.get('personal_resolution',{}).get('units') and not (parent['source_payload'].get('identity_key') or '').startswith('tugrazonline:')
                            and all(parent['data'].get(k)==value.get(k) for k in ('date','time','group'))):matches.append(p)
                if len(matches)==1:existing=matches[0]
            if existing:identity=existing['identity']
            occurrence=(unit['course_id'],value['date'],value.get('time'),value.get('group'))
            calendar_unit=(unit['source_payload'].get('identity_key') or '').startswith('tugrazonline:')
            if occurrence in seen_units and not calendar_unit:continue
            if calendar_unit:seen_units.add(occurrence)
            expected.add(identity)
            due = (date.fromisoformat(value['date'])-timedelta(days=1)).isoformat()
            payload = dict(rule['source_payload'],title=rule['data']['title']+' · '+value['date'],
                           date=due,time=clock,date_meaning='deadline',end_date=None,end_time=None,
                           related_key=target_key,
                           source_state='cancelled' if value['source_state']=='cancelled' or rule['data']['source_state']=='cancelled' or rule['ignored'] or unit['ignored'] else 'active' if value['source_state']=='active' and rule['data']['source_state']=='active' else 'unclear',
                           uncertainty=None,description=f"Abgeleitet: Regel #{rule['id']} (Vortag um {clock[:5]}) + eigene Einheit #{unit['id']} am {value['date']}. Die Quelle nennt die Regel, nicht dieses errechnete Datum.")
            with db.connection() as con:
                old = con.execute('SELECT * FROM items WHERE identity=?',(identity,)).fetchone()
                if old is None and payload['source_state']!='active':continue
                support=[]
                for parent in (rule,unit):
                    support.extend(dict(p) for p in con.execute('SELECT * FROM item_evidence WHERE item_id=? AND current=1',(parent['id'],)).fetchall())
                for p in unit.get('personal_resolution',{}).get('units',[]):
                    support.append({'document_id':p['document_id'],'page':p['page'],'anchor':'relative-support',
                                    'quote':p['quote'],'payload':encode(unit['source_payload'])})
                keys=lambda rows:{(p['document_id'],p['page'],p['anchor'],p['quote']) for p in rows}
                if old and json.loads(old['payload'])==payload and keys(support)==keys(con.execute('SELECT * FROM item_evidence WHERE item_id=? AND current=1',(old['id'],)).fetchall()):continue
                if old:
                    identifier = old['id']
                    if json.loads(old['payload'])!=payload:
                        history=json.loads(old['history']);history.append({'at':now(),'before':json.loads(old['payload']),'after':payload,'conflict':False,'reason':'Bezugseinheit oder relative Regel geändert.'})
                        con.execute('UPDATE items SET payload=?,history=?,changed_at=?,seen=0 WHERE id=?',(encode(payload),encode(history),now(),identifier))
                else:
                    identifier=con.execute('INSERT INTO items(course_id,identity,payload,created_at,changed_at) VALUES(?,?,?,?,?)',(rule['course_id'],identity,encode(payload),now(),now())).lastrowid;generated+=1
                con.execute('UPDATE item_evidence SET current=0 WHERE item_id=?',(identifier,))
                # Preserve the two originals verbatim; never disguise the derived date as a quote.
                for p in support:
                    con.execute('INSERT OR IGNORE INTO item_evidence(item_id,document_id,page,anchor,quote,payload,current) VALUES(?,?,?,?,?,?,1)',(identifier,p['document_id'],p['page'],p['anchor'],p['quote'],p['payload']))
                    con.execute('UPDATE item_evidence SET current=1,payload=? WHERE item_id=? AND document_id=? AND page=? AND anchor=? AND quote=?',(p['payload'],identifier,p['document_id'],p['page'],p['anchor'],p['quote']))
    parents={i['id']:i for i in rows}
    for old in db.rows("SELECT * FROM items WHERE identity LIKE 'relative:%'"):
        if old['identity'] in expected:continue
        value=json.loads(old['payload']);parts=(value.get('related_key') or '').split(':')
        unit=parents.get(int(parts[2])) if len(parts)==3 and parts[2].isdigit() else None
        state='cancelled' if unit and unit['data']['source_state']=='cancelled' else 'unclear'
        changed=value|{'source_state':state,'uncertainty':'Bezugsregel oder eigene Einheit nicht mehr eindeutig bestätigt; keine Erinnerung.'}
        if value!=changed:
            history=json.loads(old['history']);history.append({'at':now(),'before':value,'after':changed,'conflict':False,'reason':'Belegter Bezug fehlt oder wurde abgesagt.'})
            db.execute('UPDATE items SET payload=?,history=?,changed_at=? WHERE id=?',(encode(changed),encode(history),now(),old['id']))
    return generated
