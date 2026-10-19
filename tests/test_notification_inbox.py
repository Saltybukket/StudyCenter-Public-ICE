from datetime import datetime,timedelta
import shutil
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studycenter_ice.app import create_app,item_timing
from studycenter_ice.backup import create_backup,inspect_archive
from studycenter_ice.config import Settings,VIENNA
from studycenter_ice.db import Database
from studycenter_ice.models import Candidate
from studycenter_ice.reminders import ReminderService,utc


ROOT = Path(__file__).resolve().parents[1]


async def test_read_after_app_restart_refreshes_token_without_changing_tasks(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright, expect
    browsers = ROOT/'.local'/'browsers'
    if browsers.exists(): monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(browsers))
    app = create_app(Settings(data_dir=tmp_path))
    iid = item(app.state.db, 'Synthetisches Quiz 2')
    other = item(app.state.db, 'Quiz Einheit 2 bearbeiten')
    current = datetime.now(VIENNA)
    job(app.state.db, iid, 'quiz-reminder', current)
    job(app.state.db, other, 'planned-reminder', current)
    old_client = TestClient(app, base_url='http://127.0.0.1')
    old_html = old_client.get('/board').text
    # The page stays open while the local process is restarted.
    restarted = create_app(Settings(data_dir=tmp_path))
    client = TestClient(restarted, base_url='http://127.0.0.1')
    before = {table:restarted.state.db.rows(f'SELECT * FROM {table} ORDER BY 1')
              for table in ('items', 'item_evidence', 'reminder_jobs')}
    posts = []; refreshes = []; fail_next_read = [False]
    async with async_playwright() as driver:
        executable = __import__('os').environ.get('STUDYCENTER_ICE_TEST_CHROMIUM', driver.chromium.executable_path)
        if not Path(executable).exists(): pytest.skip('Project Chromium not installed')
        browser = await driver.chromium.launch(headless=True, executable_path=executable, chromium_sandbox=True)
        page = await browser.new_page()
        async def route_request(route):
            request = route.request
            path = request.url.split('127.0.0.1', 1)[1]
            if path == '/board':
                await route.fulfill(content_type='text/html', body=old_html)
            elif path.startswith('/static/'):
                asset = ROOT/'studycenter_ice'/'static'/path.split('?',1)[0].split('/')[-1]
                await route.fulfill(content_type='text/javascript' if asset.suffix=='.js' else 'text/css', body=asset.read_bytes())
            else:
                if path == '/api/notifications/read' and fail_next_read[0]:
                    fail_next_read[0] = False; posts.append(503)
                    await route.fulfill(status=503, json={'detail':'Speichern vorübergehend nicht möglich'})
                    return
                response = client.request(request.method, path, content=request.post_data_buffer,
                                          headers=request.headers, follow_redirects=False)
                if path == '/api/notifications/read': posts.append(response.status_code)
                if path == '/api/session': refreshes.append(response.status_code)
                await route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        await page.route('**/*', route_request)
        await page.goto('http://127.0.0.1/board')
        await page.locator('#notifications-open').click()
        await page.get_by_role('button', name='Synthetisches Quiz 2 als gelesen markieren', exact=True).click()
        try:
            await expect(page.locator('#notifications-list .notification-entry')).to_have_count(1)
        except AssertionError:
            pytest.fail(f'POST statuses={posts}, refresh statuses={refreshes}, error={await page.locator("#notifications-error").inner_text()}')
        await expect(page.locator('#notifications-list')).to_contain_text('Quiz Einheit 2 bearbeiten')
        await expect(page.locator('#notifications-badge')).to_have_text('1')
        assert posts == [403, 200] and refreshes == [200]
        fail_next_read[0] = True
        await page.locator('#notifications-read').click()
        await expect(page.locator('#notifications-error')).to_contain_text('Speichern nicht bestätigt')
        await expect(page.locator('#notifications-list .notification-entry')).to_have_count(1)
        await expect(page.locator('#notifications-read')).to_be_enabled()
        assert posts == [403, 200, 503] and refreshes == [200]
        await page.locator('#notifications-read').click()
        await expect(page.locator('#notifications-list .notification-entry')).to_have_count(0)
        await page.get_by_role('button', name='Verlauf', exact=True).click()
        await expect(page.locator('#notifications-list .notification-entry')).to_have_count(2)
        assert posts == [403, 200, 503, 200] and refreshes == [200]
        await browser.close()
    assert client.get('/api/notifications?unread=true').json()['unread'] == 0
    assert {table:restarted.state.db.rows(f'SELECT * FROM {table} ORDER BY 1') for table in before} == before
    fresh = create_app(Settings(data_dir=tmp_path))
    assert TestClient(fresh).get('/api/notifications?unread=true').json()['unread'] == 0


def test_session_refresh_does_not_bypass_origin_or_form_protection(tmp_path):
    app = create_app(Settings(data_dir=tmp_path)); client = TestClient(app)
    assert client.get('/api/session').status_code == 403
    headers = {'X-Requested-With':'fetch'}
    response = client.get('/api/session', headers=headers)
    assert response.status_code == 200 and response.json()['csrf'] == app.state.csrf
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/api/session', headers=headers|{'Origin':'https://foreign.example'}).status_code == 403
    iid=item(app.state.db);job(app.state.db,iid,'protected',datetime.now(VIENNA))
    rejected=client.post('/api/notifications/read',json={'ids':['protected']},headers={'X-CSRF-Token':'old'})
    assert rejected.status_code == 403 and rejected.json()['code'] == 'csrf_expired'
    opaque=client.post('/api/notifications/read',json={'ids':['protected']},headers={'X-CSRF-Token':'old','Origin':'null'})
    assert opaque.status_code == 403 and opaque.json()['code'] == 'csrf_expired'
    assert client.get('/api/notifications?unread=true').json()['unread'] == 1
    assert client.post('/api/notifications/read',json={'ids':['protected']},headers={'X-CSRF-Token':app.state.csrf,'Origin':'https://foreign.example'}).status_code == 403
    assert client.post('/api/notifications/read',json={'ids':['protected']},headers={'Origin':'null'}).status_code == 403


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
