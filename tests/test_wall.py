import re
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from studycenter_ice.app import create_app, pin_state, action_state, calendar_events
from studycenter_ice.config import Settings, VIENNA
from studycenter_ice.db import Database
from studycenter_ice.models import Candidate


def test_action_closing_dates_precision_registration_and_offers():
    now = datetime(2026,10,12,16,tzinfo=VIENNA)
    def entry(**values):
        d=Candidate(title='Organisatorischer Eintrag',kind='task',obligation='required',
                    quote='Belegter Eintrag',page=1,anchor='test',**values).model_dump(mode='json')
        return {'id':1,'data':d,'status':'open','ignored':0,'conflict':0,'pinned':0,'identity':'test'}
    item=entry(date='2026-10-01',time='08:00',end_date='2026-10-14',date_meaning='window')
    s=action_state(item,now)
    assert s['actionable'] and s['closing'] and s['action_date']=='2026-10-14'
    assert s['action_time'] is None and s['remaining']==2 and s['relative']=='In 2 Tagen' and not s['urgent']
    assert calendar_events([item])[0]['start']=='2026-10-14'
    item['data']['end_time']='15:00:00'
    assert action_state(item,now)['urgent']
    item['data'].update(date='2026-10-14',end_time=None)
    assert action_state(item,now)['action_time'] is None  # Same-day start time is not a closing time.
    item['data'].update(kind='registration',date='2026-10-01',end_date='2026-10-13')
    s=action_state(item,now)
    assert s['action_label']=='Anmeldeschluss' and s['urgent']
    item['data'].update(end_date=None,date='2026-10-13',quote='Öffnet am 13. Oktober')
    assert not action_state(item,now)['closing']
    assert action_state(item,now)['action_label']=='Anmeldeöffnung'
    item['data']['quote']='Opens on 12 October until 13 October'
    assert not action_state(item,now)['closing']
    assert action_state(item,now)['action_label']=='Anmeldung · Datum unklar'
    item['data']['quote']='Schließt am 13. Oktober'
    assert action_state(item,now)['closing']
    item['data']['title']='Anmeldung oder Abmeldung zur Übung'
    assert action_state(item,now)['action_label']=='An-/Abmeldeschluss'
    item['data']['title']='Abmeldung von der Übung'
    assert action_state(item,now)['action_label']=='Abmeldeschluss'
    item['status']='done'
    assert not action_state(item,now)['actionable'] and not action_state(item,now)['urgent']
    item['status']='open'; item['data'].update(kind='event',date_meaning='start',obligation='required',
        uncertainty='Mehrere alternative Termine; keine persönliche Slot-Zuteilung angegeben.')
    assert not action_state(item,now)['appointment']
    item['personal_event']=True
    assert action_state(item,now)['appointment']
    item['data'].update(group='Lab B');item['course_group']='Lab A'
    assert not action_state(item,now)['appointment']
    item['data'].update(source_state='cancelled')
    assert not action_state(item,now)['actionable']


def test_wall_deadline_boundaries_cancellation_notes_and_daily_ray(tmp_path):
    now = datetime(2026,10,11,15,tzinfo=VIENNA)
    value = Candidate(title='Abgabe',kind='task',date='2026-10-15',time='15:00:00',
                      date_meaning='deadline',quote='Eigene Notiz',page=1,anchor='test').model_dump(mode='json')
    item = {'data':value,'status':'open','ignored':0,'conflict':0}
    assert not pin_state(item,now)['auto_pin']
    value['time']='14:59:00'
    assert pin_state(item,now)['auto_pin']
    value['date']='2026-10-13';value['time']='15:00:00'
    assert not pin_state(item,now)['urgent']
    value['time']='14:59:00'
    assert pin_state(item,now)['note_color']=='yellow'
    value.update(end_date='2026-10-17',end_time=None)
    assert not pin_state(item,now)['auto_pin']  # Closing date decides a deadline range.
    value.update(end_date=None,end_time=None)
    value['date_meaning']='recommended'
    assert not pin_state(item,now)['auto_pin']
    value.update(kind='exam',date_meaning='start',source_state='active',date='2027-02-05')
    assert pin_state(item,now)['auto_pin']
    value['source_state']='cancelled'
    assert not pin_state(item,now)['auto_pin']
    value['source_state']='active'
    item.update(status='open',conflict=1,date_conflict=False)
    assert pin_state(item,now)['auto_pin']  # Obligation conflict is not a date conflict.
    item['date_conflict']=True
    assert not pin_state(item,now)['auto_pin']
    value['source_state']='active';item['status']='done'
    assert not pin_state(item,now)['auto_pin']
    settings=Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        page=client.get('/pins')
        csrf=re.search(r'name="csrf-token" content="([^"]+)"',page.text)[1]
        response=client.post('/notes',data={'csrf':csrf,'title':'Meine Lernnotiz','text':'Aus eigener Planung','date':'2026-10-12'})
        assert response.status_code==200
        db=Database(tmp_path/'studycenter_ice.sqlite3');note=db.items()[0]
        assert note['pinned'] and note['status']=='open' and note['data']['kind']=='task'
        assert 'Meine Lernnotiz' in client.get('/board').text
        assert 'sticky-note' in client.get('/pins').text
        ray=client.get('/?days=14').text
        assert ray.count('data-day=')==15 and 'time-branch' in ray
        assert client.post('/notes',data={'csrf':csrf,'title':'Ungültig','date':'bad'}).status_code==400


