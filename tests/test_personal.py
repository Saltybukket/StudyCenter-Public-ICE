import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from studycenter_ice.db import Database,encode
from studycenter_ice.models import Candidate
from studycenter_ice.config import VIENNA
from studycenter_ice.app import action_state,calendar_events,item_timing,create_app,pin_state
from studycenter_ice.reminders import ReminderService
from studycenter_ice.personal import reconcile_relative
from studycenter_ice.presentation import item_tags
from studycenter_ice.ical import export_calendar
from studycenter_ice.tugrazonline import calendar_candidate,match_course,personal_courses,exam_candidates,TUGRAZonlineError
from studycenter_ice.config import Settings


def test_coverage_reports_saved_exam_check_and_keeps_items(tmp_path):
    from studycenter_ice.personal import coverage
    db=Database(tmp_path/'coverage.sqlite3')
    cid=db.execute("INSERT INTO courses(title,semester) VALUES('VO','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url,config) VALUES(?,'tugrazonline','Eigene LV','https://example.org',?)",
        (cid,encode({'exam_checked_at':'2030-10-13T10:00:00+00:00','exam_entries':0,'exam_issues':[]})))
    before=db.rows('SELECT * FROM items')
    checked=coverage(db)[0]
    assert checked['exam_coverage']=={'checked_at':'2030-10-13T10:00:00+00:00','offers':0,'booked':0,'issues':[]}
    assert 'Nicht vollständig erfasst' not in checked['exam_registration']
    db.execute('UPDATE sources SET config=? WHERE id=?',(encode({'exam_checked_at':'2030-10-13T10:00:00+00:00','exam_entries':1,'exam_issues':['Prüfungsseite nicht lesbar.']}),sid))
    assert 'Prüfungsabgleich: Prüfungsseite nicht lesbar.' in coverage(db)[0]['gaps']
    db.execute("UPDATE sources SET config='{}' WHERE id=?",(sid,))
    assert coverage(db)[0]['exam_coverage']['checked_at'] is None
    media=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','APK','https://apkpure.com/example')",(cid,))
    doc=document(db,media,'https://apkpure.com/example','Bewusst ausgelassen')
    db.execute("UPDATE documents SET analysis_state='pending' WHERE id=?",(doc['id'],))
    result=coverage(db)[0]
    assert result['pending']==0 and result['deferred_analysis']==1
    assert db.rows('SELECT * FROM items')==before


