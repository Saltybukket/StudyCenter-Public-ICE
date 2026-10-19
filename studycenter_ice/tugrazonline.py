"""Read the user's own TUGRAZonline pages with StudyCenter-ICE's browser session."""
from urllib.parse import urlsplit, urljoin, parse_qs
from pathlib import Path
import re
import json
import unicodedata
from datetime import datetime, timezone
import asyncio

from bs4 import BeautifulSoup

from .browser import LoginRequired
from .collectors import Fetched, safe_url
from .documents import calendar_content
from .config import VIENNA
from .db import encode, now
from .models import Candidate

ROOT_URL = 'https://online.tugraz.at/tug_online/'


class TUGRAZonlineError(ValueError):
    """A concrete user-facing collector error, without response bodies or credentials."""


class TUGRAZonlineCollector:
    def __init__(self, context):
        self.context = context

    async def export_calendar(self, url, start, end):
        """Use the existing private download form, never 'Veröffentlichen'."""
        if urlsplit(url).hostname != 'online.tugraz.at' or urlsplit(url).path != '/tug_online/wbKalender.wbExport':
            raise TUGRAZonlineError('Kein verlinkter persönlicher Kalenderexport')
        page = await self.context.new_page()
        try:
            response = await page.goto(url, wait_until='domcontentloaded', timeout=35000)
            if response and response.status >= 400:
                raise TUGRAZonlineError(f'TUGRAZonline HTTP {response.status}')
            await page.locator('input[name="pDateFrom"]').wait_for(timeout=25000)
            await page.locator('input[name="pDateFrom"]').fill(start)
            await page.locator('input[name="pDateTo"]').fill(end)
            await page.locator('#pFormat_ICS').check()
            async with page.expect_download(timeout=35000) as pending:
                await page.locator('#idDownloadButton').click()
            download = await pending.value
            return Path(await download.path()).read_bytes()
        finally:
            await page.close()

    async def read_page(self, url):
        # CAMPUSonline uses hash routes; the generic public collector drops those.
        fragment = urlsplit(url).fragment
        url = safe_url(url) + ('#' + fragment if fragment else '')
        if urlsplit(url).hostname != 'online.tugraz.at':
            raise TUGRAZonlineError('Nur TUGRAZonline-Seiten im eigenen TU-Browser')
        page = await self.context.new_page()
        self.exam_responses=[];observations=[]
        async def observe(response):
            try:
                raw=await response.body();data=json.loads(raw)
                if isinstance(data,dict) and isinstance(data.get('examOffers'),list):self.exam_responses.append((response.url,raw,data['examOffers']))
            except Exception:pass  # Only already rendered responses; no extra request or inferred ID.
        def response_seen(response):
            part=urlsplit(response.url)
            if part.hostname=='online.tugraz.at' and part.path=='/tug_online/ee/rest/slc.xm.exr/exExamOffer':observations.append(asyncio.create_task(observe(response)))
        page.on('response',response_seen)
        try:
            response=await page.goto(url, wait_until='domcontentloaded', timeout=35000)
            if response and response.status>=400:raise TUGRAZonlineError(f'TUGRAZonline HTTP {response.status}; verlinkte Seite prüfen.')
            if urlsplit(page.url).hostname!='online.tugraz.at':raise LoginRequired('TUGRAZonline: bitte im eigenen TU-Browser erneut anmelden.')
            # Angular fills the desktop after the initial HTML; do not store its loader.
            try:
                # Angular's initial title/network can settle before its own session UI.
                # Wait for actual authenticated content or a real login form.
                await page.locator('#id-ca-sidenav-logout, .profile-username, input[type="password"]').first.wait_for(state='attached', timeout=25000)
                if not fragment and urlsplit(url).path.rstrip('/')=='/tug_online':
                    await page.get_by_role('link', name=re.compile('^Meine Lehrveranstaltungen')).first.wait_for(state='attached', timeout=25000)
                if '/pages/slc.tm.cp/course/' in url:
                    await page.locator('#toc_examInformation').wait_for(state='attached',timeout=25000)
                await page.wait_for_load_state('networkidle', timeout=10000)
            except Exception:
                pass  # Long polling must not prevent an explicit partial-content result.
            html = await page.content()
            soup = BeautifulSoup(html, 'html.parser')
            if soup.select_one('input[type="password"]') or urlsplit(page.url).hostname != 'online.tugraz.at':
                raise LoginRequired('TUGRAZonline: bitte im eigenen TU-Browser erneut anmelden.')
            title = await page.title()
            if 'could not be found' in title.lower():raise TUGRAZonlineError('Verlinkte TUGRAZonline-Seite nicht gefunden; Navigation/Seitenstruktur prüfen.')
            if title == 'Loading' or not soup.select_one('#id-ca-sidenav-logout, .profile-username'):
                raise LoginRequired('TUGRAZonline: angemeldeter Inhalt nicht erkennbar; manuell anmelden.')
            # Originals contain course content only, never form values or executable code.
            for node in soup.select('script,style,input,textarea,select,meta,link'):
                node.decompose()
            for node in soup.select('form'):
                node.unwrap()  # Legacy CAMPUSonline puts its read tables inside forms.
            for node in soup.find_all(True):
                for key in list(node.attrs):
                    if key.lower().startswith('on') or key.lower() in ('nonce','value'):
                        del node.attrs[key]
            if observations:await asyncio.gather(*observations,return_exceptions=True)
            return Fetched(page.url, str(soup).encode('utf-8'), 'text/html', title)
        finally:
            await page.close()
            if observations:await asyncio.gather(*observations,return_exceptions=True)


