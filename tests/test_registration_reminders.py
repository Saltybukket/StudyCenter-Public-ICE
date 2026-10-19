import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from studycenter_ice.app import create_app, item_timing
from studycenter_ice.config import Settings, VIENNA
from studycenter_ice.db import Database, encode
from studycenter_ice.models import Candidate
from studycenter_ice.reminders import ItemPreferences, ReminderService, ReminderSettings, rule_label


def at(hour, minute=0):
    return datetime(2030, 10, 23, hour, minute, tzinfo=VIENNA)


def setup(db):
    cid=db.execute("INSERT INTO courses(title,semester) VALUES('Messtechnik','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Kurs','https://example.org')",(cid,))
    def add(**changes):
        value=dict(title='Gruppenanmeldung',kind='registration',identity_key='group-registration',identity_scope='course',date_meaning='window',
                   date='2030-10-23',time='10:00',end_date='2030-10-25',end_time='12:00',obligation='required',quote='Anmeldung von 23.10. 10:00 bis 25.10. 12:00',page=1,anchor='registration')
        value.update(changes)
        docid=db.execute("INSERT INTO documents(source_id,url,title,mime,hash,fetched_at,original_path) VALUES(?,?,'Fenster','text/plain',?,'2030-10-01T00:00:00+00:00','unused')",(sid,'https://example.org/'+str(len(db.rows('SELECT id FROM documents'))),str(len(db.rows('SELECT id FROM documents')))))
        doc=db.one('SELECT * FROM documents WHERE id=?',(docid,))
        identifier=db.ingest(doc,Candidate(**value))
        return identifier
    return cid,add


def read(db,identifier):
    return next(i for i in db.items() if i['id']==identifier)


def service(db):
    sent=[]
    return ReminderService(db,item_timing,SimpleNamespace(send=lambda *a:sent.append(a))),sent


def test_registration_default_zero_delivery_restart_and_shift(tmp_path):
    db=Database(tmp_path/'test.sqlite3');_,add=setup(db);identifier=add()
    worker,sent=service(db)
    plans,issues=worker.plans(read(db,identifier),at(8))
    assert not issues and [p['rule'] for p in plans]==['registration:10','registration:0']
    assert [datetime.fromisoformat(p['due_at']).astimezone(VIENNA) for p in plans]==[at(9,50),at(10)]
    worker.tick(at(9,50));worker.tick(at(10));worker.tick(at(10))
    ReminderService(db,item_timing,worker.adapter).tick(at(10,1))
    assert len(sent)==2 and 'Messtechnik' in sent[-1][1] and '23.10.2030 10:00' in sent[-1][1]
    assert 'zum Beginn' in sent[-1][1] and sent[-1][2].endswith('/items/'+str(identifier))
    db.execute('UPDATE items SET overrides=? WHERE id=?',(encode({'time':'11:00:00'}),identifier))
    worker.reconcile(at(10,1))
    assert db.one("SELECT count(*) n FROM reminder_jobs WHERE item_id=? AND state='pending'",(identifier,))['n']==2
    oldids={p['id'] for p in plans}
    assert not oldids & {r['id'] for r in db.rows("SELECT id FROM reminder_jobs WHERE state='pending'")}
    # A pending old opening is cancelled, rather than replayed under a second ID.
    db.execute('UPDATE items SET overrides=? WHERE id=?',(encode({'time':'12:00:00'}),identifier))
    worker.reconcile(at(10,2))
    assert db.one("SELECT count(*) n FROM reminder_jobs WHERE state='cancelled'")['n']==2
    defaults=ReminderSettings()
    assert (defaults.exam_minutes,defaults.exercise_minutes,defaults.lecture_minutes,defaults.task_minutes)==([60,30],[30],[30],[1440,120])
    assert rule_label('registration:0')=='Anmeldestart · zum Beginn'