def document(db,sid,url,text,mime='text/html',title='Quelle',page=1):
    n=db.one('SELECT count(*) n FROM documents')['n']+1
    identifier=db.execute('INSERT INTO documents(source_id,url,title,mime,hash,fetched_at,original_path,pages,analysis_state,processing_state) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (sid,url,title,mime,str(n),f'2030-10-01T10:{n:02}:00+00:00','unused',encode([{'text':text,'page':page,'anchor':'original'}]),'success','success'))
    return db.one('SELECT * FROM documents WHERE id=?',(identifier,))


def test_sources_render_exam_facts_and_separate_deferred_media(tmp_path):
    from fastapi.testclient import TestClient
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    cid=db.execute("INSERT INTO courses(title,semester) VALUES('Prüfkurs','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url,config) VALUES(?,'tugrazonline','Eigene LV','https://example.org',?)",
        (cid,encode({'exam_checked_at':'2030-10-13T10:00:00+00:00','exam_entries':2,'exam_issues':['Eine Prüfungsseite nicht lesbar.']})))
    doc=document(db,sid,'https://example.org/exam','Bereits angemeldet: Prüfung am 20. Oktober 2030.')
    db.ingest(doc,Candidate(title='Prüfung',kind='exam',identity_key='tugrazonline:exam:1',identity_scope='course',
        date='2030-10-20',date_meaning='start',quote='Bereits angemeldet: Prüfung am 20. Oktober 2030.',page=1,anchor='exam'))
    media=db.execute("INSERT INTO sources(course_id,kind,title,url,state) VALUES(?,'public','Video','https://tube.tugraz.at/example','partial')",(cid,))
    ordinary=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Kursseite','https://example.org/course')",(cid,))
    for source_id in (media,ordinary):
        row=document(db,source_id,f'https://example.org/{source_id}','Gespeicherter Text')
        db.execute("UPDATE documents SET analysis_state='pending' WHERE id=?",(row['id'],))
    before=db.rows('SELECT * FROM items');evidence=db.rows('SELECT * FROM item_evidence');sources=db.rows('SELECT * FROM sources')
    client=TestClient(app)
    response=client.get('/sources');assert response.status_code==200
    dom=BeautifulSoup(response.text,'html.parser');exam=dom.select_one('.exam-coverage').get_text(' ',strip=True)
    assert '13.10.2030 12:00' in exam and 'Angebote: 2' in exam and 'Belegte Buchungen: 1' in exam
    assert 'Prüfungsabgleich: Eine Prüfungsseite nicht lesbar.' in dom.get_text(' ',strip=True)
    deferred=dom.select_one(f'#source-{media}')
    assert not deferred.has_attr('open') and 'bewusst zurückgestellt' in deferred.get_text(' ',strip=True)
    assert deferred.select_one('[data-source-analysis]').get_text(strip=True)=='Medienquelle bewusst auswerten'
    assert not deferred.select_one('.source-options').has_attr('open')
    assert 'Gespeicherte Auswertung fortsetzen' not in deferred.get_text(' ',strip=True)
    normal=dom.select_one(f'#source-{ordinary}')
    assert normal.has_attr('open') and 'Gespeicherte Auswertung fortsetzen' in normal.get_text(' ',strip=True)
    db.execute("UPDATE sources SET config='{}' WHERE id=?",(sid,))
    missing=BeautifulSoup(client.get('/sources').text,'html.parser').select_one('.exam-coverage').get_text(' ',strip=True)
    assert 'noch nicht geprüft' in missing and 'Angebote: nicht erfasst' in missing
    db.execute('UPDATE sources SET config=? WHERE id=?',(sources[0]['config'],sid))
    assert db.rows('SELECT * FROM items')==before and db.rows('SELECT * FROM item_evidence')==evidence
    assert db.rows('SELECT * FROM sources')==sources


def fixture(tmp_path):
    db=Database(tmp_path/'data.sqlite3')
    cid=db.execute("INSERT INTO courses(title,semester) VALUES('Prüfkurs','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'teachcenter','Kurs','https://tc.tugraz.at/main/course/view.php?id=1')",(cid,))
    return db,cid,sid


def test_quiz_window_evidence_roles_preserve_originals_and_real_conflicts(tmp_path):
    db,cid,sid=fixture(tmp_path)
    common=dict(title='Quiz 1',kind='exam',identity_key='Quiz 1',identity_scope='course',
                date='2030-10-13',time='18:00',end_date='2030-10-19',end_time='12:00',
                obligation='unclear',page=1,anchor='quiz')
    for index,meaning,quote in [(1,'window','Öffnet: 13.10. 18:00. Schließt: 19.10. 12:00.'),
                              (2,'start','Quiz 1 (offen von Di. 13.10. 18h -- Mo. 19.10. 12h).')]:
        doc=document(db,sid,f'https://example.org/quiz/{index}',quote)
        identifier=db.ingest(doc,Candidate(**common,date_meaning=meaning,quote=quote))
    rule='Mindestens 50% der Gesamtpunkte aller Quizzes; nach individuellem Start 60 Minuten.'
    doc=document(db,sid,'https://example.org/quiz/rules',rule)
    db.ingest(doc,Candidate(**(common | dict(date=None,time=None,end_date=None,end_time=None)),date_meaning='none',quote=rule))
    # Reproduce a persisted legacy flag; reading must repair only its interpretation.
    db.execute("UPDATE items SET conflict=1,status='doing',pinned=1,ignored=1,overrides=? WHERE id=?",(encode({'description':'Meine Notiz'}),identifier))
    before=db.one('SELECT * FROM items WHERE id=?',(identifier,))
    evidence=db.rows('SELECT * FROM item_evidence ORDER BY id');originals=db.rows('SELECT * FROM documents ORDER BY id')
    row=db.items()[0]
    assert not row['conflict'] and not row['date_conflict']
    assert item_timing(row['data'])==('2030-10-19','12:00:00',True)
    assert row['data']['obligation']=='unclear' and not row.get('booked_event')
    assert db.one('SELECT * FROM items WHERE id=?',(identifier,))==before
    assert db.rows('SELECT * FROM item_evidence ORDER BY id')==evidence and db.rows('SELECT * FROM documents ORDER BY id')==originals
    # A genuinely different closing time must remain a conflict, despite window wording.
    quote='Quiz 1 (offen von 13.10. 18h bis 19.10. 13h).'
    doc=document(db,sid,'https://example.org/quiz/contrary',quote)
    db.ingest(doc,Candidate(**(common | {'end_time':'13:00'}),date_meaning='start',quote=quote))
    row=db.items()[0]
    assert row['conflict'] and row['date_conflict']
    assert row['status']=='doing' and row['pinned'] and row['ignored'] and row['overrides']==before['overrides']
    # Ordinary start/end metadata and the attempt duration alone are not a window.
    from studycenter_ice.db import temporal_claim
    normal=Candidate(**common,date_meaning='start',quote='Prüfung ab 18 Uhr, 60 Minuten Bearbeitungszeit.').model_dump(mode='json')
    assert temporal_claim(normal)==normal


def test_quiz_closing_reminder_is_explicit_exact_and_not_participation(tmp_path):
    from fastapi.testclient import TestClient
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    cid=db.execute("INSERT INTO courses(title,semester) VALUES('WFI','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'teachcenter','Quizzes','https://example.org')",(cid,))
    quote='Quiz offen von 13.10. 18h bis 19.10. 12h. Nach individuellem Start 60 Minuten.'
    doc=document(db,sid,'https://example.org/quiz',quote)
    identifier=db.ingest(doc,Candidate(title='Quiz 1',kind='exam',date_meaning='window',date='2030-10-13',time='18:00',end_date='2030-10-19',end_time='12:00',obligation='unclear',quote=quote,page=1,anchor='quiz'))
    db.execute("UPDATE items SET status='doing',pinned=1,overrides=? WHERE id=?",(encode({'description':'Meine Notiz'}),identifier))
    before=db.one('SELECT * FROM items WHERE id=?',(identifier,));evidence=db.rows('SELECT * FROM item_evidence')
    sent=[];service=ReminderService(db,item_timing,SimpleNamespace(send=lambda *a:sent.append(a)))
    current=datetime(2030,10,18,12,tzinfo=VIENNA)
    def item():return next(i for i in db.items() if i['id']==identifier)
    assert not service.plans(item(),current)[0]
    client=TestClient(app)
    assert 'name="at_closing"' in client.get(f'/items/{identifier}').text
    response=client.post(f'/items/{identifier}/reminders',data={'mode':'standard','at_closing':'on'},headers={'X-CSRF-Token':app.state.csrf},follow_redirects=False)
    assert response.status_code==303
    row=item();plans,issues=service.plans(row,current)
    assert len(plans)==1 and plans[0]['rule']=='window:exact' and plans[0]['due_at']==plans[0]['target_at']=='2030-10-19T10:00:00+00:00'
    assert any('keine Einzelpflicht' in issue for issue in issues)
    from studycenter_ice.reminders import ReminderSettings
    assert not service.plans(row,current,settings=ReminderSettings(enabled=False))[0]
    assert not service.plans(row,current,settings=ReminderSettings(exam=False))[0]
    off=dict(row,reminder_profile=row['reminder_profile'] | {'mode':'off'})
    assert not service.plans(off,current)[0]
    assert not row['reminder_profile']['personal'] and not action_state(row,current)['booking_proven']
    assert row['data']['obligation']=='unclear'
    row['data']['end_time']=None
    assert not service.plans(row,current)[0]  # No invented closing clock.
    service.tick(current)
    assert db.one("SELECT count(*) n FROM reminder_jobs WHERE state='pending'")['n']==1
    service.tick(datetime(2030,10,19,12,0,1,tzinfo=VIENNA));service.tick(datetime(2030,10,19,12,0,2,tzinfo=VIENNA))
    assert len(sent)==1 and db.one("SELECT count(*) n FROM reminder_jobs WHERE state='delivered'")['n']==1
    assert db.one('SELECT * FROM items WHERE id=?',(identifier,))==before and db.rows('SELECT * FROM item_evidence')==evidence
    assert not db.rows('SELECT * FROM personal_reminders')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,attendance,title', [('exam','','Optionale Prüfung'), ('event','lecture','Freiwillige Vorlesung'), ('event','exercise','Angebotener Labslot')])
async def test_explicit_personal_choice_shared_by_agenda_calendar_mcp_and_reminders(tmp_path,kind,attendance,title):
    from fastapi.testclient import TestClient
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    cid=db.execute("INSERT INTO courses(title,semester) VALUES('Kurs','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Angebot','https://example.org')",(cid,))
    doc=document(db,sid,'https://example.org/offering','Optionaler Termin am 13. Oktober um 18 Uhr.')
    identifier=db.ingest(doc,Candidate(title=title,kind=kind,obligation='optional',date='2030-10-13',time='18:00',date_meaning='start',quote='Optionaler Termin am 13. Oktober um 18 Uhr.',anchor='offering',page=1))
    db.execute("UPDATE items SET status='doing',pinned=1,overrides=? WHERE id=?",(encode({'description':'Meine Notiz'}),identifier))
    before=db.one('SELECT * FROM items WHERE id=?',(identifier,));current=datetime(2030,10,13,12,tzinfo=VIENNA)
    client=TestClient(app)
    def item():return next(i for i in db.items() if i['id']==identifier)
    def preferences(selected):
        response=client.post(f'/items/{identifier}/reminders',data={'personal':'on' if selected else '', 'attendance':attendance,'mode':'standard'},headers={'X-CSRF-Token':app.state.csrf},follow_redirects=False)
        assert response.status_code==303
    def exported(row):return export_calendar([row],calendar_events([row]),{identifier:action_state(row,current)})
    assert action_state(item(),current)['offered'] and not calendar_events([item()])
    assert not app.state.reminders.plans(item(),current)[0]  # A pin is not a booking.
    preferences(True);row=item();state=action_state(row,current)
    assert state['personal'] and state['personally_planned'] and state['appointment'] and not state['offered'] and not state['booking_proven']
    if kind=='exam':assert pin_state(row,current)['pin_reason']=='Persönlich geplante Prüfung'
    assert {'planned'} <= {t['code'] for t in item_tags(row)} and 'booked' not in {t['code'] for t in item_tags(row)}
    event=calendar_events([row])[0];assert event['extendedProps']['personallyPlanned'] and not event['extendedProps']['bookingProven']
    raw,count=exported(row);assert count==1 and b'20301013T180000' in raw and b'DTEND' not in raw.split(b'BEGIN:VEVENT')[1]
    assert app.state.reminders.plans(row,current)[0]
    upcoming=await app.state.gateway.call('list_upcoming',{'date_from':'2030-10-13','date_to':'2030-10-13'})
    assert upcoming['items'][0]['id']==identifier and upcoming['items'][0]['personally_planned'] and not upcoming['items'][0]['booking_proven']
    preferences(False);assert action_state(item(),current)['offered'] and exported(item())[1]==0
    assert not app.state.reminders.plans(item(),current)[0]
    assert not (await app.state.gateway.call('list_upcoming',{'date_from':'2030-10-13','date_to':'2030-10-13'}))['items']
    assert db.one('SELECT * FROM items WHERE id=?',(identifier,))==before


def test_booking_activity_rule_same_item_and_no_past_notifications(tmp_path):
    db,cid,sid=fixture(tmp_path)
    url='https://tc.tugraz.at/main/mod/scheduler/view.php?id=8'
    booked='Dienstag, 13. Oktober 2030 18:00 – 19:00'
    offered='Montag, 12. Oktober 2030 18:00 – 19:00'
    doc=document(db,sid,url+'&forceview=1',booked,title='Setup Session Slots')
    candidate=Candidate(page=1,title='Setup Session Slot',kind='event',date='2030-10-13',time='18:00',end_date='2030-10-13',end_time='19:00',date_meaning='start',quote=booked,anchor='arbitrary-model-anchor')
    identifier=db.ingest(doc,candidate)
    other=db.ingest(doc,candidate.model_copy(update={'anchor':'other','date':candidate.date.replace(day=12),'end_date':candidate.date.replace(day=12),'quote':offered}))
    document(db,sid,url,'Aktuelle Zeitfenster\n'+booked+'\nIhre Eintragung war erfolgreich.\nVerfügbare Zeitfenster\n[DURCHGESTRICHEN: '+offered+']',title='Setup Session Slots')
    duty=document(db,sid,'https://example.org/admin.pdf','Mandatory Setup Session. To ensure the setup works, we require you to participate in a 60 minute setup session. Students that do not participate will be deregistered from the course.','application/pdf','Administrative Information',4)
    items={i['id']:i for i in db.items()};item=items[identifier]
    assert item['booked_event'] and item['data']['obligation']=='required'
    assert item['personal_resolution']['attendance'][0]['page']==4
    assert not items[other]['booked_event'] and items[other]['data']['obligation']=='unclear'
    assert {'attendance','booked'} <= {t['code'] for t in item_tags(item)}
    assert db.one('SELECT count(*) n FROM items')['n']==2
    sent=[];service=ReminderService(db,item_timing,SimpleNamespace(send=lambda *a:sent.append(a)))
    current=datetime(2030,10,13,12,tzinfo=VIENNA)
    plans,_=service.plans(item,current)
    assert datetime.fromisoformat(plans[0]['due_at']).astimezone(VIENNA).strftime('%H:%M')=='17:30'
    assert not service.plans(items[other],current)[0]
    ics,_=export_calendar(list(items.values()),calendar_events(list(items.values())),{i['id']:action_state(i,current) for i in items.values()})
    assert b'20301013T180000' in ics and b'20301012T180000' not in ics
    service.tick(datetime(2030,10,14,12,tzinfo=VIENNA));assert not sent
    document(db,sid,'https://example.org/updated.html','Setup Session attendance is optional.',title='Setup Session')
    item=next(i for i in db.items() if i['id']==identifier)
    assert item['personal_resolution']['issues'] and not service.plans(item,current)[0]
    document(db,sid,url,'Aktuelle Zeitfenster\n[DURCHGESTRICHEN: '+booked+']\nIhre Eintragung war erfolgreich.',title='Setup Session Slots')
    assert next(i for i in db.items() if i['id']==identifier)['data']['source_state']=='cancelled'


def test_vo_ue_card_identity_and_calendar_do_not_mix_groups():
    raw=''.join(f'<div class="mobile-flex-column"><div class="list-spacer"><a href="app/desktop/#/slc.tm.cp/student/courses/{num}">Messtechnik</a> - {group} {kind} | 2 SSt</div>Fixplatz zugewiesen</div>' for num,group,kind in [('11','Standardgruppe','VO'),('12','Gruppe A','UE')]).encode()
    entries=personal_courses(raw)
    actual_base='https://online.tugraz.at/tug_online/ee/ui/ca2/app/desktop/#/slc.tm.cp/student/myCourses'
    rooted=personal_courses(b'<base href="../../">'+raw,actual_base)
    assert rooted[0]['url'].startswith('https://online.tugraz.at/tug_online/ee/ui/ca2/app/desktop/')
    source={'course_title':'Messtechnik (UE)','semester':'2030W','config':'{"course_type":"UE"}'}
    assert match_course(entries,source)['online_id']=='12'
    assert all(e['confirmed'] for e in entries)
    with pytest.raises(TUGRAZonlineError):match_course(entries,source|{'course_title':'Messtechnik','config':'{}'})
    def page(kind,group='Gruppe A'):
        return {'page':1,'anchor':'uid','text':f'UID:unit@tu\nSUMMARY:Messtechnik {kind}, {group}\nURL:https://online.tugraz.at/tug_online/wbLv.wbShowLVDetail?pStpSpNr=12\nDESCRIPTION:fix; Abhaltung\nDTSTART:20301013T123000Z\nDTEND:20301013T140000Z'}
    assert calendar_candidate(page('UE'),source,['Gruppe A'],'12').time.hour==14
    assert calendar_candidate(page('VO'),source,['Gruppe A'],'12') is None
    assert calendar_candidate(page('UE','Gruppe B'),source,['Gruppe A'],'12') is None
    assert calendar_candidate(page('UE'),source,[],'12') is None


def test_exam_cards_separate_offers_status_and_windows_without_enrollment_inference(tmp_path):
    db,cid,sid=fixture(tmp_path)
    db.execute("UPDATE sources SET kind='tugrazonline' WHERE id=?",(sid,))
    source=db.source(sid);url='https://online.tugraz.at/tug_online/pages/slc.tm.cp/course/123'
    def card(status):
        return f'''<xm-exam-offer-list-entry><div class="first-column"><span class="appointment-day">29</span><span class="appointment-month-year">Jan 2031</span>12:00 - 14:00</div>
        <div class="exam-start-end-date">Anmeldezeitraum 12.10.2030, 10:00 - 26.01.2031, 23:55</div><div class="exam-start-end-date">Abmeldung bis 26.01.2031, 23:55</div>
        Aktuell angemeldet 12 Fixplatz zugewiesen {status}<a href="https://online.tugraz.at/tug_online/ee/rest/pages/slc.xm.ex/exam/987">Zur Prüfungsanmeldung</a>Prüfungsbeginn 12:15</xm-exam-offer-list-entry>'''
    raw=card('Nicht angemeldet');items,issues=exam_candidates(raw,source,'123',url)
    assert not issues and len(items)==3
    exam,registration,withdrawal=items
    assert str(exam.time)=='12:15:00' and str(exam.end_time)=='14:00:00' and exam.obligation=='optional'
    assert str(registration.date)=='2030-10-12' and str(registration.end_date)=='2031-01-26'
    assert withdrawal.end_date is None and item_timing(withdrawal.model_dump(mode='json'))[2]
    doc=document(db,sid,url,BeautifulSoup(raw,'html.parser').get_text(' ',strip=True))
    identifier=db.ingest(doc,exam);row=next(i for i in db.items() if i['id']==identifier)
    assert not row['booked_event'] and action_state(row,datetime(2030,10,13,tzinfo=VIENNA))['offered']
    db.execute("UPDATE items SET status='doing',pinned=1 WHERE id=?",(identifier,))
    raw=card('Bereits angemeldet');updated,_=exam_candidates(raw,source,'123',url)
    doc=document(db,sid,url,BeautifulSoup(raw,'html.parser').get_text(' ',strip=True))
    assert db.ingest(doc,updated[0])==identifier
    row=next(i for i in db.items() if i['id']==identifier)
    assert row['booked_event'] and row['status']=='doing' and row['pinned']
    assert 'booked' in {t['code'] for t in item_tags(row)}
    with pytest.raises(TUGRAZonlineError):exam_candidates(raw,source,'456',url)
    unlabelled=card('Nicht angemeldet').replace('/exam/987','/other/987')
    assert exam_candidates(unlabelled,source,'123',url)[1]
    observed={'id':987,'courseId':123,'courseSemesterShortName':{'value':'2030 W'},'examDate':{'value':'2031-01-29'},'examStart':{'value':'12:00:00'}}
    matched,issues=exam_candidates(unlabelled,source,'123',url,[observed])
    assert not issues and matched[0].identity_key=='tugrazonline:exam:987'
    assert exam_candidates(unlabelled,source,'123',url,[observed|{'courseId':456}])[1]
    assert exam_candidates(unlabelled,source,'123',url,[observed,observed|{'id':988}])[1]


def test_relative_checkbox_rule_shift_cancel_preserves_personal_values(tmp_path):
    db,cid,sid=fixture(tmp_path)
    quote='Kreuzen Sie wie gewohnt bis 22:00 am Vortag die Beispiele an!'
    doc=document(db,sid,'https://example.org/rule',quote)
    rule=db.ingest(doc,Candidate(page=1,title='Beispiele ankreuzen',kind='task',obligation='required',quote=quote,anchor='rule'))
    personal=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'tugrazonline','Kalender','https://online.tugraz.at/calendar')",(cid,))
    def unit(when,state='active',key='unit'):
        doc=document(db,personal,'https://online.tugraz.at/ics','Eigene konkrete Übungseinheit '+when,'text/calendar')
        return db.ingest(doc,Candidate(page=1,title='Übungseinheit '+key,kind='event',identity_scope='course',identity_key='tugrazonline:'+key,date=when,time='08:15',date_meaning='start',source_state=state,quote='Eigene konkrete Übungseinheit '+when,anchor=key))
    uid=unit('2030-10-27')
    assert reconcile_relative(db)==1 and reconcile_relative(db)==0
    row=db.one("SELECT * FROM items WHERE identity LIKE 'relative:%'");identifier=row['id']
    assert json.loads(row['payload'])['date']=='2030-10-26' and json.loads(row['payload'])['time']=='22:00:00'
    db.execute("UPDATE items SET status='doing',pinned=1,overrides=? WHERE id=?",(encode({'description':'Meine Planung'}),identifier))
    unit('2030-10-29');assert reconcile_relative(db)==0
    row=db.one('SELECT * FROM items WHERE id=?',(identifier,))
    assert json.loads(row['payload'])['date']=='2030-10-28' and row['status']=='doing' and row['pinned']==1 and json.loads(row['overrides'])['description']=='Meine Planung'
    assert db.one('SELECT count(DISTINCT document_id) n FROM item_evidence WHERE item_id=? AND current=1',(identifier,))['n']==2
    unit('2030-10-29','cancelled');reconcile_relative(db)
    assert json.loads(db.one('SELECT payload FROM items WHERE id=?',(identifier,))['payload'])['source_state']=='cancelled'
    assert db.one("SELECT count(*) n FROM items WHERE identity LIKE 'relative:%'")['n']==1
    unit('2030-10-29');db.execute("UPDATE sources SET kind='public' WHERE id=?",(personal,));reconcile_relative(db)
    assert json.loads(db.one('SELECT payload FROM items WHERE id=?',(identifier,))['payload'])['source_state']=='unclear'


