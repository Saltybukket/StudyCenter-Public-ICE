import json
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from studycenter_ice.app import action_state,item_timing
from studycenter_ice.assistant import Assistant,Command
from studycenter_ice.config import Settings,VIENNA
from studycenter_ice.db import Database,encode,now
from studycenter_ice.documents import DocumentStore
from studycenter_ice.models import Candidate
from studycenter_ice.reminders import ReminderService
from studycenter_ice.search import SearchIndex
from fastapi.testclient import TestClient
from studycenter_ice.app import create_app


@pytest.fixture
def env(tmp_path):
    settings=Settings(data_dir=tmp_path,model_provider='chatgpt',oauth_model='gpt-6-luna',oauth_probe_passed=True)
    settings.chatgpt=SimpleNamespace(status=lambda:{'connected':True,'plan_enabled':True})
    db=Database(tmp_path/'studycenter_ice.sqlite3')
    con=db.execute("INSERT INTO courses(title,semester) VALUES('Computer Organization and Networks','2026W')")
    other=db.execute("INSERT INTO courses(title,semester) VALUES('Elektronische Schaltungstechnik 2','2026W')")
    adapter=SimpleNamespace(responses=AsyncMock())
    service=Assistant(db,settings,adapter,SearchIndex(db),action_state)
    return service,db,adapter,con,other


def own(db,title,course,date_role='start',kind='task'):
    day=(datetime.now(VIENNA)+timedelta(days=1)).date()
    data=Candidate(title=title,kind=kind,date=day,date_meaning=date_role,quote='Eigene Planung',page=1,anchor='user')
    return db.execute('INSERT INTO items(identity,course_id,payload,created_at,changed_at) VALUES(?,?,?,?,?)',('note:'+title,course,data.model_dump_json(),now(),now()))


async def submit(service,adapter,text,plan,*,previous=None,context=None):
    adapter.responses.return_value=encode(plan)
    req=Command(request_id=uuid4(),text=text,preview=True,previous_id=previous,context_item_id=context)
    return req,await service.command(req)


async def apply(service,req):
    command=Command(request_id=uuid4(),text='Übernehmen',previous_id=req.request_id,preview=True,apply_preview=True)
    result=await service.command(command)
    assert await service.command(command)==result
    return command,result


@pytest.mark.asyncio
async def test_completed_creation_status_followup_priority_and_real_clarification(env):
    service,db,adapter,con,_=env
    req,proposal=await submit(service,adapter,'Leg mir eine Aufgabe für morgen an, als wichtig. Keine Benachrichtigung.',
        {'action':'create_task','title':'Nachholen','date':(datetime.now(VIENNA)+timedelta(days=1)).date().isoformat(),'task_date_role':'planned'})
    assert proposal['preview']['plan']['pinned'] is True
    saved,result=await apply(service,req)
    req,proposal=await submit(service,adapter,'Damit habe ich jetzt angefangen.',{'action':'clarify','planned_action':'create_task','title':'Nachholen'},previous=saved.request_id)
    assert proposal['preview']['plan']['action']=='set_status'
    _,changed=await apply(service,req)
    assert changed['item_id']==result['item_id'] and len(db.rows('SELECT * FROM items'))==1
    assert db.one('SELECT status,pinned FROM items')=={'status':'doing','pinned':1}
    _,question=await submit(service,adapter,'Schreib zwei getrennte Aufgaben auf.',{'action':'clarify','planned_action':'create_task','title':'Erste','question':'Welche soll ich zuerst anlegen?'})
    assert question.get('clarify') and not question.get('preview')


