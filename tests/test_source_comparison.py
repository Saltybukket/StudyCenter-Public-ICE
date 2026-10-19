import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from studycenter_ice.app import create_app, action_state, calendar_events
from studycenter_ice.comparison import comparison, save
from studycenter_ice.config import Settings, VIENNA
from studycenter_ice.db import Database, encode
from studycenter_ice.google_calendar import projection
from studycenter_ice.ical import export_calendar
from studycenter_ice.models import Candidate
from studycenter_ice.presentation import item_tags


def source(db,day='2030-10-16',url='https://example.org/registration',text=None,**extra):
    course=db.one('SELECT id FROM courses')
    cid=course['id'] if course else db.execute("INSERT INTO courses(title,semester) VALUES('Messtechnik','2030W')")
    sid=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'teachcenter','Kursseite',?)",(cid,url))
    n=db.one('SELECT COUNT(*) n FROM documents')['n']+1
    quote=text or f'Anmeldung endet {day} um 10:00.'
    identifier=db.execute('INSERT INTO documents(source_id,url,title,mime,hash,fetched_at,original_path,pages,analysis_state) VALUES(?,?,?,?,?,?,?,?,?)',
        (sid,url,'Kursseite','text/html',str(n),f'2030-10-01T10:{n:02}:00+00:00','unused',encode([{'page':1,'text':quote}]),'success'))
    doc=db.one('SELECT * FROM documents WHERE id=?',(identifier,))
    value=Candidate(title='Gruppenanmeldung',kind='registration',identity_key='group-registration',identity_scope='course',
        semester='2030W',date=day,time='10:00',date_meaning='window',obligation='required',quote=quote,page=1,anchor='registration',**extra)
    iid=db.ingest(doc,value)
    return iid,doc


def row(db,iid): return next(i for i in db.items() if i['id']==iid)


def test_two_sources_apply_shared_projections_and_safe_reset(tmp_path):
    db=Database(tmp_path/'db.sqlite3'); iid,_=source(db); source(db,day='2030-10-09',url='https://example.org/other')
    before=db.rows('SELECT * FROM item_evidence'); raw=db.one('SELECT payload,status FROM items WHERE id=?',(iid,))
    view=comparison(db,iid); assert view['fact']=='closing' and len(view['cards'])==2 and not view['same_document']
    c=view['cards'][0]; assert save(db,iid,'closing',view['token'],c['key'])
    item=row(db,iid); assert not item['date_conflict'] and item['data']['date']==c['value']['date']
    assert item['personal_decisions'] and 'decided' in [t['code'] for t in item_tags(item)]
    current=datetime(2030,10,1,12,tzinfo=VIENNA); states={iid:action_state(item,current)}; events=calendar_events([item])
    ics,count=export_calendar([item],events,states); assert count==1 and 'Persönliche Entscheidung' in ics.decode('utf-8')
    projected,_=projection([item],events,states,{},'test')
    assert projected and c['value']['date'] in encode(projected) and 'Persönliche Entscheidung' in encode(projected)
    assert db.rows('SELECT * FROM item_evidence')==before and db.one('SELECT payload,status FROM items WHERE id=?',(iid,))==raw
    view=comparison(db,iid); assert not save(db,iid,'closing',view['token'],c['key'])
    # Another tab cannot overwrite a personal decision made after its preview.
    alternative=next(card for card in view['cards'] if card['key']!=c['key'])
    assert save(db,iid,'closing',view['token'],alternative['key'])
    with pytest.raises(ValueError): save(db,iid,'closing',view['token'],c['key'])
    view=comparison(db,iid)
    db.execute('UPDATE items SET overrides=? WHERE id=?',(encode({'date':'2030-10-21','time':'11:00:00'}),iid))
    with pytest.raises(ValueError): save(db,iid,'closing',view['token'],reset=True)
    fresh=comparison(db,iid); save(db,iid,'closing',fresh['token'],reset=True)
    assert row(db,iid)['data']['date']=='2030-10-21' and row(db,iid)['date_conflict']