def navigation_links(raw, url):
    """Only server-advertised read links; never submit registration or logout forms."""
    result = []
    soup = BeautifulSoup(raw, 'html.parser')
    base = soup.find('base', href=True)
    base_url = urljoin(url, base['href']) if base else url
    for node in soup.select('a[href]'):
        target = urljoin(base_url, node['href'])
        if urlsplit(target).hostname == 'online.tugraz.at' and not any(
            word in target.lower() for word in ('logout','logoff','abmelden','delete')):
            result.append({'title':node.get_text(' ',strip=True),'url':target})
    return result


def assigned_groups(raw):
    """Only the personal status table proves assignment; available groups do not."""
    soup = BeautifulSoup(raw, 'html.parser')
    groups = []
    for table in soup.select('table'):
        headers = [h.get_text(' ',strip=True) for h in table.select('tr:first-child th')]
        if headers[:3] != ['Gruppe','Status','Status erreicht am']:
            continue
        for row in table.select('tr'):
            cells = row.find_all('td', recursive=False)
            if len(cells) >= 3 and cells[1].get_text(strip=True).upper() == 'FIXPLATZ':
                groups.append(cells[0].get_text(' ',strip=True))
    if not groups:
        raise TUGRAZonlineError('Keine persönliche Fixplatz-Gruppe bestätigt')
    return groups


def calendar_fields(page):
    fields = {}
    for line in page['text'].splitlines():
        if ':' in line:
            key, value = line.split(':',1)
            fields[key.split(';')[0]] = value.replace('\\,',',').replace('\\;',';').replace('\\n','\n').replace('\\N','\n')
    return fields


def course_type(source):
    configured=json.loads(source.get('config') or '{}').get('course_type')
    if configured in ('VO','UE','VU','LU'):return configured
    title=source.get('course_title',source.get('title',''))
    match=re.search(r'\b(VO|UE|VU|LU)\b',title)
    return match[1] if match else 'UE' if 'Übung' in title else ''


def title_key(title):
    title=re.sub(r'\[[^]]*\]|\([^)]*\)|\b(?:VO|UE|VU|LU)\b|,\s*Übung', '', title)
    return ' '.join(re.findall(r'[^\W_]+',unicodedata.normalize('NFKD',title).casefold()))


def personal_courses(raw, url=ROOT_URL):
    """Existing personal course cards, retaining their VO/UE identity and group."""
    soup=BeautifulSoup(raw,'html.parser');result={}
    base=soup.find('base',href=True)
    base_url=urljoin(url,base['href']) if base else url
    for link in soup.select('a[href]'):
        match=re.search(r'/student/courses/(\d+)',link['href'])
        if not match or not link.get_text(strip=True):continue
        card=link.find_parent('div',class_='list-spacer')
        if card is None:continue
        text=card.get_text(' ',strip=True);kind=re.search(r'\b(VO|UE|VU|LU)\s*\|',text)
        title=link.get_text(' ',strip=True)
        full=link.find_parent('div',class_='mobile-flex-column') or card
        group=re.search(re.escape(title)+r'\s*-\s*(.+?)\s+(?:VO|UE|VU|LU)\s*\|',text)
        if not kind:continue
        result[match[1]]={'online_id':match[1],'title':title,'course_type':kind[1],
                         'course_code':text.split()[0],'group':group[1] if group else '',
                         'url':urljoin(base_url,link['href']),'quote':full.get_text(' ',strip=True),
                         'exam_url':next((urljoin(base_url,a['href']) for a in full.select('a[href]') if a.get_text(' ',strip=True)=='Prüfungstermine'),None),
                         'confirmed':'Fixplatz zugewiesen' in full.get_text(' ',strip=True)}
    return list(result.values())