@pytest.mark.asyncio
async def test_course_kind_search_and_deadlines_exclude_planning(env):
    service,db,adapter,con,other=env
    task=own(db,'CON Folien ansehen',con)
    own(db,'CON Vorlesung',con,kind='event');own(db,'Converters Vorlesung',other,kind='event')
    _,result=await submit(service,adapter,'Zeig mir meine offenen CON-Aufgaben.',{'action':'search_items','query':'CON','kind':'task','status':'open'})
    assert [r['id'] for r in result['items']]==[task]
    for n in range(7):own(db,'Zusätzliche Aufgabe '+str(n),con)
    req,result=await submit(service,adapter,'Zeig meine offenen CON-Aufgaben.',{'action':'search_items','kind':'task','course':'CON','status':'open'})
    assert result['has_more']
    _,tail=await submit(service,adapter,'Weitere Treffer.',{},previous=req.request_id)
    assert len(tail['items'])==2 and not tail.get('failed') and not tail['has_more']
    day=(datetime.now(VIENNA)+timedelta(days=1)).date()
    _,result=await submit(service,adapter,'Was muss ich morgen abgeben?',{'action':'list_upcoming','kind':'task','date_from':day.isoformat(),'date_to':day.isoformat()})
    assert result['items']==[]
    vo=db.execute("INSERT INTO courses(title,semester) VALUES('Messtechnik (VO)','2026W')")
    ue=db.execute("INSERT INTO courses(title,semester) VALUES('Messtechnik, Übung (WS)','2026W')")
    req,result=await submit(service,adapter,'Notier mir für Messtechnik die Formelsammlung durchsehen.',{'action':'create_task','title':'Formelsammlung','course':'Messtechnik'})
    assert len(result['courses'])==2
    req,result=await submit(service,adapter,'Die Übung, nicht die Vorlesung.',{'new_request':False,'cancel':False,'updates':{'course_id':ue}},previous=req.request_id)
    assert result['preview']['plan']['course_id']==ue


@pytest.mark.asyncio
async def test_explicit_duration_end_date_and_preserved_duration_on_shift(env):
    service,db,adapter,con,_=env
    req,_=await submit(service,adapter,'Leg mir morgen einen Lerntermin an.',{'action':'create_event','title':'Lerntermin','date':(datetime.now(VIENNA)+timedelta(days=1)).date().isoformat()})
    req,preview=await submit(service,adapter,'Um halb drei nachmittags, eine Stunde lang.',{'new_request':False,'cancel':False,'updates':{'time':'14:30:00','end_time':'15:30:00'}},previous=req.request_id)
    assert preview.get('preview') and preview['preview']['plan']['end_date']==preview['preview']['date']
    saved,created=await apply(service,req)
    day=(datetime.now(VIENNA)+timedelta(days=2)).date().isoformat()
    req,preview=await submit(service,adapter,'Verschieb das auf übermorgen um 16 Uhr.',{'action':'update_personal_item','target_id':created['item_id'],'patch':{'date':day,'time':'16:00:00'}},previous=saved.request_id)
    assert preview['preview']['plan']['patch']['end_time']=='17:00:00'


@pytest.mark.asyncio
async def test_document_search_only_real_documents_and_open_page(env):
    service,db,adapter,con,_=env
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','CON','https://example.org')",(con,))
    store=DocumentStore(db,service.settings)
    document=store.save(sid,'https://example.org/A1','Aufgabe 1','text/plain',b'Angabe Aufgabe 1 FSM SystemVerilog')
    store.process(document)
    req,result=await submit(service,adapter,'Zeig mir die CON-Angabe zu Aufgabe 1 als PDF.',{'action':'search_documents','query':'Aufgabe 1','course':'CON','content_type':'pdf'})
    assert result['documents'][0]['document_id']==document and 'PDF' in result['message']
    _,opened=await submit(service,adapter,'Öffne das erste Dokument.',{'action':'open_document','document_id':document,'page':1},previous=req.request_id)
    assert opened['navigate']==f'/documents/{document}?page=1'


