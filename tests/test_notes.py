import json

import httpx
import pytest

from studycenter_ice.app import create_app
from studycenter_ice.config import Settings


def test_explicit_single_registration_closing_wording_keeps_date_precision():
    from studycenter_ice.app import item_timing
    value={'kind':'registration','date_meaning':'start','date':'2026-10-01','time':'23:59',
           'quote':'Der Schließzeitpunkt ist Donnerstag, 1. Oktober um 23:59 Uhr.'}
    assert item_timing(value)==('2026-10-01','23:59',True)
    assert item_timing(value|{'quote':'You must make this choice before Thursday 23:59.'})==('2026-10-01','23:59',True)
    assert item_timing(value|{'quote':'Die Anmeldung öffnet am 1. Oktober.'})==('2026-10-01','23:59',False)


@pytest.mark.asyncio
async def test_done_note_moves_to_paginated_archive_without_losing_personal_data(tmp_path):
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    headers={'X-CSRF-Token':app.state.csrf,'X-Requested-With':'fetch'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        for n in range(22):
            result=await client.post('/notes',data={'title':f'Archive fixture {n}','request_id':f'{n:032x}'},headers=headers)
            assert result.status_code==200
            if n==0:item_id=int(result.json()['url'].rsplit('/',1)[1])
        db.execute('UPDATE items SET overrides=? WHERE id=?',(json.dumps({'title':'Personal archive correction'}),item_id))
        assert (await client.post(f'/items/{item_id}',data={'action':'status','status':'done'},headers=headers)).status_code==200
        before=db.rows('SELECT * FROM items ORDER BY id')
        board=await client.get('/board')
        archive=await client.get('/archive')
        assert f'data-item-id="{item_id}"' not in board.text
        assert f'data-item-id="{item_id}"' in archive.text and 'Personal archive correction' in archive.text
        assert (await client.get(f'/items/{item_id}')).status_code==200
        information=await client.get('/entries?view=undated')
        assert information.text.count('data-item-id=')==20
        assert (await client.get('/entries?view=undated&page=2')).text.count('data-item-id=')==1
        assert 'Was bedeuten die Tags?' not in information.text
        assert db.rows('SELECT * FROM items ORDER BY id')==before
        assert (await client.post(f'/items/{item_id}',data={'action':'status','status':'open'},headers=headers)).status_code==200
        assert f'data-item-id="{item_id}"' not in (await client.get('/archive')).text
        restored=db.one('SELECT * FROM items WHERE id=?',(item_id,))
        original=next(i for i in before if i['id']==item_id)
        assert {k:v for k,v in restored.items() if k!='status'}=={k:v for k,v in original.items() if k!='status'}


@pytest.mark.asyncio
async def test_manual_postit_course_and_explicit_date_precision_with_invalid_input_rollback(tmp_path):
    app=create_app(Settings(data_dir=tmp_path))
    db=app.state.db
    course=db.execute("INSERT INTO courses(title,semester) VALUES('Fixture course','2026W')")
    headers={'X-CSRF-Token':app.state.csrf,'X-Requested-With':'fetch'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        planned={'title':'Preparation','course_id':str(course),'date':'2026-10-14','date_role':'planned','request_id':'a'*32}
        response=await client.post('/notes',data=planned,headers=headers)
        assert response.status_code==200
        item_id=int(response.json()['url'].rsplit('/',1)[1])
        item=db.one('SELECT * FROM items WHERE id=?',(item_id,))
        value=json.loads(item['payload'])
        assert item['course_id']==course and item['pinned']==1 and item['identity']=='note:manual:'+'a'*32
        assert value['semester']=='2026W' and value['date']=='2026-10-14' and value['time'] is None
        assert value['date_meaning']=='start' and value['obligation']=='optional' and value['quote']=='Eigene Notiz'
        explicit=await client.post('/notes',data=planned|{'request_id':'b'*32,'date_role':'deadline','time':'09:30'},headers=headers)
        assert explicit.status_code==200
        timed=json.loads(db.one('SELECT payload FROM items WHERE identity=?',('note:manual:'+'b'*32,))['payload'])
        assert timed['time']=='09:30:00' and timed['date_meaning']=='deadline'
        from datetime import datetime
        from studycenter_ice.app import action_state
        from studycenter_ice.config import VIENNA
        own=next(i for i in db.items() if i['id']==item_id)
        state=action_state(own,datetime.now(VIENNA))
        assert state['planned_task'] and not state['offered'] and not state['action_deadline']
        exported=await client.get('/calendar/export.ics')
        assert exported.status_code==200 and exported.headers['x-studycenter-ice-events']=='2'
        assert 'DTSTART;VALUE=DATE:20261014' in exported.text
        assert 'DTSTART;TZID=Europe/Vienna:20261014T093000' in exported.text
        assert 'DTEND' not in exported.text
        legacy=await client.post('/notes',data={'title':'Undated'},headers={'X-CSRF-Token':app.state.csrf})
        assert legacy.status_code==303
        undated=json.loads(db.one('SELECT payload FROM items WHERE id=?',(int(legacy.headers['location'].rsplit('/',1)[1]),))['payload'])
        assert undated['date'] is None and undated['time'] is None and undated['date_meaning']=='none'
        before=db.rows('SELECT * FROM items ORDER BY id')
        for invalid in ({'request_id':'bad'}, {'course_id':'-1'}, {'course_id':'99999'}, {'course_id':'9'*25}, {'date_role':'other'},
                        {'date':'','time':'09:00'}, {'time':'09:00+02:00'}):
            assert (await client.post('/notes',data=planned|invalid,headers=headers)).status_code==400
            assert db.rows('SELECT * FROM items ORDER BY id')==before
        assert db.rows('SELECT * FROM item_evidence')==[]


@pytest.mark.asyncio
async def test_manual_postit_replay_preserves_edits_and_rejects_same_id_different_payload(tmp_path):
    app=create_app(Settings(data_dir=tmp_path))
    db=app.state.db
    course=db.execute("INSERT INTO courses(title,semester) VALUES('Other fixture course','2026W')")
    headers={'X-CSRF-Token':app.state.csrf,'X-Requested-With':'fetch'}
    form={'title':'Original note','text':'Original description','request_id':'c'*32}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        first=await client.post('/notes',data=form,headers=headers)
        assert first.status_code==200
        item_id=int(first.json()['url'].rsplit('/',1)[1])
        db.execute("UPDATE items SET status='done',pinned=0,overrides=? WHERE id=?",(json.dumps({'title':'Personal correction'}),item_id))
        before=db.rows('SELECT * FROM items')
        replay=await client.post('/notes',data=form,headers=headers)
        assert replay.status_code==200 and replay.json()==first.json()
        assert db.rows('SELECT * FROM items')==before
        conflict=await client.post('/notes',data=form|{'text':'Changed description'},headers=headers)
        assert conflict.status_code==409 and db.rows('SELECT * FROM items')==before
        different_course=await client.post('/notes',data=form|{'course_id':str(course)},headers=headers)
        assert different_course.status_code==409 and db.rows('SELECT * FROM items')==before
