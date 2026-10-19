"""Post-it controls use isolated data and route browser requests in process."""
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright, expect

from studycenter_ice.app import action_state, create_app
from studycenter_ice.assistant import Assistant, Command
from studycenter_ice.config import Settings
from studycenter_ice.db import now
from studycenter_ice.models import Candidate
from studycenter_ice.search import SearchIndex

ROOT = Path(__file__).resolve().parents[1]


def add_note(db, title='Kontrollnotiz'):
    value = Candidate(title=title, description='Persönliche Planung', kind='task',
                      obligation='required', quote='Eigene Notiz', page=1, anchor='test')
    return db.execute('INSERT INTO items(identity,payload,overrides,pinned,created_at,changed_at) VALUES(?,?,?,?,?,?)',
                      ('note:'+str(uuid4()), value.model_dump_json(), '{"description":"Meine Korrektur"}', 1, now(), now()))


def test_postit_controls_keep_sources_and_archive_completed(tmp_path):
    app = create_app(Settings(data_dir=tmp_path)); db = app.state.db
    identifier = add_note(db)
    imported = add_note(db, 'Importierte Aufgabe')
    db.execute('UPDATE items SET identity=? WHERE id=?', ('fixture:source', imported))
    before = db.one('SELECT * FROM items WHERE id=?', (identifier,))
    with TestClient(app) as client:
        for path in ('/board', '/pins'):
            dom = BeautifulSoup(client.get(path).text, 'html.parser')
            card = dom.select_one(f'[data-item-id="{identifier}"]')
            assert [button['data-status-choice'] for button in card.select('[data-status-choice]')] == ['open', 'doing', 'done']
            assert card.select_one('.postit-work-row .postit-chat')['data-voice-item'] == str(identifier)
            assert len(card.select('.postit-work-row')) == 1
            assert card.select_one('.postit-top-actions > .postit-menu + .pin-form')
            assert card.select_one('.postit-pin')['aria-pressed'] == 'true'
            assert not card.select_one('select[name="status"]')
            assert not card.select_one('.card-action')
            assert dom.select_one('.page-toolbar .note-compose>summary').get_text() == '+ Post-it'
            assert len(dom.select('.personal-chip')) == 2
            assert not dom.select_one('.wall-filter-options [name="personal"]')
            personal = BeautifulSoup(client.get(path+'?personal=false&personal=true').text, 'html.parser')
            assert personal.select_one(f'[data-item-id="{identifier}"]')
            assert not personal.select_one(f'[data-item-id="{imported}"]')
        csrf = dom.select_one('meta[name="csrf-token"]')['content']
        for status in ('doing', 'done'):
            response = client.post(f'/items/{identifier}', data={'csrf':csrf,'action':'status','status':status}, headers={'X-Requested-With':'fetch'})
            assert response.json() == {'ok':True}
        assert not BeautifulSoup(client.get('/board').text, 'html.parser').select_one(f'[data-item-id="{identifier}"]')
        assert BeautifulSoup(client.get('/archive').text, 'html.parser').select_one(f'[data-item-id="{identifier}"]')
        client.post(f'/items/{identifier}', data={'csrf':csrf,'action':'pin'}, headers={'X-Requested-With':'fetch'})
        assert db.one('SELECT pinned FROM items WHERE id=?', (identifier,))['pinned'] == 0
        client.post(f'/items/{identifier}', data={'csrf':csrf,'action':'pin'}, headers={'X-Requested-With':'fetch'})
        assert db.one('SELECT pinned FROM items WHERE id=?', (identifier,))['pinned'] == 1
    after = db.one('SELECT * FROM items WHERE id=?', (identifier,))
    for key in before.keys() - {'status'}:
        assert before[key] == after[key]


def test_pinned_wall_entries_stay_first_without_changing_status(tmp_path):
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    first=add_note(db,'Ungeheftete alte Aufgabe');pinned=add_note(db,'Wichtiges Post-it')
    working=add_note(db,'Bald weiterarbeiten')
    db.execute('UPDATE items SET pinned=0 WHERE id=?',(first,))
    db.execute("UPDATE items SET status='doing' WHERE id=?",(pinned,))
    db.execute("UPDATE items SET status='doing',pinned=0 WHERE id=?",(working,))
    before=db.rows('SELECT * FROM items ORDER BY id')
    client=TestClient(app)
    dom=BeautifulSoup(client.get('/board').text,'html.parser')
    assert dom.select_one('.postit-board [data-item-id]')['data-item-id']==str(pinned)
    assert dom.select_one(f'[data-work-status="pinned"] [data-item-id="{pinned}"]')
    assert dom.select_one(f'[data-work-status="open"] [data-item-id="{first}"]')
    assert [s['data-work-status'] for s in dom.select('.postit-board > .work-section')]==['pinned','doing','open']
    assert [i['data-item-id'] for i in dom.select('.postit-board [data-item-id]')]==[str(pinned),str(working),str(first)]
    detail=BeautifulSoup(client.get(f'/items/{pinned}').text,'html.parser')
    assert detail.select_one('[data-entry-action][data-postit-status]')
    assert detail.select_one(f'[data-voice-item="{pinned}"]')
    assert detail.select_one('#evidence')
    assert db.rows('SELECT * FROM items ORDER BY id')==before