def test_postit_colors_use_proven_closing_time_and_vienna_date():
    # Vienna's autumn clock change adds an elapsed hour to this local span.
    now = datetime(2030,10,26,22,tzinfo=VIENNA)
    value = Candidate(title='Abgabe',kind='task',date='2030-10-27',time='03:00',
        date_meaning='deadline',obligation='required',quote='Abgabe bis 03:00',page=1,anchor='test').model_dump(mode='json')
    item = {'data':value,'status':'open','ignored':0,'conflict':0}
    assert pin_state(item,now)['note_color']=='red'
    assert pin_state(item,now.astimezone(timezone.utc))['note_color']=='red'
    value['time']='03:01:00'
    assert pin_state(item,now)['note_color']=='yellow'
    value.update(date='2030-10-26',time=None)
    assert pin_state(item,now)['note_color']=='yellow'
    assert not pin_state(item,now)['imminent'] and not pin_state(item,now)['expired']
    value['time']='21:59:00'
    state=pin_state(item,now)
    assert state['note_color']=='orange' and state['pin_label']=='Frist abgelaufen'
    assert '2 Tagen' not in state['pin_label']
    value.update(date='2030-10-27',time='03:00:00',date_meaning='window',end_date='2030-10-29',end_time=None)
    assert pin_state(item,now)['note_color']=='green'  # The opening time is not the closing time.
    value.update(end_date='2030-10-27',end_time='03:00:00')
    assert pin_state(item,now)['note_color']=='red'
    item['date_conflict']=True
    assert pin_state(item,now)['note_color']=='green'
    item['date_conflict']=False;value.update(obligation='optional')
    assert pin_state(item,now)['note_color']=='green' and not pin_state(item,now)['auto_pin']
    value['obligation']='required';item['status']='done'
    assert not pin_state(item,now)['expired'] and not pin_state(item,now)['auto_pin']


def test_action_relative_hours_match_elapsed_time_across_vienna_clock_changes():
    value = Candidate(title='Belegte Abgabe',kind='task',date='2030-10-27',time='03:00',
        date_meaning='deadline',obligation='required',quote='Abgabe bis 03:00',page=1,anchor='test').model_dump(mode='json')
    item = {'data':value,'status':'open','ignored':0,'conflict':0}
    autumn = datetime(2030,10,26,22,tzinfo=VIENNA)
    assert action_state(item,autumn)['relative']=='In 6 Stunden'
    assert action_state(item,autumn.astimezone(timezone.utc))['remaining']==.25
    assert pin_state(item,autumn)['imminent']
    value.update(date='2030-03-31',time='03:00:00')
    spring = datetime(2030,3,30,22,tzinfo=VIENNA)
    assert action_state(item,spring)['relative']=='In 4 Stunden'
    # Without a source clock, date-only precision remains a calendar day.
    value['time']=None
    assert action_state(item,spring)['remaining']==1
    assert action_state(item,spring)['relative']=='Morgen'


def test_overview_focus_keeps_lectures_in_agenda_without_hiding_deadlines(tmp_path,monkeypatch):
    from studycenter_ice import app as app_module
    from fastapi.responses import HTMLResponse
    fixed=datetime(2030,10,26,9,tzinfo=VIENNA)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls,tz=None): return cls.fromtimestamp(fixed.timestamp(),tz)
    monkeypatch.setattr(app_module,'datetime',FixedDatetime)
    app=create_app(Settings(data_dir=tmp_path))
    def item(identifier,**changes):
        value=Candidate(title='Persönliche Vorlesung',kind='event',date='2030-10-26',
            time='10:00',date_meaning='start',quote='Eigener Kalender',page=1,anchor='test').model_dump(mode='json')|changes
        return {'id':identifier,'identity':'fixture:'+str(identifier),'data':value,'status':'open',
                'ignored':0,'conflict':0,'pinned':0,'personal_event':True,'seen':1,'changed_at':'2030-10-25T08:00:00+00:00','history':[]}
    lectures=[item(i) for i in range(1,8)]
    deadline=item(8,title='Abgabe',kind='task',date='2030-10-27',obligation='required',date_meaning='deadline')
    required=item(9,title='Pflichtübung',obligation='required')
    rows=lectures+[deadline,required]
    app.state.db.items=lambda active_only=False:rows
    captured={}
    # Inspect the route context before template presentation without touching data.
    monkeypatch.setattr(app_module.Jinja2Templates,'TemplateResponse',lambda self,request,name,context,**kw:(captured.update(context) or HTMLResponse('ok')))
    response=TestClient(app).get('/overview')
    assert response.status_code==200
    assert {i['id'] for i in captured['focus_actions']}=={8,9}
    assert captured['focus_action_count']==2
    assert {i['id'] for i in captured['week']}==set(range(1,10))
    assert rows==lectures+[deadline,required]
    # A relevant conflicting deadline precedes a later clear deadline, but not
    # today's reliably evidenced required appointment. Conflicts aren't acute.
    deadline['data']['date']='2030-11-04'
    clarification=item(10,title='Anmeldeschluss prüfen',kind='registration',date='2030-10-26',
        end_date='2030-10-27',end_time='10:00',date_meaning='window',obligation='required')
    clarification.update(conflict=True,date_conflict=True)
    rows.append(clarification)
    assert TestClient(app).get('/overview').status_code==200
    assert [i['id'] for i in captured['focus_actions']]==[9,10,8]
    assert captured['focus_urgent']==1
    assert {i['id'] for i in captured['week']}==set(range(1,8))|{9,10}