def match_course(entries, source):
    config=json.loads(source.get('config') or '{}')
    url_id=re.search(r'/student/courses/(\d+)',source.get('url',''))
    identifier=config.get('online_id') or (url_id[1] if url_id else '')
    kind=course_type(source)
    code=re.search(r'\[(\d+)\]',source['course_title'])
    candidates=[e for e in entries if (not kind or e['course_type']==kind) and
                (e['online_id']==identifier if identifier else
                 re.sub(r'\W','',e['course_code'])==code[1] if code else title_key(e['title'])==title_key(source['course_title']))]
    if len(candidates)!=1:raise TUGRAZonlineError('Persönliche LV-Zuordnung nicht eindeutig (Kennung und VO/UE/VU prüfen).')
    return candidates[0]


def exam_candidates(raw, source, online_id, url, offers=()):
    """The actual authenticated course page's exam cards; never submit enrollment."""
    if not re.search(r'/(?:course|student/courses)/'+re.escape(online_id)+r'(?:[?/#]|$)',url):
        raise TUGRAZonlineError('Prüfungsseite gehört nicht zur bestätigten LV-Kennung')
    soup=BeautifulSoup(raw,'html.parser');items=[];issues=[]
    months={'jan':1,'jän':1,'feb':2,'mär':3,'mar':3,'apr':4,'mai':5,'jun':6,'jul':7,'aug':8,'sep':9,'okt':10,'nov':11,'dez':12}
    for card in soup.select('xm-exam-offer-list-entry'):
        quote=card.get_text(' ',strip=True)
        identifier=next((m[1] for a in card.select('a[href]') if (m:=re.search(r'/exam/(\d+)',a['href']))),None)
        try:
            day=card.select_one('.appointment-day').get_text(strip=True)
            month,year=card.select_one('.appointment-month-year').get_text(' ',strip=True).split()
            when=datetime(int(year),months[month.lower()[:3]],int(day)).date().isoformat()
            column=card.select_one('.first-column').get_text(' ',strip=True)
            clocks=re.search(r'(?<!\d)(\d{1,2}:\d{2})(?:\s*-\s*(\d{1,2}:\d{2}))?',column)
            def raw_value(value):return value.get('value','') if isinstance(value,dict) else value
            matches=[o for o in offers if str(o.get('courseId'))==online_id
                     and str(raw_value(o.get('courseSemesterShortName',{}))).replace(' ','')==source['semester']
                     and raw_value(o.get('examDate',{}))==when
                     and clocks and str(raw_value(o.get('examStart',{})))[:5]==clocks[1].zfill(5)]
            if not identifier and len(matches)==1:identifier=str(matches[0].get('id',''))
            if not identifier or not re.fullmatch(r'[1-9]\d*',identifier):raise ValueError('eindeutige Prüfungskennung fehlt')
            explicit=re.search(r'Prüfungsbeginn\s+(\d{1,2}:\d{2})',quote)
            clock=explicit[1].zfill(5) if explicit else clocks[1].zfill(5) if clocks else None
            end_clock=clocks[2].zfill(5) if clocks and clocks[2] else None
            if end_clock and clock and end_clock<clock:raise ValueError('Ende vor Prüfungsbeginn')
            booked=bool(re.search(r'\bBereits angemeldet\b',quote)) and not re.search(r'\bNicht angemeldet\b',quote)
            status='Bereits angemeldet' if booked else 'Nicht angemeldet' if 'Nicht angemeldet' in quote else 'Anmeldestatus unklar'
            key='tugrazonline:exam:'+identifier
            common={'identity_scope':'course','semester':source['semester'],'page':1,'quote':quote}
            items.append(Candidate(**common,title='Prüfung · '+source['course_title'],kind='exam',identity_key=key,
                date=when,time=clock,end_date=when if end_clock else None,end_time=end_clock,date_meaning='start',
                obligation='unclear' if booked else 'optional',description=status+'. '+quote,anchor='exam-'+identifier))
            for window in card.select('.exam-start-end-date'):
                text=window.get_text(' ',strip=True)
                values=[datetime.strptime(m,'%d.%m.%Y, %H:%M') for m in re.findall(r'\d{2}\.\d{2}\.\d{4},\s*\d{2}:\d{2}',text)]
                if text.startswith('Anmeldezeitraum') and len(values)==2:
                    opening,closing=values
                    items.append(Candidate(**common,title='Prüfungsanmeldung · '+source['course_title'],kind='registration',identity_key=key+':registration',
                        date=opening.date(),time=opening.time(),end_date=closing.date(),end_time=closing.time(),date_meaning='window',obligation='optional',
                        description=status+'. '+text,anchor='exam-registration-'+identifier))
                elif text.startswith('Abmeldung bis') and len(values)==1:
                    closing=values[0]
                    items.append(Candidate(**common,title='Prüfungsabmeldung · '+source['course_title'],kind='registration',identity_key=key+':withdrawal',
                        date=closing.date(),time=closing.time(),date_meaning='window',obligation='optional',description=status+'. '+text,anchor='Abmeldeschluss-'+identifier))
        except (ValueError,AttributeError,KeyError):
            issues.append('Prüfungskarte ohne eindeutig lesbare Kennung/Datumswerte; Original prüfen.')
    return items,issues