@pytest.mark.parametrize('change,stage',[('cancel','snapshot'),('cancel','dispatch'),('move','dispatch'),('rule','dispatch'),('none','dispatch')])
def test_relative_reminder_checks_current_parents_before_dispatch(tmp_path,monkeypatch,change,stage):
    db,cid,sid=fixture(tmp_path)
    quote='Kreuzen Sie wie gewohnt bis 22:00 am Vortag die Beispiele an!'
    doc=document(db,sid,'https://example.org/rule',quote)
    rule=db.ingest(doc,Candidate(page=1,title='Beispiele ankreuzen',kind='task',obligation='required',quote=quote,anchor='rule'))
    source=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'tugrazonline','Kalender','https://example.org/calendar')",(cid,))
    doc=document(db,source,'https://example.org/ics','Eigene konkrete Übungseinheit 2030-10-27','text/calendar')
    unit=db.ingest(doc,Candidate(page=1,title='Übungseinheit',kind='event',identity_scope='course',identity_key='tugrazonline:unit',
        date='2030-10-27',time='08:15',date_meaning='start',quote='Eigene konkrete Übungseinheit 2030-10-27',anchor='unit'))
    assert reconcile_relative(db)==1
    derived=db.one("SELECT * FROM items WHERE identity LIKE 'relative:%'")
    db.execute("UPDATE items SET status='doing',pinned=1,overrides=? WHERE id=?",(encode({'description':'Meine Planung'}),derived['id']))
    def edit_parent():
        if change=='cancel':db.execute('UPDATE items SET overrides=? WHERE id=?',(encode({'source_state':'cancelled'}),unit))
        elif change=='move':db.execute('UPDATE items SET overrides=? WHERE id=?',(encode({'date':'2030-10-29'}),unit))
        elif change=='rule':db.execute('UPDATE items SET overrides=? WHERE id=?',(encode({'source_state':'cancelled'}),rule))
    sent=[];service=ReminderService(db,item_timing,SimpleNamespace(send=lambda *args:sent.append(args)))
    if stage=='snapshot':
        original=db.items;armed=True
        def snapshot(*args,**kwargs):
            nonlocal armed
            rows=original(*args,**kwargs)
            if armed:armed=False;edit_parent()
            return rows
        monkeypatch.setattr(db,'items',snapshot)
    else:
        original=service.reconcile
        def reconcile(current):original(current);edit_parent()
        monkeypatch.setattr(service,'reconcile',reconcile)
    service.tick(datetime(2030,10,26,20,tzinfo=VIENNA))
    assert len(sent)==(1 if change=='none' else 0)
    job=db.one('SELECT state FROM reminder_jobs WHERE item_id=? ORDER BY due_at DESC',(derived['id'],))
    assert job['state']==('delivered' if change=='none' else 'cancelled')
    after=db.one('SELECT * FROM items WHERE id=?',(derived['id'],))
    assert after['identity']==derived['identity'] and after['status']=='doing' and after['pinned']==1
    assert json.loads(after['overrides'])=={'description':'Meine Planung'}