@pytest.mark.asyncio
@pytest.mark.parametrize('direct',[False,True])
async def test_quiz_reminder_uses_existing_rule_preserves_sources_and_undo(env,direct):
    service,db,adapter,con,_=env
    if direct:db.execute("INSERT INTO app_preferences VALUES('assistant_mode',?)",(encode({'mode':'direct'}),))
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Quiz','https://example.org')",(con,))
    doc=db.execute("INSERT INTO documents(source_id,url,title,mime,hash,fetched_at,original_path) VALUES(?,'https://example.org','Quiz','text/html','hash',?,'unused')",(sid,now()))
    value=Candidate(title='Quiz 1',kind='exam',identity_key='quiz1',identity_scope='course',date='2030-10-13',time='18:00',end_date='2030-10-19',end_time='12:00',date_meaning='window',obligation='optional',quote='Quiz available until 19 October at 12:00',page=1,anchor='quiz')
    identifier=db.ingest(db.one('SELECT * FROM documents WHERE id=?',(doc,)),value)
    before=db.rows('SELECT * FROM items');proofs=db.rows('SELECT * FROM item_evidence')
    db.execute('INSERT INTO item_preferences VALUES(?,?)',(identifier,encode({'source_decisions':{'kept':True},'attendance':''})))
    req,preview=await submit(service,adapter,'Erinnere mich eine Stunde vor Abgabeschluss an dieses Quiz.',{'action':'set_reminder','target_id':identifier,'notification_at':'2030-10-19T11:00:00+02:00'},context=identifier)
    if direct:
        assert preview.get('direct') and preview['undoable'] and not preview.get('preview')
        saved,result=req,preview
    else:
        assert preview.get('preview') and preview['preview']['plan']['action']=='set_item_reminder'
        saved,result=await apply(service,req)
    assert result['undoable'] and db.rows('SELECT * FROM items')==before and db.rows('SELECT * FROM item_evidence')==proofs
    profile=json.loads(db.one('SELECT value FROM item_preferences')['value'])
    assert profile['source_decisions']=={'kept':True} and profile['minutes']==[60] and profile['at_closing']
    item=next(r for r in db.items() if r['id']==identifier)
    jobs,_=ReminderService(db,item_timing).plans(item,datetime(2030,10,14,tzinfo=VIENNA))
    assert datetime.fromisoformat(jobs[0]['due_at']).astimezone(VIENNA).hour==11
    await service.undo(saved.request_id,True)
    assert json.loads(db.one('SELECT value FROM item_preferences')['value'])=={'source_decisions':{'kept':True},'attendance':''}


@pytest.mark.asyncio
async def test_multiple_tasks_atomic_preview_replay_undo_and_cancel(env):
    service,db,adapter,con,_=env
    req,proposal=await submit(service,adapter,'Schreib zwei getrennte Aufgaben: CON-Folien ansehen und Tasche packen.',
        {'action':'create_tasks','tasks':[{'title':'CON-Folien ansehen','course':'CON'},{'title':'Tasche packen','pinned':True}]})
    assert len(proposal['preview']['tasks'])==2 and not db.rows('SELECT * FROM items')
    saved,created=await apply(service,req)
    assert len(created['created_ids'])==2 and len(db.rows('SELECT * FROM items'))==2
    assert db.rows('SELECT course_id,pinned FROM items ORDER BY id')==[{'course_id':con,'pinned':0},{'course_id':None,'pinned':1}]
    req,selected=await submit(service,adapter,'Markiere die erste Aufgabe als erledigt.',{'action':'set_status','target_id':created['created_ids'][0],'status':'done'},previous=saved.request_id)
    assert selected['preview']['plan']['target_id']==created['created_ids'][0]
    cancelled=await service.command(Command(request_id=uuid4(),text='Abbrechen',previous_id=req.request_id,cancel=True,preview=True))
    assert cancelled['cancelled']
    undone=await service.undo(saved.request_id,True)
    assert undone['undone'] and all(r['ignored'] for r in db.rows('SELECT * FROM items'))
    assert await service.undo(saved.request_id,True)==undone
    req,proposal=await submit(service,adapter,'Zwei weitere Aufgaben anlegen.',{'action':'create_tasks','tasks':[{'title':'Eine'},{'title':'Andere'}]})
    cancelled=await service.command(Command(request_id=uuid4(),text='Abbrechen',previous_id=req.request_id,cancel=True,preview=True))
    assert cancelled['cancelled'] and len(db.rows('SELECT * FROM items'))==2
    _,bad=await submit(service,adapter,'Zwei Aufgaben anlegen.',{'action':'create_tasks','tasks':[{'title':'Gültig'},{'title':'Falscher Kurs','course':'Unbekannter Kurs'}]})
    assert bad.get('clarify') and len(db.rows('SELECT * FROM items'))==2
    _,unsupported=await submit(service,adapter,'Zwei Aufgaben mit Erinnerung anlegen.',{'action':'create_tasks','tasks':[{'title':'Eine'},{'title':'Andere'}],'reminder_requested':True})
    assert unsupported.get('clarify') and len(db.rows('SELECT * FROM items'))==2