def test_important_wall_includes_unpinned_tasks_and_filters_by_actual_deadline(tmp_path):
    from datetime import datetime, timedelta
    from studycenter_ice.config import VIENNA
    app = create_app(Settings(data_dir=tmp_path)); db = app.state.db
    today = datetime.now(VIENNA).date()
    def add(title, days=None, **changes):
        identifier = add_note(db, title)
        value = json.loads(db.one('SELECT payload FROM items WHERE id=?', (identifier,))['payload'])
        value.update(date=(today+timedelta(days=days)).isoformat() if days is not None else None,
                     date_meaning='deadline' if days is not None else 'none', **changes)
        db.execute('UPDATE items SET identity=?,pinned=0,payload=? WHERE id=?',
                   ('fixture:'+str(identifier), json.dumps(value), identifier))
        return identifier
    tomorrow = add('Abgabe morgen', 1)
    week = add('Aufgabe diese Woche', 6)
    month = add('Aufgabe in drei Wochen', 20)
    window = add('Bearbeitungsfenster mit späterer Abgabe', -2,
                 end_date=(today+timedelta(days=20)).isoformat())
    value = json.loads(db.one('SELECT payload FROM items WHERE id=?', (window,))['payload'])
    value['date_meaning'] = 'window'
    db.execute('UPDATE items SET payload=? WHERE id=?', (json.dumps(value), window))
    later = add('Spätere Aufgabe', 40)
    undated = add('Aufgabe ohne Datum')
    overdue = add('Offene vergangene Abgabe', -2)
    completed = add('Erledigte Abgabe', 20)
    db.execute("UPDATE items SET status='done' WHERE id=?", (completed,))
    lecture = add('Vorlesung', 1, kind='event')
    cancelled = add('Abgesagte Aufgabe', 20, source_state='cancelled')
    db.execute("UPDATE items SET status='doing',overrides='{}' WHERE id=?", (month,))
    before = db.rows('SELECT * FROM items ORDER BY id')
    client = TestClient(app)
    for period, expected in [('all', {tomorrow, week, month, window, later, undated, overdue}),
                             ('1', {tomorrow, overdue}), ('7', {tomorrow, week, overdue}),
                             ('30', {tomorrow, week, month, window, overdue})]:
        response = client.get('/pins?due='+period)
        assert response.status_code == 200
        dom = BeautifulSoup(response.text, 'html.parser')
        actual = {int(node['data-item-id']) for node in dom.select('.magnet-wall [data-item-id]')}
        assert actual == expected
        assert not actual & {completed, lecture, cancelled}
    assert db.rows('SELECT * FROM items ORDER BY id') == before


def test_overview_status_buttons_save_and_archive_without_changing_source_facts(tmp_path):
    from datetime import datetime,timedelta
    from studycenter_ice.config import VIENNA
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    ids=[]
    for kind in ('task','registration'):
        identifier=add_note(db,'Übersicht '+kind)
        value=json.loads(db.one('SELECT payload FROM items WHERE id=?',(identifier,))['payload'])
        value.update(kind=kind,date=(datetime.now(VIENNA).date()+timedelta(days=1)).isoformat(),
                     date_meaning='window' if kind=='registration' else 'deadline')
        if kind=='registration':
            value.update(date=datetime.now(VIENNA).date().isoformat(),end_date=(datetime.now(VIENNA).date()+timedelta(days=1)).isoformat())
        db.execute('UPDATE items SET payload=? WHERE id=?',(json.dumps(value),identifier));ids.append(identifier)
    before=db.rows('SELECT * FROM items ORDER BY id')
    with TestClient(app) as client:
        dom=BeautifulSoup(client.get('/overview').text,'html.parser')
        assert dom.select_one('script[src*="postits.js"]')
        csrf=dom.select_one('meta[name="csrf-token"]')['content']
        for identifier in ids:
            form=dom.select_one(f'[data-item-id="{identifier}"] .overview-status')
            assert form and form.has_attr('data-entry-action')
            assert [b['data-status-choice'] for b in form.select('[data-status-choice]')]==['open','doing','done']
            assert not form.select_one('select')
            for status in ('doing','open','done'):
                response=client.post(f'/items/{identifier}',data={'csrf':csrf,'action':'status','status':status},headers={'X-Requested-With':'fetch'})
                assert response.json()=={'ok':True}
                current=BeautifulSoup(client.get('/overview').text,'html.parser')
                if status!='done':
                    assert current.select_one(f'[data-item-id="{identifier}"] [data-status-choice="{status}"]')['aria-pressed']=='true'
                else:
                    assert not current.select_one(f'[data-item-id="{identifier}"]')
                    assert BeautifulSoup(client.get('/archive').text,'html.parser').select_one(f'[data-item-id="{identifier}"]')
    after=db.rows('SELECT * FROM items ORDER BY id')
    for old,new in zip(before,after):
        assert new['status']=='done'
        assert {k:v for k,v in old.items() if k!='status'}=={k:v for k,v in new.items() if k!='status'}