def test_first_unit_rule_rebinds_to_real_calendar_uid_without_duplicate_on_shift(tmp_path):
    db,cid,sid=fixture(tmp_path)
    rule_doc=document(db,sid,'https://example.org/rule','Kreuzen Sie bis 22:00 am Vortag die Beispiele an!')
    rule=db.ingest(rule_doc,Candidate(title='Beispiele ankreuzen',kind='task',obligation='required',quote='Kreuzen Sie bis 22:00 am Vortag die Beispiele an!',page=1,anchor='rule'))
    personal=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'tugrazonline','Eigener Kalender','https://online.tugraz.at/calendar')",(cid,))
    registration=document(db,personal,'https://online.tugraz.at/registration','Gruppe 1 · Fixplatz zugewiesen')
    db.execute('UPDATE sources SET config=? WHERE id=?',(encode({'assigned_groups':['Gruppe 1'],'registration_document':registration['id']}),personal))
    plan=document(db,sid,'https://example.org/groups','Die erste Übungseinheit findet am 17. November statt.\nGruppe 1 | Dienstags, 08:15 - 09:45',title='Übungsgruppen')
    legacy=db.ingest(plan,Candidate(title='Erste Übungseinheit',kind='event',date='2030-11-17',date_meaning='start',quote='Die erste Übungseinheit findet am 17. November statt.',page=1,anchor='first'))
    assert reconcile_relative(db)==1
    first=db.one("SELECT * FROM items WHERE identity LIKE 'relative:%'");identifier=first['id'];identity=first['identity']
    db.execute("UPDATE items SET status='doing',pinned=1,overrides=? WHERE id=?",(encode({'description':'Meine Planung'}),identifier))
    def calendar(key,when,state='active'):
        doc=document(db,personal,'https://online.tugraz.at/ics','Eigener Kalender '+when,'text/calendar')
        return db.ingest(doc,Candidate(title='Lehrveranstaltung',kind='event',identity_key='tugrazonline:'+key,identity_scope='course',
            date=when,time='08:15',end_date=when,end_time='09:45',date_meaning='start',group='Gruppe 1',source_state=state,quote='Eigener Kalender '+when,page=1,anchor=key))
    uid=calendar('uid-first','2030-11-17');assert reconcile_relative(db)==0
    assert len(calendar_events(db.items()))==2  # One first unit plus its derived deadline, not both source copies.
    assert json.loads(db.one('SELECT payload FROM items WHERE id=?',(identifier,))['payload'])['related_key']==f'relative:{rule}:{uid}'
    calendar('uid-next','2030-11-24');assert reconcile_relative(db)==1 and reconcile_relative(db)==0
    calendar('uid-first','2030-11-18');assert reconcile_relative(db)==0
    assert next(i for i in db.items() if i['id']==legacy)['data']['date']=='2030-11-18'
    row=db.one('SELECT * FROM items WHERE id=?',(identifier,))
    assert row['identity']==identity and row['status']=='doing' and row['pinned'] and json.loads(row['overrides'])['description']=='Meine Planung'
    assert json.loads(row['payload'])['date']=='2030-11-17'
    calendar('uid-first','2030-11-18','cancelled');assert reconcile_relative(db)==0
    assert json.loads(db.one('SELECT payload FROM items WHERE id=?',(identifier,))['payload'])['source_state']=='cancelled'
    assert db.one("SELECT count(*) n FROM items WHERE identity LIKE 'relative:%'")['n']==2