def test_postit_filters_include_target_day_keep_expired_and_show_all_kinds(tmp_path,monkeypatch):
    from bs4 import BeautifulSoup
    from studycenter_ice import app as app_module
    from studycenter_ice.documents import DocumentStore
    fixed=datetime(2030,10,26,22,tzinfo=VIENNA)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls,tz=None):
            return cls.fromtimestamp(fixed.timestamp(),tz) if tz else cls.fromtimestamp(fixed.timestamp())
    monkeypatch.setattr(app_module,'datetime',FixedDatetime)
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    cid=db.execute("INSERT INTO courses(title,semester) VALUES('Prüfkurs','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Quelle','https://example.org')",(cid,))
    document=DocumentStore(db,app.state.settings).save(sid,'https://example.org/a','Plan','text/plain',b'Plan')
    doc=db.one('SELECT * FROM documents WHERE id=?',(document,))
    def add(title,**changes):
        data=dict(title=title,kind='task',date='2030-10-27',date_meaning='deadline',obligation='required',
            quote='Abgabe',page=1,anchor=title,identity_key=title,identity_scope='course')
        identifier=db.ingest(doc,Candidate(**(data|changes)))
        db.execute('UPDATE items SET pinned=1 WHERE id=?',(identifier,))
        return identifier
    expired=add('Alte offene Frist',date='2030-10-20')
    target=add('Ende des Zieltags',time='23:59')
    outside=add('Nach dem Zieltag',date='2030-10-28')
    window=add('Späterer Fensterschluss',date='2030-10-25',time='08:00',end_date='2030-11-04',date_meaning='window')
    lecture=add('Vorlesung Algebra',kind='event',date_meaning='start')
    exercise=add('Übung 2',kind='event',date_meaning='start',obligation='optional')
    notice=add('Wichtiger Hinweis ohne Termin',kind='notice',date=None,date_meaning='unclear')
    db.execute('UPDATE items SET pinned=0 WHERE id=?',(notice,))
    offered=add('Ungebuchter Prüfungsslot',kind='exam',date_meaning='start',obligation='optional')
    db.execute('UPDATE items SET pinned=0 WHERE id=?',(offered,))
    completed=add('Erledigte Notiz')
    db.execute("UPDATE items SET status='done' WHERE id=?",(completed,))
    before=db.rows('SELECT * FROM items ORDER BY id')
    with TestClient(app) as client:
        for url in ('/board?due=1','/pins?due=1'):
            dom=BeautifulSoup(client.get(url).text,'html.parser')
            assert dom.select_one(f'[data-item-id="{expired}"].note-orange')
            assert dom.select_one(f'[data-item-id="{target}"]')
            assert not dom.select_one(f'[data-item-id="{outside}"]')
            assert not dom.select_one(f'[data-item-id="{window}"]')
            assert not dom.select_one(f'[data-item-id="{completed}"]')
            assert len(dom.select('.due-chip'))==4
            assert 'Frist abgelaufen' in dom.select_one(f'[data-item-id="{expired}"]').get_text()
        dom=BeautifulSoup(client.get('/board').text,'html.parser')
        assert dom.select_one(f'[data-item-id="{lecture}"]')
        assert dom.select_one(f'[data-item-id="{notice}"]')
        exercise_card=dom.select_one(f'[data-item-id="{exercise}"]')
        assert exercise_card and 'Angebotener Termin, nicht gebucht' in exercise_card.get_text()
        offered_card=dom.select_one(f'[data-item-id="{offered}"]')
        assert offered_card and 'Angebotener Termin, nicht gebucht' in offered_card.get_text()
        assert not dom.select_one(f'[data-item-id="{completed}"]')
        assert '/archive' in client.get('/board').text
        assert not BeautifulSoup(client.get('/board?personal=true').text,'html.parser').select('[data-item-id]')
        assert client.get('/board?status=done',follow_redirects=False).headers['location'].startswith('/archive?')
        # The clicked chip follows the hidden current value in native form order.
        submitted=BeautifulSoup(client.get('/board?due=all&due=1').text,'html.parser')
        assert {n['data-item-id'] for n in submitted.select('[data-item-id]')}=={str(expired),str(target),str(exercise),str(lecture),str(offered)}
    assert db.rows('SELECT * FROM items ORDER BY id')==before