@pytest.mark.parametrize('minute,expected',[(5,1),(15,1),(16,0)])
def test_wake_catches_one_stage_with_fifteen_minute_limit(tmp_path,minute,expected):
    db=Database(tmp_path/'test.sqlite3');_,add=setup(db);identifier=add();worker,sent=service(db)
    worker.tick(at(10,minute));worker.tick(at(10,minute))
    assert len(sent)==expected
    assert db.one("SELECT count(*) n FROM reminder_jobs WHERE item_id=? AND state='pending'",(identifier,))['n']==0
    # A custom warning caught up after the target cannot replay again after restart.
    db.execute("DELETE FROM reminder_jobs")
    db.execute('INSERT INTO item_preferences VALUES(?,?)',(identifier,encode({'mode':'custom','minutes':[10]})))
    worker.tick(at(10,5))
    before=len(sent)
    db.execute('UPDATE item_preferences SET value=? WHERE item_id=?',(encode({'mode':'custom','minutes':[10,0]}),identifier))
    ReminderService(db,item_timing,worker.adapter).tick(at(10,6))
    assert len(sent)==before


def test_relevance_booking_missing_clock_and_distinct_roles(tmp_path):
    db=Database(tmp_path/'test.sqlite3');cid,add=setup(db);identifier=add();worker,_=service(db)
    item=read(db,identifier)
    # No prior assigned group needed; task completion is not an official booking.
    db.execute("UPDATE items SET status='done' WHERE id=?",(identifier,))
    assert len(worker.plans(read(db,identifier),at(8))[0])==2
    assert not worker.plans(dict(item,booked_event=True),at(8))[0]
    assert not worker.plans(dict(item,ignored=True),at(8))[0]
    assert not worker.plans(dict(item,course_active=False),at(8))[0]
    assert not worker.plans(dict(item,data=item['data']|{'source_state':'cancelled'}),at(8))[0]
    for settings in (ReminderSettings(enabled=False),ReminderSettings(registration=False)):
        assert not worker.plans(item,at(8),settings=settings)[0]
    assert not worker.plans(dict(item,reminder_profile={'mode':'off'}),at(8))[0]
    assert not worker.plans(dict(item,evidence_resolution={'unresolved_fields':['obligation']}),at(8))[0]
    unknown=add(identity_key='unknown',time=None)
    assert not worker.plans(read(db,unknown),at(8),settings=ReminderSettings(unknown_task=True,unknown_events=True))[0]
    missing=add(identity_key='closing-only',end_date=None,end_time=None)
    assert not worker.plans(read(db,missing),at(8))[0]
    quiz=add(identity_key='quiz',kind='exam',obligation='unclear')
    assert not worker.plans(read(db,quiz),at(8))[0]
    withdrawal=add(identity_key='tugrazonline:exam:123:withdrawal',obligation='optional')
    assert not worker.plans(dict(read(db,withdrawal),reminder_profile={'personal':True}),at(8))[0]
    offered=add(identity_key='tugrazonline:exam:123:registration',obligation='optional',description='Nicht angemeldet. Anmeldezeitraum')
    offer=read(db,offered)
    assert not worker.plans(offer,at(8))[0]
    assert len(worker.plans(dict(offer,reminder_profile={'personal':True}),at(8))[0])==2
    exam=add(identity_key='tugrazonline:exam:123',kind='exam',date_meaning='start',obligation='optional')
    db.execute('INSERT INTO item_preferences VALUES(?,?)',(exam,encode({'personal':True})))
    assert len(worker.plans(offer,at(8))[0])==2
    booked=dict(offer,source_payload=offer['source_payload']|{'description':'Bereits angemeldet. Anmeldezeitraum','quote':'Bereits angemeldet · Anmeldezeitraum'})
    assert not worker.plans(dict(booked,reminder_profile={'personal':True}),at(8))[0]
    quiet=ReminderSettings(quiet=True,quiet_start='09:00',quiet_end='11:00')
    db.execute("INSERT INTO app_preferences VALUES('notifications',?)",(quiet.model_dump_json(),))
    worker.tick(at(10))
    assert db.one("SELECT count(*) n FROM reminder_jobs WHERE state='delivered'")['n']==0