@pytest.mark.asyncio
async def test_mcp_paging_text_continuation_and_source_status(tmp_path):
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db;gateway=app.state.gateway
    cid=db.execute("INSERT INTO courses(title,semester) VALUES('Kurs','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url,last_success) VALUES(?,'public','Quelle','https://example.org','2030-10-01T12:00:00+00:00')",(cid,))
    text='Ankreuzregel '+('Originaltext ' * 3000)
    doc=document(db,sid,'https://example.org/doc',text,'application/pdf')
    for n in range(55):db.ingest(doc,Candidate(page=1,title=f'Ankreuzregel {n}',kind='task',identity_key=str(n),date='2030-10-13',date_meaning='deadline',obligation='required',quote='Ankreuzregel Originaltext',anchor=str(n)))
    before=db.rows('SELECT * FROM items')
    first=await gateway.call('search',{'query':'Ankreuzregel','page':1});second=await gateway.call('search',{'query':'Ankreuzregel','page':2})
    assert first['next_page']==2 and first['has_more'] and first['results'][0]['entity_id']!=second['results'][0]['entity_id']
    a=await gateway.call('list_upcoming',{'date_from':'2030-10-01','date_to':'2030-10-30'})
    b=await gateway.call('list_upcoming',{'date_from':'2030-10-01','date_to':'2030-10-30','page':2})
    assert len(a['items'])==50 and len(b['items'])==5 and not b['has_more']
    collected='';offset=0
    while True:
        result=await gateway.call('get_source',{'document_id':doc['id'],'offset':offset});collected+=result['page']['text']
        assert result['truncated']==result['has_more']
        assert result['next_offset']==(offset+len(result['page']['text']) if result['has_more'] else None)
        if not result['has_more']:break
        offset=result['next_offset']
    assert collected==text and result['total_length']==len(text)
    end=await gateway.call('get_source',{'document_id':doc['id'],'offset':len(text)})
    assert end['page']['text']=='' and not end['truncated'] and not end['has_more'] and end['next_offset'] is None
    assert result['last_source_success']=='2030-10-01T12:00:00+00:00' and result['retrieved_at']!=result['last_source_success']
    assert (await gateway.call('get_status',{}))['courses'][0]['last_source_success']==result['last_source_success']
    for n in range(32):
        db.execute('INSERT INTO item_evidence(item_id,document_id,page,anchor,quote,payload) VALUES(?,?,?,?,?,?)',
                   (before[0]['id'],doc['id'],1,'proof:'+str(n),'Originaltext',before[0]['payload']))
    evidence=await gateway.call('get_item',{'item_id':before[0]['id']})
    continuation=await gateway.call('get_item',{'item_id':before[0]['id'],'page':evidence['next_page']})
    assert evidence['total']==33 and len(evidence['evidence'])==30 and len(continuation['evidence'])==3 and not continuation['has_more']
    with pytest.raises(ValueError):await gateway.call('search',{'query':'Ankreuzregel','date_from':'2030-01-01'})
    assert db.rows('SELECT * FROM items')==before