def calendar_candidate(page, course, groups, online_id):
    f = calendar_fields(page)
    summary = f.get('SUMMARY','')
    matched=re.fullmatch(r'(.+?)\s+(VO|UE|VU|LU),\s*(.+)',summary)
    if not matched or matched[3] not in groups:
        return None
    expected=course_type(course)
    if expected and matched[2]!=expected:return None
    if title_key(matched[1])!=title_key(course['course_title']):return None
    if parse_qs(urlsplit(f.get('URL','')).query).get('pStpSpNr') != [online_id]:
        return None
    description = f.get('DESCRIPTION','')
    # A personal calendar also lists all offered slots for an enrolled group.
    # Only plain teaching events are unambiguous; booking/room choices stay open.
    parts=[p.strip() for p in description.split(';') if p.strip()]
    common=parts[:2] in (['fix','Abhaltung'],['abgesagt','Abhaltung']) and len(parts)==3 and re.match(r'Alle Gruppen\s*:',parts[2],re.I)
    if description.strip().rstrip(';').strip() not in ('fix; Abhaltung','abgesagt; Abhaltung') and not common:
        return None
    if 'RRULE' in f:
        raise TUGRAZonlineError('Serienregel im Export noch nicht unterstützt; keine Termine erfinden')
    for key in ('DTSTART','DTEND'):
        if not re.fullmatch(r'\d{8}T\d{6}Z',f.get(key,'')):
            raise TUGRAZonlineError('Kalenderzeit ohne bestätigten UTC-Bezug')
    start,end = [datetime.strptime(f[key],'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).astimezone(VIENNA) for key in ('DTSTART','DTEND')]
    group = next(g for g in groups if summary.endswith(', '+g))
    label={'VO':'Vorlesung','UE':'Übungseinheit'}.get(matched[2],'Lehrveranstaltung')
    return Candidate(title=course['course_title']+' · '+label, kind='event',
        identity_key='tugrazonline:'+f['UID'],identity_scope='course', semester=course['semester'],group=group,
        description='Termin deiner zugewiesenen '+matched[2]+'-Gruppe aus dem persönlichen TUGRAZonline-Kalender. Ort: '+f.get('LOCATION','nicht angegeben')+('. Keine persönliche Lab-Slotbuchung.' if matched[2] in ('VU','LU') else ''),
        date=start.date(),time=start.time().replace(tzinfo=None),end_date=end.date(),end_time=end.time().replace(tzinfo=None),
        date_meaning='start',obligation='unclear',source_state='cancelled' if f.get('STATUS')=='CANCELLED' or description.startswith('abgesagt') else 'active',
        quote=page['text'],page=page['page'],anchor=page['anchor'])


async def collect_course(source, context, db, store):
    collector = TUGRAZonlineCollector(context)
    root = await collector.read_page(ROOT_URL)
    personal_url = next((l['url'] for l in navigation_links(root.raw,root.url) if l['title'].startswith('Meine Lehrveranstaltungen')),None)
    if not personal_url:
        raise LoginRequired('TUGRAZonline: persönliche Kursliste fehlt; manuell anmelden.')
    personal = await collector.read_page(personal_url)
    text = BeautifulSoup(personal.raw,'html.parser').get_text(' ',strip=True)
    expected = re.fullmatch(r'(\d{4})([WS])',source['semester'] or '')
    if not expected or f'Semester {expected[1]} {expected[2]}' not in text:
        raise TUGRAZonlineError('Persönliche Kursliste zeigt nicht das konfigurierte Semester')
    matched = match_course(personal_courses(personal.raw,personal.url),source)
    detail = await collector.read_page(matched['url'])
    online_id = re.search(r'/student/courses/(\d+)',detail.url)[1]
    registration_url = next((l['url'] for l in navigation_links(detail.raw,detail.url) if 'LV-Anmeldedetails' in l['title']),None)
    scoped = BeautifulSoup('<html><head><title>Eigene LV-Anmeldung</title></head><body></body></html>','html.parser')
    if registration_url:
        registration = await collector.read_page(registration_url)
        groups = assigned_groups(registration.raw)
        soup = BeautifulSoup(registration.raw,'html.parser')
        main = soup.select_one('#idLVGrpAnmeldung') or soup.select_one('.MaskContent') or soup.body
        for table in main.select('table.list'):scoped.body.append(BeautifulSoup(str(table),'html.parser'))
    elif matched['confirmed'] and matched['group']:
        # The live VO page has no old detail link. Its own personal course card
        # explicitly names the fixed group; preserve that exact card as evidence.
        groups=[matched['group']];registration_url=personal.url
        soup=BeautifulSoup(personal.raw,'html.parser')
        link=next((a for a in soup.select('a[href]') if '/student/courses/'+online_id in a['href']),None)
        card=link.find_parent('div',class_='mobile-flex-column') if link else None
        if card is None:raise TUGRAZonlineError('Persönliche Fixplatz-Karte fehlt')
        scoped.body.append(card)
    else:raise TUGRAZonlineError('Keine aktuelle eigene Fixplatz-Gruppe bestätigt')
    if not scoped.body.get_text(strip=True):
        raise TUGRAZonlineError('Persönliche Anmeldetabellen fehlen')
    reg_id=store.save(source['id'],registration_url,'Eigene LV-Anmeldung','text/html',str(scoped).encode())
    document=store.process(reg_id)
    quote=next(p['text'] for p in json.loads(document['pages']))
    notice=Candidate(title='TUGRAZonline: '+', '.join(groups)+' / Fixplatz',kind='notice',
        identity_key='tugrazonline-registration:'+online_id,identity_scope='course',semester=source['semester'],
        description='Bestätigte eigene LV-Gruppe: '+', '.join(groups)+'. Dies ist keine Prüfungs- oder individuelle Slotbuchung.',
        quote=quote[:1500],page=1,anchor='registration-'+online_id)
    db.ingest(document,notice)
    db.execute("UPDATE documents SET analysis_state='success',analysis_at=? WHERE id=?",(now(),reg_id))
    exam_count=0;exam_issues=[];exam_document=None
    identity_documents=[]
    if matched.get('exam_url'):
        exam_page=await collector.read_page(matched['exam_url'])
        responses=[r for r in collector.exam_responses if r[2] and all(str(o.get('courseId'))==online_id for o in r[2])]
        offers=[o for _,_,rows in responses for o in rows]
        exam_items,exam_issues=exam_candidates(exam_page.raw,source,online_id,exam_page.url,offers)
        original=BeautifulSoup(exam_page.raw,'html.parser')
        scoped=BeautifulSoup('<html><head><title>Prüfungsinformation</title></head><body></body></html>','html.parser')
        section=original.select_one('#toc_examInformation')
        if section:scoped.body.append(section.parent)
        else:
            for card in original.select('xm-exam-offer-list-entry'):scoped.body.append(card)
        if scoped.body.get_text(strip=True):
            exam_document=store.save(source['id'],exam_page.url,'Prüfungsinformation','text/html',str(scoped).encode())
            document=store.process(exam_document)
            for candidate in exam_items:db.ingest(document,candidate)
            exam_count=sum(c.kind=='exam' for c in exam_items)
            db.execute("UPDATE documents SET analysis_state='success',analysis_at=? WHERE id=?",(now(),exam_document))
            for response_url,raw,records in responses:
                identity_doc=store.process(store.save(source['id'],response_url,'Prüfungskennungen · '+source['course_title'],'text/plain',raw))
                identity_documents.append(identity_doc['id']);text=json.loads(identity_doc['pages'])[0]['text']
                for candidate in exam_items:
                    if candidate.kind!='exam':continue
                    number=candidate.identity_key.rsplit(':',1)[-1];match=re.search(r'"id"\s*:\s*'+re.escape(number)+r'\b',text)
                    if not match:continue
                    identifier=db.one('SELECT id FROM items WHERE course_id=? AND json_extract(payload,\'$.identity_key\')=?',(source['course_id'],candidate.identity_key))['id']
                    quote=text[max(0,match.start()-250):min(len(text),match.end()+250)]
                    support=candidate.model_copy(update={'date':None,'time':None,'end_date':None,'end_time':None,'date_meaning':'none','source_state':'unclear','obligation':'unclear','quote':quote}).model_dump(mode='json')
                    # Identifier-only support, not a second assertion of the exam's time or booking.
                    db.execute('UPDATE item_evidence SET current=0 WHERE item_id=? AND document_id IN (SELECT id FROM documents WHERE source_id=? AND url=? AND id!=?)',(identifier,source['id'],response_url,identity_doc['id']))
                    db.execute('INSERT OR IGNORE INTO item_evidence(item_id,document_id,page,anchor,quote,payload) VALUES(?,?,?,?,?,?)',(identifier,identity_doc['id'],1,'exam-identity-'+number,quote,encode(support)))
                    db.execute('UPDATE item_evidence SET current=1,payload=? WHERE item_id=? AND document_id=? AND anchor=? AND quote=?',(encode(support),identifier,identity_doc['id'],'exam-identity-'+number,quote))
                db.execute("UPDATE documents SET analysis_state='success',analysis_at=? WHERE id=?",(now(),identity_doc['id']))
        else:exam_issues.append('Verlinkte Prüfungsinformation ohne lesbare Prüfungskarten.')
    calendar_url=next(l['url'] for l in navigation_links(root.raw,root.url) if l['title'].startswith('Mein Terminkalender'))
    calendar=await collector.read_page(calendar_url)
    export_url=next(l['url'] for l in navigation_links(calendar.raw,calendar.url) if l['title']=='Exportieren')
    year=int(expected[1]); start=f'01.10.{year}' if expected[2]=='W' else f'01.03.{year}'
    end=f'28.02.{year+1}' if expected[2]=='W' else f'30.09.{year}'
    raw=await collector.export_calendar(export_url,start,end)
    all_pages=calendar_content(raw)
    selected=[p for p in all_pages if 'pStpSpNr='+online_id in calendar_fields(p).get('URL','')]
    # Retain the exact unfolded source fields of this course only, before interpretation.
    filtered=('BEGIN:VCALENDAR\nVERSION:2.0\n'+'\n'.join(p['text'] for p in selected)+'\nEND:VCALENDAR\n').encode()
    accepted=0
    if selected:
        doc_id=store.save(source['id'],export_url,'Persönlicher Kalender · '+source['course_title'],'text/calendar',filtered)
        document=store.process(doc_id)
        for page in json.loads(document['pages']):
            candidate=calendar_candidate(page,source,groups,online_id)
            if candidate:
                db.ingest(document,candidate); accepted+=1
        db.execute("UPDATE documents SET analysis_state='success',analysis_at=? WHERE id=?",(now(),doc_id))
    config=json.loads(source['config']);config.update(online_id=online_id,assigned_groups=groups,course_type=matched['course_type'],registration_document=reg_id,
        exam_checked_at=now(),exam_document=exam_document,exam_entries=exam_count,exam_issues=exam_issues,exam_identity_documents=identity_documents)
    missing=len(selected)-accepted
    message=f'Fixplatz: {", ".join(groups)}. {accepted} eindeutige LV-Termine; {missing} angebotene/unklare Slots nicht als persönliche Buchung übernommen.'
    message+=f' {exam_count} Prüfungstermine mit getrenntem Anmeldestatus/Fenstern gelesen.'
    if not selected:message+=' Kalenderexport enthält keinen Termin dieses Kurses; bisherigen Bestand erhalten.'
    if exam_issues:message+=' '+' '.join(exam_issues)
    complete=bool(selected) and not missing and not exam_issues
    db.execute("UPDATE sources SET url=?,config=?,state=?,message=?,last_success=? WHERE id=?",(detail.url,encode(config),'success' if complete else 'partial',message,now(),source['id']))
    return {'source':source['id'],'complete':complete,'documents':1+bool(selected)+bool(exam_document)+len(identity_documents),'personal_group':groups,'calendar_entries':accepted,
        'exam_entries':exam_count,'offered_or_unclear':missing,'message':message}