def test_personal_wall_means_user_created_not_confirmed_calendar(tmp_path, monkeypatch):
    from datetime import datetime
    from studycenter_ice.config import VIENNA
    app = create_app(Settings(data_dir=tmp_path)); db = app.state.db
    note = add_note(db, 'Selbst angelegte Aufgabe')
    imported = add_note(db, 'CON Übung aus persönlichem Kalender')
    db.execute('UPDATE items SET identity=? WHERE id=?', ('tugrazonline:calendar', imported))
    before = db.rows('SELECT * FROM items ORDER BY id')
    rows = db.items()
    own = next(i for i in rows if i['id'] == note)
    own['data'].update(kind='event', title='Selbst angelegte Vorlesungsvorbereitung')
    entry = next(i for i in rows if i['id'] == imported)
    entry['data'].update(kind='event', date='2030-10-20', date_meaning='start')
    entry['personal_event'] = True
    assert action_state(entry, datetime.now(VIENNA))['personal']
    monkeypatch.setattr(db, 'items', lambda **kwargs: rows)
    with TestClient(app) as client:
        for path in ('/board', '/pins'):
            assert BeautifulSoup(client.get(path).text, 'html.parser').select_one(f'[data-item-id="{imported}"]')
            filtered = BeautifulSoup(client.get(path+'?personal=true').text, 'html.parser')
            assert filtered.select_one(f'[data-item-id="{note}"]')
            assert not filtered.select_one(f'[data-item-id="{imported}"]')
    assert db.rows('SELECT * FROM items ORDER BY id') == before


async def test_explicit_item_chat_context_validates_id_and_retains_preview(tmp_path):
    app = create_app(Settings(data_dir=tmp_path)); db = app.state.db
    first = add_note(db, 'Gleicher Titel'); second = add_note(db, 'Gleicher Titel')
    adapter = SimpleNamespace(responses=AsyncMock(side_effect=AssertionError('No model needed for exact status controls')))
    service = Assistant(db, app.state.settings, adapter, SearchIndex(db), action_state)
    before = db.rows('SELECT * FROM items ORDER BY id')
    command = Command(request_id=uuid4(), text='Markiere das als erledigt', context_item_id=first, preview=True)
    result = await service.command(command)
    assert result['preview']['plan']['target_id'] == first and result['focus_item']['id'] == first
    assert not result['changed'] and db.rows('SELECT * FROM items ORDER BY id') == before
    invalid = await service.command(Command(request_id=uuid4(), text='Markiere das als erledigt', context_item_id=999999, preview=True))
    assert invalid['failed'] and 'nicht mehr vorhanden' in invalid['message']
    switched = await service.command(Command(request_id=uuid4(), text='Markiere das als erledigt', previous_id=result['request_id'], context_item_id=second, preview=True))
    assert switched['failed'] and 'neue Unterhaltung' in switched['message']
    fresh = await service.command(Command(request_id=uuid4(), text='Markiere das als erledigt', context_item_id=second, preview=True))
    assert fresh['preview']['plan']['target_id'] == second
    assert db.rows('SELECT * FROM items ORDER BY id') == before