INTERNAL='''###
Anmeldung zu Übungsgruppe
Gruppenwahl
Geöffnet:
Dienstag, 6. Oktober 2030, 19:00
Schließt:
Freitag, 16. Oktober 2030, 10:00
Sie müssen sich zu einer Gruppe bis
spätestens 9.10.2030
um
10 Uhr anmelden
###
Arbeitsblatt
Geöffnet:
Montag, 12. Oktober 2030, 00:00'''


def internal(db):
    iid,doc=source(db,text=INTERNAL,source_state='unclear',uncertainty='Widerspruch zwischen dem Schließzeitpunkt 16.10.2030 und dem Fließtext mit Frist 9.10.2030.')
    payload=json.loads(db.one('SELECT payload FROM items WHERE id=?',(iid,))['payload']); payload['quote']='Schließt:\\nFreitag, 16. Oktober 2030, 10:00'
    db.execute('UPDATE item_evidence SET quote=?,payload=? WHERE item_id=?',(payload['quote'],encode(payload),iid)); db.execute('UPDATE items SET payload=? WHERE id=?',(encode(payload),iid))
    return iid,doc


def test_internal_statements_opening_missing_and_other_fact_preserved(tmp_path):
    db=Database(tmp_path/'db.sqlite3'); iid,doc=internal(db); view=comparison(db,iid)
    assert view['same_document'] and len(view['cards'])==2 and {c['value']['date'] for c in view['cards']}=={'2030-10-16','2030-10-09'}
    item=row(db,iid); opening=item['evidence_resolution']['registration_opening']
    assert (opening['date'],opening['time'])==('2030-10-06','19:00:00') and opening['proofs'][0]['hash']==doc['hash']
    assert item['source_payload']['date']=='2030-10-16' and item['data']['date']=='2030-10-16' and item['data']['end_date'] is None
    assert comparison(db,iid,'start')['cards']==[]
    save(db,iid,'closing',view['token'],view['cards'][1]['key'])
    item=row(db,iid); assert item['data']['end_date']=='2030-10-09' and item['data']['source_state']=='active' and not item['conflict']
    assert item['status']=='open' and not item['booked_event']
    # A second independent policy conflict must not disappear on a temporal choice.
    db.execute("UPDATE item_evidence SET payload=json_set(payload,'$.obligation','optional') WHERE item_id=?",(iid,))
    source(db,day='2030-10-09',url='https://example.org/policy')
    item=row(db,iid); assert item['conflict'] and 'obligation' in item['evidence_resolution']['unresolved_fields']


def test_unchanged_new_version_not_stale_meaningful_new_version_warns(tmp_path):
    db=Database(tmp_path/'db.sqlite3'); iid,doc=internal(db); view=comparison(db,iid)
    save(db,iid,'closing',view['token'],view['cards'][0]['key'])
    def replacement(text,hash_):
        n=db.execute('INSERT INTO documents(source_id,url,title,mime,hash,fetched_at,original_path,pages,analysis_state) VALUES(?,?,?,?,?,?,?,?,?)',
            (doc['source_id'],doc['url'],doc['title'],doc['mime'],hash_,'2030-10-03T10:00:00+00:00','unused',encode([{'page':1,'text':text}]),'success'))
        db.execute('UPDATE item_evidence SET document_id=? WHERE item_id=?',(n,iid))
    replacement(INTERNAL+'\nNavigation geändert','new')
    assert not row(db,iid)['personal_decisions'][0]['stale']
    old=comparison(db,iid)
    replacement(INTERNAL.replace('9.10.2030','10.10.2030'),'changed')
    assert row(db,iid)['personal_decisions'][0]['stale'] and row(db,iid)['data']['end_date']=='2030-10-16'
    with pytest.raises(ValueError):save(db,iid,'closing',old['token'],old['cards'][0]['key'])


