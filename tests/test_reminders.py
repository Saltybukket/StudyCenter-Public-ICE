import json
from datetime import datetime,timedelta
from types import SimpleNamespace
from studycenter_ice.app import item_timing
from studycenter_ice.config import VIENNA
from studycenter_ice.db import Database,encode
from studycenter_ice.models import Candidate
from studycenter_ice.reminders import ReminderService,ReminderSettings
from studycenter_ice.presentation import item_tags


def test_native_rules_precision_reschedule_status_restart_and_tags(tmp_path):
    db=Database(tmp_path/'studycenter_ice.sqlite3');sent=[]
    service=ReminderService(db,item_timing,SimpleNamespace(send=lambda *args:sent.append(args)))
    current=datetime(2030,10,23,8,0,tzinfo=VIENNA)
    def own(title,kind='task',**extra):
        value=Candidate(title=title,kind=kind,date='2030-10-23',time='10:00',date_meaning='deadline' if kind=='task' else 'start',obligation='required',quote='Eigene Planung',page=1,anchor='user',**extra)
        identifier=db.execute('INSERT INTO items(identity,payload,created_at,changed_at) VALUES(?,?,?,?)',('note:'+title,value.model_dump_json(),'2030-10-01','2030-10-01'))
        return next(i for i in db.items() if i['id']==identifier)
    exam=own('Prüfung','exam');plans,_=service.plans(exam,current)
    assert [datetime.fromisoformat(p['due_at']).astimezone(VIENNA).hour for p in plans]==[9,9]
    assert [datetime.fromisoformat(p['due_at']).astimezone(VIENNA).minute for p in plans]==[0,30]
    exercise=own('Lab','event');assert service.plans(exercise,current)[0]==[]  # Generic required does not invent attendance.
    exercise['reminder_profile']={'attendance':'exercise'}
    assert len(service.plans(exercise,current)[0])==1
    lecture=own('Vorlesung','event');lecture['reminder_profile']={'attendance':'lecture'}
    assert service.plans(lecture,current)[0][0]['rule']=='lecture:30'
    task=own('Bearbeitungsfenster',end_date='2030-10-24',end_time='12:00')
    plans,_=service.plans(task,current)
    assert datetime.fromisoformat(plans[0]['due_at']).astimezone(VIENNA)==datetime(2030,10,23,12,tzinfo=VIENNA)
    task['data']['end_time']=None
    assert not service.plans(task,current)[0] and 'Uhrzeit' in service.plans(task,current)[1][0]
    settings=ReminderSettings(unknown_task=True)
    db.execute('INSERT INTO app_preferences VALUES(?,?)',('notifications',settings.model_dump_json()))
    plan=service.plans(task,current)[0][0]
    assert datetime.fromisoformat(plan['due_at']).astimezone(VIENNA)==datetime(2030,10,23,9,tzinfo=VIENNA)
    # A document coverage issue does not relabel a known date as unreliable.
    task['data']['uncertainty']='Grafik-Leselücke auf anderen PDF-Seiten'
    assert not any(t['code']=='specific' for t in item_tags(task))
    exercise['booked_event']=True;exercise['identity']='source:lab'
    codes={t['code'] for t in item_tags(exercise)}
    assert {'booked','attendance','open'}<=codes and 'done' not in codes
    service.tick(datetime(2030,10,23,9,40,tzinfo=VIENNA))
    assert len([s for s in sent if s[1].startswith('Prüfung')])==1  # Latest stage after wake, not both.
    restarted=ReminderService(db,item_timing,service.adapter);restarted.tick(datetime(2030,10,23,9,41,tzinfo=VIENNA))
    assert len([s for s in sent if s[1].startswith('Prüfung')])==1
    db.execute('UPDATE items SET overrides=? WHERE id=?',(encode({'time':'15:00:00'}),exam['id']))
    restarted.reconcile(current)
    assert db.one("SELECT count(*) n FROM reminder_jobs WHERE item_id=? AND state='pending'",(exam['id'],))['n']==2
    db.execute("UPDATE items SET status='done' WHERE id=?",(exam['id'],));restarted.reconcile(current)
    assert db.one("SELECT count(*) n FROM reminder_jobs WHERE item_id=? AND state='pending'",(exam['id'],))['n']==0
    unknown=own('Angebot','event');unknown['identity']='source:offer';unknown['data']['kind']='exam'
    assert not service.plans(unknown,current)[0]
    offered=own('Lab-Alternativen','event');offered['identity']='source:enrolled';offered['personal_event']=True
    offered['reminder_profile']={'attendance':'exercise'}
    assert not service.plans(offered,current)[0]  # Course calendar alone does not book a particular lab slot.
    optional=own('Optionale Vorlesung','event');optional['identity']='source:optional';optional['personal_event']=True
    optional['data']['obligation']='optional';optional['data']['description']='No mandatory attendance.'
    assert not any(t['code']=='attendance' for t in item_tags(optional))
    assert not service.plans(optional,current)[0]