async def test_card_question_supplies_exact_note_and_original_evidence(tmp_path):
    from studycenter_ice.documents import DocumentStore
    from studycenter_ice.db import encode
    app = create_app(Settings(data_dir=tmp_path)); db = app.state.db
    first = add_note(db, 'Vorbereitung'); other = add_note(db, 'Vorbereitung')
    description = 'Lade die Setup-Unterlagen herunter und bringe einen Laptop mit.'
    value = json.loads(db.one('SELECT payload FROM items WHERE id=?', (first,))['payload'])
    value['description'] = description
    db.execute('UPDATE items SET payload=?,overrides=? WHERE id=?', (encode(value), '{}', first))
    db.execute('UPDATE items SET overrides=? WHERE id=?', ('{"description":"Anderer Eintrag"}', other))
    course = db.execute("INSERT INTO courses(title,semester) VALUES('CON','2026W')")
    source = db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Fixture','https://example.test/setup')", (course,))
    store = DocumentStore(db, app.state.settings)
    document = store.save(source, 'https://example.test/setup', 'Setup-Original', 'text/plain', description.encode())
    store.process(document)
    db.execute('UPDATE items SET course_id=? WHERE id=?', (course, first))
    db.execute('INSERT INTO item_evidence(item_id,document_id,page,anchor,quote,payload) VALUES(?,?,?,?,?,?)',
               (first, document, 1, 'test', description, encode(value)))
    index = SearchIndex(db); index.refresh()
    app.state.settings.model_provider = 'chatgpt'
    app.state.settings.oauth_model = 'fixture'
    app.state.settings.oauth_probe_passed = True
    app.state.settings.chatgpt = SimpleNamespace(status=lambda: {'connected': True, 'plan_enabled': True})
    answer = encode({'claims': [{'text': description, 'citations': [{'source_id': f'item:{first}', 'quote': description}]}]})
    adapter = SimpleNamespace(responses=AsyncMock(side_effect=[encode({'action': 'answer_course_question'}), answer]))
    service = Assistant(db, app.state.settings, adapter, index, action_state)
    before = {table: db.rows('SELECT * FROM '+table+' ORDER BY id') for table in ('items','item_evidence','documents','courses','sources')}
    request = Command(request_id=uuid4(), text='Was brauche ich dafür?', context_item_id=first)
    result = await service.command(request)
    assert not result.get('failed') and not result['insufficient'] and not result['changed']
    interpretation = json.loads(adapter.responses.call_args_list[0].args[1])
    payload = json.loads(adapter.responses.call_args_list[1].args[1])
    assert interpretation['previous']['focus_item']['id'] == first
    assert interpretation['previous']['focus_item']['description'] == description
    assert payload['focus_item']['id'] == first and payload['focus_item']['description'] == description
    assert any(s.get('document_id') == document and s['page'] == 1 for s in payload['sources'])
    assert any(s['source_id'] == f'item:{first}' and description in s['text'] for s in payload['sources'])
    # The same-titled second note is never silently treated as the selected card.
    assert all(s['source_id'] != f'item:{other}' for s in payload['sources'])
    assert result['focus_item']['id'] == first
    # A course-less personal note is itself a usable source, even without a PDF
    # or matching topic words. Identical titles cannot change its exact subject.
    from studycenter_ice.assistant import Action
    other_item = next(i for i in db.items() if i['id'] == other)
    sources, _, _ = service.questions.retrieve(Action(action='answer_course_question'),
        Command(request_id=uuid4(), text='Was brauche ich dafür?', context_item_id=other),
        {'focus_item': service.summary(other_item)}, None, '')
    assert [s['source_id'] for s in sources if s['type']=='item'] == [f'item:{other}']
    assert 'Anderer Eintrag' in next(s['text'] for s in sources if s['type']=='item')
    assert all(db.rows('SELECT * FROM '+table+' ORDER BY id') == rows for table, rows in before.items())