@pytest.mark.asyncio
@pytest.mark.parametrize('action,noun',[('create_task','Aufgabe'),('create_note','Notiz'),('create_event','Termin')])
async def test_direct_single_creation_retry_followup_status_pin_reminder_and_undo(env,action,noun):
    service,db,adapter,con,_=env
    assert service.mode()=='preview'
    db.execute("INSERT INTO app_preferences VALUES('assistant_mode',?)",(encode({'mode':'direct'}),))
    day=(datetime.now(VIENNA)+timedelta(days=1)).date().isoformat()
    plan={'action':action,'title':'Eigene Schlussprobe','course':'CON','date':day}
    if action=='create_event':plan.update(time='14:30',end_time='15:30')
    text=f'Erstelle eine persönliche {noun} für CON morgen'+(' um 14:30 für eine Stunde.' if action=='create_event' else '.')
    req,created=await submit(service,adapter,text,plan)
    assert created.get('direct') and created['created'] and not created.get('preview')
    assert created['items'][0]['url']==f"/items/{created['item_id']}" and created['undo_request_id']==str(req.request_id)
    assert adapter.responses.await_count==1
    assert await service.command(req)==created
    _,duplicate=await submit(service,adapter,text,plan,previous=req.request_id)
    assert duplicate==created and adapter.responses.await_count==1 and len(db.rows('SELECT * FROM items'))==1
    status_req,status=await submit(service,adapter,'Damit habe ich jetzt angefangen.',{'action':'clarify'},previous=req.request_id)
    assert status.get('direct') and status['undoable'] and status['item_id']==created['item_id']
    assert db.one('SELECT status FROM items')['status']=='doing' and adapter.responses.await_count==1
    repeat=Command(request_id=uuid4(),text=status_req.text,previous_id=req.request_id,preview=True)
    assert await service.command(repeat)==status
    stale=await service.command(Command(request_id=uuid4(),text='Damit bin ich fertig.',previous_id=req.request_id,preview=True))
    assert stale.get('failed') and db.one('SELECT status FROM items')['status']=='doing'
    reverted=await service.undo(status_req.request_id,True)
    assert reverted['undone'] and db.one('SELECT status FROM items')['status']=='open'
    pinned=action!='create_note'
    pin_req,pin=await submit(service,adapter,'Hefte diesen Eintrag an.' if pinned else 'Löse diesen Eintrag.',{'action':'set_pin','target_id':created['item_id'],'pinned':pinned},previous=status_req.request_id)
    assert pin.get('direct') and pin['undoable'] and db.one('SELECT pinned FROM items')['pinned']==int(pinned)
    clock=(datetime.now(VIENNA)+timedelta(days=1)).isoformat()
    rem_req,rem=await submit(service,adapter,'Erinnere mich morgen um diese Uhrzeit an diesen Eintrag.',{'action':'set_reminder','target_id':created['item_id'],'notification_at':clock},previous=pin_req.request_id)
    assert rem.get('direct') and rem['undoable'] and db.one('SELECT item_id FROM personal_reminders')['item_id']==created['item_id']
    await service.undo(rem_req.request_id,True)
    assert not db.rows('SELECT * FROM personal_reminders')
    await service.undo(req.request_id,True)
    assert db.one('SELECT ignored FROM items')['ignored']==1 and len(db.rows('SELECT * FROM items'))==1
    # Stored receipts remain stable even when the user switches mode after sending.
    db.execute("UPDATE app_preferences SET value=? WHERE name='assistant_mode'",(encode({'mode':'preview'}),))
    assert (await service.command(req))['undone']