def test_routes_preview_only_one_apply_and_original_metadata(tmp_path):
    app=create_app(Settings(data_dir=tmp_path)); iid,_=internal(app.state.db); client=TestClient(app)
    page=client.get(f'/items/{iid}/compare'); assert page.status_code==200
    assert 'Veröffentlicht: nicht angegeben' in page.text and 'Abgerufen: 01.10.2030 12:01' in page.text
    assert 'Information fehlt' in client.get(f'/items/{iid}/compare?fact=start').text
    view=comparison(app.state.db,iid); before=app.state.db.rows('SELECT * FROM item_preferences')
    data={'fact':'closing','token':view['token'],'choice':view['cards'][1]['key'],'action':'preview'}
    response=client.post(f'/items/{iid}/compare',data=data,headers={'X-CSRF-Token':app.state.csrf})
    assert response.status_code==200 and 'Auswahl übernehmen' in response.text and app.state.db.rows('SELECT * FROM item_preferences')==before
    data['action']='apply'; response=client.post(f'/items/{iid}/compare',data=data,headers={'X-CSRF-Token':app.state.csrf},follow_redirects=False)
    assert response.status_code==303 and row(app.state.db,iid)['data']['end_date']=='2030-10-09'


@pytest.mark.parametrize('complete_first', [True, False])
def test_completion_and_conflict_decisions_are_independent_in_both_orders(tmp_path, complete_first):
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    iid,_=internal(db)
    linked=Candidate(title='Verknüpfte Vorbereitung',kind='task',related_key='group-registration',
                     quote='Eigene Planung',page=1,anchor='linked')
    other=db.execute('INSERT INTO items(course_id,identity,payload,status,created_at,changed_at) VALUES(?,?,?,?,?,?)',
        (row(db,iid)['course_id'],'note:linked',linked.model_dump_json(),'doing','2030-01-01','2030-01-01'))
    linked_before=db.one('SELECT * FROM items WHERE id=?',(other,))
    # A second linked entry must retain its own work status and preferences.
    db.execute('UPDATE items SET pinned=1 WHERE id=?',(iid,))
    db.execute('INSERT INTO item_preferences(item_id,value) VALUES(?,?)',(iid,encode({'mode':'off'})))
    before=db.rows('SELECT * FROM item_evidence ORDER BY id')
    original=db.one('SELECT payload,overrides FROM items WHERE id=?',(iid,))
    client=TestClient(app);headers={'X-CSRF-Token':app.state.csrf,'X-Requested-With':'fetch'}
    assert 'Dein Bearbeitungsstatus:' in client.get(f'/items/{iid}/compare').text
    view=comparison(db,iid)
    def status(value):
        response=client.post(f'/items/{iid}',data={'action':'status','status':value},headers=headers)
        assert response.status_code==200
        assert row(db,iid)['status']==value
    def decide(action='apply',current=None):
        current=current or comparison(db,iid)
        response=client.post(f'/items/{iid}/compare',data={'fact':'closing','action':action,
            'token':current['token'],'choice':current['cards'][1]['key']},headers=headers,follow_redirects=False)
        assert response.status_code==303
    if complete_first:status('done')
    decide(current=view)
    if not complete_first:status('done')
    assert row(db,iid)['status']=='done' and not row(db,iid)['conflict']
    # An already open comparison remains valid after a pure status change.
    status('open'); decision=row(db,iid)['personal_decisions']
    status('doing');status('done');status('open')
    assert row(db,iid)['personal_decisions']==decision
    assert comparison(db,iid)['token']!=view['token']  # Only the decision changed it.
    decide('reset');assert row(db,iid)['status']=='open' and row(db,iid)['conflict']
    status('done');decide();status('open')
    assert not row(db,iid)['conflict']
    assert db.rows('SELECT * FROM item_evidence ORDER BY id')==before
    assert db.one('SELECT payload,overrides FROM items WHERE id=?',(iid,))==original
    assert row(db,iid)['pinned'] and row(db,iid)['reminder_profile']['mode']=='off'
    assert db.one('SELECT * FROM items WHERE id=?',(other,))==linked_before