async def test_postit_status_click_is_single_request_and_rolls_back_on_error(tmp_path, monkeypatch):
    browsers = ROOT/'.local'/'browsers'
    if browsers.exists(): monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(browsers))
    app = create_app(Settings(data_dir=tmp_path)); first = add_note(app.state.db); second = add_note(app.state.db, 'Andere Notiz')
    before = app.state.db.rows('SELECT * FROM items ORDER BY id')
    with TestClient(app) as client:
        html = client.get('/board').text
    requests = []; voice_requests = []
    async with async_playwright() as driver:
        if not Path(driver.chromium.executable_path).exists(): pytest.skip('Project Chromium not installed')
        browser = await driver.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width':1280,'height':1000})
        async def routes(route):
            path = route.request.url.split('studycenter_ice.test', 1)[1].split('?',1)[0]
            if path == '/board': await route.fulfill(content_type='text/html', body=html)
            elif path.startswith('/static/'):
                asset = ROOT/'studycenter_ice'/'static'/path.split('/')[-1]
                await route.fulfill(content_type='text/javascript' if asset.suffix=='.js' else 'text/css', body=asset.read_text(encoding='utf-8'))
            elif path.startswith('/items/'):
                requests.append(route.request.post_data)
                await route.fulfill(status=400 if len(requests)==1 else 200, json={'detail':'Absichtlicher Fehler'} if len(requests)==1 else {'ok':True})
            elif path == '/api/voice/command':
                voice_requests.append(route.request.post_data_json)
                await route.fulfill(json={'request_id':route.request.post_data_json['request_id'],'message':'Fixture','changed':False})
            elif path == '/api/voice/status':
                await route.fulfill(json={'engine':'whisper','model':'fixture','device':'cpu','connected':False})
            elif path == '/api/status': await route.fulfill(json={'active':False,'login_mode':False,'sources':[]})
            else: await route.fulfill(status=404, body='No external requests')
        await page.route('**/*', routes)
        await page.goto('https://studycenter_ice.test/board')
        captures = ROOT/'.local'/'ui-feedback-next-20261014'
        captures.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(captures/'D-postit-implemented-desktop.png'), full_page=True)
        await page.evaluate('document.documentElement.dataset.theme="dark"')
        await page.screenshot(path=str(captures/'D-postit-implemented-dark.png'), full_page=True)
        await page.evaluate('document.documentElement.dataset.theme="light"')
        card = page.locator(f'[data-item-id="{first}"]')
        await card.locator('[data-status-choice="doing"]').evaluate('(button)=>{button.click();button.click();}')
        await expect(page.locator('#notice')).to_contain_text('Absichtlicher Fehler')
        await expect(card.locator('[data-status-choice="open"]')).to_have_attribute('aria-pressed','true')
        await expect(card.locator('input[name="status"]')).to_have_value('open')
        await card.locator('[data-status-choice="doing"]').click()
        await expect(card.locator('[data-status-choice="doing"]')).to_have_attribute('aria-pressed','true')
        # A pinned card keeps its top position when its personal status changes.
        assert await card.evaluate('(e)=>e.closest("[data-work-status]").dataset.workStatus') == 'pinned'
        await expect(card.locator('input[name="status"]')).to_have_value('doing')
        assert len(requests)==2 and requests[1].count('name="status"')==1 and '\r\ndoing\r\n' in requests[1]
        await card.locator('[data-status-choice="doing"]').click()
        assert len(requests)==2
        await card.locator('.postit-chat').click()
        await expect(page.locator('#voice-panel')).to_be_visible()
        assert not voice_requests
        await page.locator('#voice-transcript').fill('Besprechen')
        await page.locator('#voice-send').click()
        await expect(page.locator('#voice-process')).to_have_text('Bereit.')
        assert voice_requests[0]['context_item_id']==first and voice_requests[0]['previous_id'] is None
        await page.locator('#voice-close').click()
        await page.locator(f'[data-item-id="{second}"] .postit-chat').click()
        await page.locator('#voice-transcript').fill('Anderer Bezug')
        await page.locator('#voice-send').click()
        await expect(page.locator('#voice-process')).to_have_text('Bereit.')
        assert voice_requests[1]['context_item_id']==second and voice_requests[1]['previous_id'] is None
        await page.locator('#voice-new-chat').click()
        await page.locator('#voice-transcript').fill('Ohne Eintrag')
        await page.locator('#voice-send').click()
        await expect(page.locator('#voice-process')).to_have_text('Bereit.')
        assert 'context_item_id' not in voice_requests[2]
        await page.locator('#voice-close').click()
        row = card.locator('.postit-work-row')
        rectangles = await row.locator('[data-status-choice],.postit-chat').evaluate_all('(nodes)=>nodes.map(e=>({y:e.getBoundingClientRect().y,h:e.getBoundingClientRect().height}))')
        assert len(rectangles)==4 and len({round(r['y']) for r in rectangles})==1 and len({round(r['h']) for r in rectangles})==1
        await page.evaluate('document.body.classList.add("compact")')
        await page.set_viewport_size({'width':390,'height':844})
        await page.screenshot(path=str(captures/'D-postit-implemented-mobile-compact.png'), full_page=True)
        assert await page.locator('.postit-grid').first.evaluate('(e)=>getComputedStyle(e).display') == 'grid'
        rectangles = await row.locator('[data-status-choice],.postit-chat').evaluate_all('(nodes)=>nodes.map(e=>Math.round(e.getBoundingClientRect().y))')
        assert len(set(rectangles))==1
        await card.locator('[data-status-choice="done"]').click()
        await expect(card).to_have_count(0)
        assert len(requests)==3
        await browser.close()
    assert app.state.db.rows('SELECT * FROM items ORDER BY id') == before