def test_reminder_backup_restore_keeps_preferences_and_delivery_state(tmp_path):
    import shutil
    from fastapi.testclient import TestClient
    from studycenter_ice.app import create_app
    from studycenter_ice.backup import create_backup
    from studycenter_ice.config import Settings
    settings=Settings(data_dir=tmp_path);app=create_app(settings);db=app.state.db;sent=[]
    adapter=SimpleNamespace(send=lambda *args:sent.append(args))
    value=Candidate(title='Eigene Probeprüfung',kind='exam',date='2030-10-23',time='10:00',date_meaning='start',obligation='required',quote='Eigene Planung',page=1,anchor='user')
    identifier=db.execute('INSERT INTO items(identity,payload,created_at,changed_at) VALUES(?,?,?,?)',('note:restore',value.model_dump_json(),'2030-10-01','2030-10-01'))
    db.execute('INSERT INTO item_preferences VALUES(?,?)',(identifier,encode({'mode':'custom','minutes':[60,30],'personal':True})))
    db.execute("INSERT INTO app_preferences VALUES('notifications',?)",(ReminderSettings(sound=False).model_dump_json(),))
    service=ReminderService(db,item_timing,adapter)
    service.reconcile(datetime(2030,10,23,8,tzinfo=VIENNA))
    before=db.rows('SELECT * FROM items');archive=create_backup(db,tmp_path)
    service.tick(datetime(2030,10,23,9,40,tzinfo=VIENNA));assert len(sent)==1
    shutil.copy2(archive,tmp_path/'restore-pending.zip')
    restored=create_app(settings);restored.state.reminders.adapter=adapter
    with TestClient(restored):
        assert db.rows('SELECT * FROM items')==before
        assert json.loads(db.one('SELECT value FROM item_preferences')['value'])['minutes']==[60,30]
        assert not restored.state.reminders.settings().sound
        assert db.one("SELECT count(*) n FROM reminder_jobs WHERE state='delivered'")['n']==1
    ReminderService(db,item_timing,adapter).tick(datetime(2030,10,23,9,41,tzinfo=VIENNA))
    assert len(sent)==1  # An older backup cannot replay already dispatched stages on this machine.


def test_idle_worker_observes_new_settings_and_completion_before_delivery(tmp_path):
    db=Database(tmp_path/'studycenter_ice.sqlite3');sent=[]
    service=ReminderService(db,item_timing,SimpleNamespace(send=lambda *args:sent.append(args)))
    value=Candidate(title='Eigene Prüfung',kind='exam',date='2030-10-23',time='10:00',date_meaning='start',obligation='required',quote='Eigene Planung',page=1,anchor='user')
    identifier=db.execute('INSERT INTO items(identity,payload,created_at,changed_at) VALUES(?,?,?,?)',('note:idle',value.model_dump_json(),'2030-10-01','2030-10-01'))
    at=lambda hour,minute=0:datetime(2030,10,23,hour,minute,tzinfo=VIENNA)
    service.tick(at(8))
    assert not sent and db.one("SELECT count(*) n FROM reminder_jobs WHERE state='pending'")['n']==2
    db.execute("INSERT INTO app_preferences VALUES('notifications',?)",(ReminderSettings(enabled=False).model_dump_json(),))
    service.tick(at(9,5))
    assert not sent and not db.one("SELECT count(*) n FROM reminder_jobs WHERE state='pending'")['n']
    db.execute("UPDATE app_preferences SET value=? WHERE name='notifications'",(ReminderSettings().model_dump_json(),))
    db.execute("UPDATE items SET status='done' WHERE id=?",(identifier,))
    service.tick(at(9,6));assert not sent
    db.execute("UPDATE items SET status='open' WHERE id=?",(identifier,))
    service.tick(at(9,7));service.tick(at(9,8))
    assert len(sent)==1 and db.one('SELECT status FROM items WHERE id=?',(identifier,))['status']=='open'