def test_course_cards_distinguish_real_conflicts_from_window_roles(tmp_path):
    from studycenter_ice.personal import coverage
    db,cid,sid=fixture(tmp_path)
    db.execute('UPDATE sources SET last_success=? WHERE id=?',('2030-10-13T10:00:00+00:00',sid))
    doc=document(db,sid,'https://example.org/windows','Organisation')
    common=dict(title='Arbeitsblatt 1',kind='task',identity_key='AB1',identity_scope='course',date_meaning='deadline',obligation='required',page=1)
    task=db.ingest(doc,Candidate(**common,date='2030-10-12',end_date='2030-10-27',quote='Ausgabe 12.10., Abgabe 27.10.',anchor='opening'))
    db.ingest(doc,Candidate(**common,date='2030-10-27',quote='Abgabe 27.10.',anchor='closing'))
    for suffix,title in [('A','Anmeldung zu einer Übungsgruppe'),('B','Anmeldung zur Übungsgruppe')]:
        db.ingest(doc,Candidate(title=title,kind='registration',identity_key='group-'+suffix,identity_scope='course',date='2030-10-16',time='10:00',date_meaning='window',obligation='required',source_state='unclear',uncertainty='Widerspruch: Schließfeld 16.10., Fließtext 09.10., jeweils 10 Uhr.',quote='Anmeldung bis 16.10. oder 09.10.',page=1,anchor=suffix))
    db.execute("UPDATE documents SET processing_state='partial',analysis_state='partial' WHERE id=?",(doc['id'],))
    db.execute("UPDATE items SET status='doing',pinned=1 WHERE id=?",(task,))
    before=db.rows('SELECT * FROM items');proofs=db.rows('SELECT * FROM item_evidence')
    card=coverage(db)[0]
    assert not any(g['title']=='Arbeitsblatt 1' for g in card['relevant_gaps'])
    groups=[g for g in card['relevant_gaps'] if g['title']=='Anmeldung zur Übungsgruppe']
    assert len(groups)==1 and len(groups[0]['items'])==2 and '16.10.' in groups[0]['detail']
    assert card['reading_gaps']==1
    assert not any('Leselücke' in g['title'] for g in card['relevant_gaps'])
    assert db.rows('SELECT * FROM items')==before and db.rows('SELECT * FROM item_evidence')==proofs