@pytest.mark.parametrize('complete_first', [True, False])
async def test_browser_can_complete_resolve_and_reopen_conflict_after_restart(tmp_path, monkeypatch, complete_first):
    from pathlib import Path
    from playwright.async_api import async_playwright, expect
    root=Path(__file__).resolve().parents[1]
    if (root/'.local/browsers').exists():monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(root/'.local/browsers'))
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db;iid,_=internal(db)
    original=db.rows('SELECT * FROM item_evidence ORDER BY id')
    client=TestClient(app,base_url='http://127.0.0.1');status_responses=[]
    async with async_playwright() as driver:
        executable=__import__('os').environ.get('STUDYCENTER_ICE_TEST_CHROMIUM',driver.chromium.executable_path)
        if not Path(executable).exists():pytest.skip('Project Chromium not installed')
        browser=await driver.chromium.launch(headless=True,executable_path=executable,chromium_sandbox=True);page=await browser.new_page()
        async def routes(route):
            request=route.request;path=request.url.split('127.0.0.1',1)[1]
            if path.startswith('/static/'):
                asset=root/'studycenter_ice/static'/path.split('?',1)[0].split('/')[-1]
                await route.fulfill(content_type='text/javascript' if asset.suffix=='.js' else 'text/css',body=asset.read_bytes())
            else:
                # Resolve redirects in-process too; a fulfilled 303 otherwise
                # leaves Playwright's route and tries a real port-80 listener.
                response=client.request(request.method,path,content=request.post_data_buffer,headers=request.headers,follow_redirects=True)
                if request.method=='POST' and path==f'/items/{iid}':status_responses.append(response.status_code)
                await route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.content)
        await page.route('**/*',routes)
        response=await page.goto(f'http://127.0.0.1/items/{iid}')
        assert response.headers['referrer-policy']=='same-origin'
        async def status(label,code):
            await page.get_by_role('button',name=f'{label}: Gruppenanmeldung',exact=True).click()
            await expect(page.locator(f'[data-status-choice="{code}"]')).to_have_attribute('aria-pressed','true')
            await expect(page.locator('.entry-detail')).to_be_visible()
        async def resolve():
            await page.get_by_role('link',name='Vergleichen →',exact=True).click()
            await page.get_by_role('button',name='Diese Angabe verwenden',exact=True).nth(1).click()
            await expect(page.get_by_role('button',name='Auswahl übernehmen',exact=True)).to_be_visible()
            await page.get_by_role('button',name='Auswahl übernehmen',exact=True).click()
            await expect(page.get_by_text('Von dir geklärt',exact=True)).to_be_visible()
            await page.locator(f'a[href="/items/{iid}"]').first.click()
        if not complete_first:await resolve()
        # Restart with the detail still open; the status action must recover once.
        client=TestClient(create_app(Settings(data_dir=tmp_path)),base_url='http://127.0.0.1')
        await status('Erledigt','done')
        if complete_first:await resolve()
        assert row(db,iid)['status']=='done' and not row(db,iid)['conflict']
        await status('Offen','open');await status('In Arbeit','doing');await status('Erledigt','done')
        await page.goto(f'http://127.0.0.1/items/{iid}/compare')
        await page.get_by_role('button',name='Persönliche Entscheidung zurücknehmen',exact=True).click()
        await page.locator(f'a[href="/items/{iid}"]').first.click()
        await status('Offen','open');await resolve()
        assert status_responses==[403,200,200,200,200,200]
        assert row(db,iid)['status']=='open' and not row(db,iid)['conflict']
        assert db.rows('SELECT * FROM item_evidence ORDER BY id')==original
        await browser.close()