def test_only_opening_conflict_blocks_its_precise_reminder(tmp_path):
    db=Database(tmp_path/'test.sqlite3');_,add=setup(db)
    identifier=add()
    add(end_date='2030-10-26')
    worker,_=service(db);item=read(db,identifier)
    assert item['date_conflict'] and set(item['evidence_resolution']['unresolved_fields'])=={'closing_date','closing_time'}
    assert len(worker.plans(item,at(8))[0])==2
    # Also preserve the independently timed opening when only the closing clock conflicts.
    internal=add(identity_key='internal',uncertainty='Widerspruch zwischen den Schließzeiten 12:00 und 16:00.')
    assert len(worker.plans(read(db,internal),at(8))[0])==2
    opening=add(identity_key='disputed')
    add(identity_key='disputed',date='2030-10-24')
    assert not worker.plans(read(db,opening),at(8))[0]
    # Exact registration-section role evidence can supplement a closing-only item.
    point=add(identity_key='source-role',date='2030-10-25',time='12:00',end_date=None,end_time=None)
    projected=read(db,point)
    role={'date':'2030-10-23','time':'10:00:00','proofs':[{'document_id':1,'page':1,'quote':'Anmeldestart'}]}
    projected.update(date_conflict=True,evidence_resolution={'registration_opening':role,'internal_conflict':True,'internal_unresolved_fields':['closing_date','closing_time']})
    plans,_=worker.plans(projected,at(8))
    assert len(plans)==2 and datetime.fromisoformat(plans[0]['target_at']).astimezone(VIENNA)==at(10)
    projected['data']=projected['data']|{'source_state':'unclear'}
    assert not worker.plans(projected,at(8))[0]
    projected['evidence_resolution']['temporal_source_uncertainty']=True
    assert len(worker.plans(projected,at(8))[0])==2
    cancelled=dict(projected,data=projected['data']|{'source_state':'cancelled'})
    assert not worker.plans(cancelled,at(8))[0]
    projected['overrides']=encode({'time':'16:00:00'})
    assert worker.plans(projected,at(8))[0][0]['target_at']==plans[0]['target_at']
    projected['evidence_resolution']['internal_unresolved_fields'].append('opening_time')
    projected['overrides']='{}'
    assert not worker.plans(projected,at(8))[0]


def test_item_and_settings_forms_allow_zero_only_for_anmeldestart(tmp_path):
    from fastapi.testclient import TestClient
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db;_,add=setup(db)
    identifier=add();exam=add(identity_key='exam',kind='exam',date_meaning='start')
    client=TestClient(app);headers={'X-CSRF-Token':app.state.csrf}
    assert 'name="registration_minutes"' in client.get('/settings').text
    assert 'Diese Anmeldung auswählen / beobachten' in client.get('/items/'+str(identifier)).text
    db.execute('INSERT INTO item_preferences VALUES(?,?)',(identifier,encode({'other_personal_preference':{'keep':True}})))
    response=client.post(f'/items/{identifier}/reminders',data={'mode':'custom','minutes':'10, 0','personal':'on'},headers=headers,follow_redirects=False)
    assert response.status_code==303
    assert read(db,identifier)['reminder_profile']['minutes']==[10,0]
    assert read(db,identifier)['reminder_profile']['other_personal_preference']=={'keep':True}
    assert client.post(f'/items/{exam}/reminders',data={'mode':'custom','minutes':'0'},headers=headers).status_code==400
    with pytest.raises(ValueError):ReminderSettings(exam_minutes=[0])
    with pytest.raises(ValueError):ItemPreferences(minutes=[0])
    settings=ReminderSettings(registration_minutes=[20,0],sound=False)
    form={key:('on' if value else '') if isinstance(value,bool) else ', '.join(map(str,value)) if isinstance(value,list) else value.isoformat() for key,value in settings.model_dump().items()}
    response=client.post('/settings/notifications',data=form,headers=headers,follow_redirects=False)
    assert response.status_code==303
    assert app.state.reminders.settings().registration_minutes==[20,0]
    assert json.loads(db.one("SELECT value FROM app_preferences WHERE name='notifications'")['value'])['exam_minutes']==[60,30]