@pytest.mark.asyncio
async def test_direct_keeps_ambiguity_conflicts_risky_batches_and_edits_in_preview(env):
    service,db,adapter,con,_=env
    db.execute("INSERT INTO app_preferences VALUES('assistant_mode',?)",(encode({'mode':'direct'}),))
    for title in ('Messtechnik (VO)','Messtechnik (UE)'):db.execute('INSERT INTO courses(title,semester) VALUES(?,?)',(title,'2026W'))
    _,ambiguous=await submit(service,adapter,'Erstelle eine Aufgabe für Messtechnik.',{'action':'create_task','title':'Lernen','course':'Messtechnik'})
    assert ambiguous.get('clarify') and not db.rows('SELECT * FROM items')
    for text,plan in [
        ('Erstelle zwei persönliche Aufgaben.',{'action':'create_task','title':'Nur die erste'}),
        ('Erstelle eine Aufgabe morgen oder am Freitag.',{'action':'create_task','title':'Unklar'}),
        ('Erstelle eine Aufgabe für morgen.',{'action':'create_task','title':'Falsches Datum','date':(datetime.now(VIENNA)+timedelta(days=3)).date().isoformat()}),
        ('Erstelle eine Aufgabe und melde mich bei der TU anmelden.',{'action':'create_task','title':'Anmeldung'}),
        ('Erstelle eine Aufgabe für unbekannten Kurs.',{'action':'create_task','title':'Kurs fehlt','course':'Unbekannter Kurs'}),
        ('Erstelle zwei getrennte Aufgaben.',{'action':'create_tasks','tasks':[{'title':'Erste'},{'title':'Zweite'}]})]:
        _,result=await submit(service,adapter,text,plan)
        assert result.get('preview') and not result.get('changed') and not db.rows('SELECT * FROM items')
    req,preview=await submit(service,adapter,'Erstelle eine Aufgabe morgen oder am Freitag.',{'action':'create_task','title':'Unklar'})
    _,still_preview=await submit(service,adapter,'Okay',{},previous=req.request_id)
    assert still_preview.get('preview') and not db.rows('SELECT * FROM items')
    item=own(db,'Unklare Quelle',con)
    value=json.loads(db.one('SELECT payload FROM items WHERE id=?',(item,))['payload']);value['source_state']='unclear'
    db.execute('UPDATE items SET payload=? WHERE id=?',(encode(value),item));before=db.rows('SELECT * FROM items')
    _,result=await submit(service,adapter,'Markiere diesen Eintrag erledigt.',{'action':'set_status','target_id':item,'status':'done'},context=item)
    assert result.get('preview') and db.rows('SELECT * FROM items')==before
    _,invalid=await submit(service,adapter,'Erstelle morgen einen Termin um 15 Uhr bis 14 Uhr.',{'action':'create_event','title':'Falscher Zeitraum','date':'2030-10-01','time':'15:00','end_date':'2030-10-01','end_time':'14:00'})
    assert invalid.get('failed') and db.rows('SELECT * FROM items')==before


@pytest.mark.asyncio
async def test_direct_multiturn_resolution_and_unchanged_value(env):
    service,db,adapter,con,_=env
    db.execute("INSERT INTO app_preferences VALUES('assistant_mode',?)",(encode({'mode':'direct'}),))
    ids=[db.execute('INSERT INTO courses(title,semester) VALUES(?,?)',(title,'2026W')) for title in ('Messtechnik (VO)','Messtechnik (UE)')]
    req,question=await submit(service,adapter,'Erstelle eine Aufgabe für Messtechnik: Vorbereitung.',{'action':'create_task','title':'Vorbereitung','course':'Messtechnik'})
    assert question.get('clarify') and not db.rows('SELECT * FROM items')
    reply=Command(request_id=uuid4(),text='Die Übung',option_id=f'course:{ids[1]}',previous_id=req.request_id,preview=True)
    created=await service.command(reply)
    assert created.get('direct') and created['created'] and db.one('SELECT course_id FROM items')['course_id']==ids[1]
    assert adapter.responses.await_count==1 and await service.command(reply)==created
    _,noop=await submit(service,adapter,'Setze diesen Eintrag auf offen.',{'action':'set_status','target_id':created['item_id'],'status':'open'},previous=reply.request_id)
    assert not noop['changed'] and not noop.get('undoable') and 'Keine Änderung nötig' in noop['message']
    assert len(db.rows('SELECT * FROM items'))==1


def test_mode_setting_defaults_preview_validates_and_preserves_other_data(tmp_path,monkeypatch):
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    monkeypatch.setattr(app.state.sync.auth,'status',lambda:{'connected':False,'plan_enabled':False,'profiles':[],'models':[]})
    client=TestClient(app);before=db.rows('SELECT * FROM items')
    assert app.state.assistant.mode()=='preview'
    assert 'Luna-Ausführungsmodus' in client.get('/model').text
    for mode in ('direct','preview'):
        result=client.post('/model/assistant-mode',data={'csrf':app.state.csrf,'mode':mode})
        assert result.status_code==200 and result.json()['mode']==mode and app.state.assistant.mode()==mode
    assert client.post('/model/assistant-mode',data={'csrf':app.state.csrf,'mode':'invalid'}).status_code==400
    assert client.post('/model/assistant-mode',data={'mode':'direct'},headers={'Origin':'https://example.org'}).status_code==403
    assert app.state.assistant.mode()=='preview' and db.rows('SELECT * FROM items')==before