def test_course_card_filters_and_evidence_are_read_only(tmp_path):
    from fastapi.testclient import TestClient
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    cid=db.execute("INSERT INTO courses(title,semester,group_name) VALUES('Persönlicher Kurs','2030W','Von mir angegeben')")
    other=db.execute("INSERT INTO courses(title,semester) VALUES('Zweiter Kurs','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url,state,last_success,last_attempt,config) VALUES(?,'tugrazonline','Eigene LV','https://example.org/own','login_required',?,?,?)",(cid,'2030-10-13T10:00:00+00:00','2030-10-14T10:00:00+00:00',encode({'course_type':'UE','assigned_groups':['Standardgruppe'],'exam_checked_at':'2030-10-13T10:00:00+00:00','exam_entries':0})))
    doc=document(db,sid,'https://example.org/group','Fixplatz zugewiesen: Standardgruppe',title='Eigene LV-Anmeldung')
    db.execute('UPDATE sources SET config=? WHERE id=?',(encode({'course_type':'UE','assigned_groups':['Standardgruppe'],'registration_document':doc['id'],'exam_checked_at':'2030-10-13T10:00:00+00:00','exam_entries':0}),sid))
    before={t:db.rows('SELECT * FROM '+t+' ORDER BY id') for t in ('items','item_evidence','courses','sources')}
    client=TestClient(app)
    dom=BeautifulSoup(client.get('/sources').text,'html.parser')
    assert len(dom.select('.course-coverage-card'))==2
    card=dom.select_one(f'#course-coverage-{cid}')
    assert 'Standardgruppe' in card.select_one('.course-group').get_text() and card.select_one('.course-proof-tag')
    assert card.select_one('.course-group-proof a')['href']==f"/documents/{doc['id']}?page=1"
    assert 'kein vollständiger Kursabgleich' in card.select_one('.course-stand').get_text()
    assert dom.select_one('#sources-settings') and not dom.select_one('#sources-settings').has_attr('open')
    refreshed=BeautifulSoup(client.get('/sources?view=refresh').text,'html.parser')
    assert len(refreshed.select('.course-coverage-card'))==1 and refreshed.select_one(f'#course-coverage-{cid}')
    assert len(BeautifulSoup(client.get('/sources?view=clarify').text,'html.parser').select('.course-coverage-card'))==2
    assert len(BeautifulSoup(client.get('/sources?view=invalid').text,'html.parser').select('.course-coverage-card'))==2
    assert {t:db.rows('SELECT * FROM '+t+' ORDER BY id') for t in before}==before
