from datetime import datetime,timedelta
import shutil
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from studycenter_ice.app import create_app,item_timing
from studycenter_ice.backup import create_backup,inspect_archive
from studycenter_ice.config import Settings,VIENNA
from studycenter_ice.db import Database
from studycenter_ice.models import Candidate
from studycenter_ice.reminders import ReminderService,utc


def item(db,title='Erinnerung'):
    data=Candidate(title=title,kind='task',quote='Eigene Planung',page=1,anchor='user')
    return db.execute('INSERT INTO items(identity,payload,created_at,changed_at) VALUES(?,?,?,?)',
                      ('note:'+title,data.model_dump_json(),'2030-01-01','2030-01-01'))


def job(db,iid,key,current,*,state='delivered',rule='task:120',target=None):
    db.execute('INSERT INTO reminder_jobs(id,item_id,target_at,due_at,rule,state,updated_at) VALUES(?,?,?,?,?,?,?)',
               (key,iid,utc(target or current+timedelta(hours=2)),utc(current),rule,state,utc(current)))


def test_grouped_bounded_inbox_reads_preserve_delivery_and_new_stage(tmp_path):
    db=Database(tmp_path/'db.sqlite3');service=ReminderService(db,item_timing,SimpleNamespace())
    current=datetime(2030,10,14,10,tzinfo=VIENNA);iid=item(db)
    job(db,iid,'one',current);job(db,iid,'two',current,rule='task:1440')
    job(db,iid,'quiet',current,state='quiet');job(db,iid,'old',current-timedelta(days=91))
    data=service.inbox(unread=True,current=current)
    assert data['unread']==1 and len(data['entries'])==1 and data['entries'][0]['deliveries']==2
    before=db.rows('SELECT * FROM reminder_jobs ORDER BY id');original=db.rows('SELECT * FROM items')
    service.read_notifications(data['entries'][0]['ids']);service.read_notifications(data['entries'][0]['ids'])
    assert service.inbox(unread=True,current=current)['entries']==[]
    assert len(service.inbox(current=current)['entries'])==1
    assert db.rows('SELECT * FROM reminder_jobs ORDER BY id')==before and db.rows('SELECT * FROM items')==original
    job(db,iid,'new-stage',current,state='error',rule='voice:exact',target=current+timedelta(hours=2))
    assert service.inbox(current=current)['unread']==1 and service.inbox(current=current)['entries'][0]['errors']==1
    with pytest.raises(ValueError):service.read_notifications('one')
    for n in range(21):job(db,item(db,'Extra'+str(n)),'extra'+str(n),current)
    first=service.inbox(current=current);second=service.inbox(page=2,current=current)
    assert len(first['entries'])==20 and first['has_more'] and len(second['entries'])==2
    assert {r['item_id'] for r in first['entries']}.isdisjoint(r['item_id'] for r in second['entries'])


def test_inbox_api_and_read_marker_survives_database_restart(tmp_path):
    app=create_app(Settings(data_dir=tmp_path));iid=item(app.state.db)
    job(app.state.db,iid,'notification',datetime.now(VIENNA))
    with TestClient(app) as client:
        result=client.get('/api/notifications?unread=true').json()
        assert result['unread']==1 and result['entries'][0]['item_id']==iid
        response=client.post('/api/notifications/read',json={'ids':['notification']},headers={'X-CSRF-Token':app.state.csrf})
        assert response.status_code==200
        assert client.get('/api/notifications?unread=true').json()['unread']==0
        assert client.get('/api/notifications?page=0').status_code==422
        assert client.post('/api/notifications/read',json={'ids':['notification']},headers={'Origin':'https://foreign.example','X-CSRF-Token':app.state.csrf}).status_code==403
    restarted=Database(tmp_path/'studycenter_ice.sqlite3')
    assert restarted.one('SELECT job_id FROM notification_reads')['job_id']=='notification'
    archive=create_backup(restarted,tmp_path)
    result=inspect_archive(archive,tmp_path/'checked-backup')
    assert result['counts']['notification_reads']==1
    shutil.copyfile(archive,tmp_path/'restore-pending.zip')
    restored_app=create_app(Settings(data_dir=tmp_path))
    with TestClient(restored_app) as client:
        assert client.get('/api/notifications?unread=true').json()['unread']==0
        assert restored_app.state.db.one("SELECT state FROM reminder_jobs WHERE id='notification'")['state']=='delivered'
