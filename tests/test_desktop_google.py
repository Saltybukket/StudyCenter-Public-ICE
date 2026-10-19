"""Focused local controls; Google responses are doubles, never live acceptance."""
import json
import asyncio
import copy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from studycenter_ice.browser import BrowserService, session_signal
from studycenter_ice.config import Settings, VIENNA
from studycenter_ice.db import Database, encode
from studycenter_ice.google_calendar import GoogleCalendar, projection, event_id, owned_fields
from studycenter_ice.app import action_state, calendar_events


def auth_fixture(tmp_path):
    from studycenter_ice.google_calendar import GoogleAuth
    auth=GoogleAuth(tmp_path)
    stored={'client_id':'fixture.apps.googleusercontent.com','client_secret':'fixture-secret',
            'refresh_token':'old-refresh','access_token':'old-access'}
    auth.read=lambda:copy.deepcopy(stored)
    def save(value):stored.clear();stored.update(copy.deepcopy(value))
    auth.save=save
    return auth,stored


def oauth_listener(callback,port=19000):
    return SimpleNamespace(callback=callback,sockets=[SimpleNamespace(getsockname=lambda:('127.0.0.1',port))],
                           close=Mock(),wait_closed=AsyncMock())


@pytest.mark.asyncio
async def test_google_parallel_connect_owns_one_listener_and_close_keeps_tokens(tmp_path,monkeypatch):
    from studycenter_ice.google_calendar import GoogleError
    auth,stored=auth_fixture(tmp_path);before=copy.deepcopy(stored)
    entered=asyncio.Event();release=asyncio.Event();servers=[]
    async def start(callback,*args,**kwargs):
        server=oauth_listener(callback);servers.append(server);entered.set()
        await release.wait();return server
    monkeypatch.setattr('studycenter_ice.google_calendar.asyncio.start_server',start)
    monkeypatch.setattr('studycenter_ice.google_calendar.webbrowser.open',Mock())
    first=asyncio.create_task(auth.connect(AsyncMock()))
    await entered.wait()
    second=asyncio.create_task(auth.connect(AsyncMock()))
    await asyncio.sleep(0);release.set()
    results=await asyncio.gather(first,second,return_exceptions=True)
    await auth.close()
    assert len(servers)==1 and sum(isinstance(result,GoogleError) for result in results)==1
    servers[0].close.assert_called_once()
    assert stored==before and auth.server is None and auth.pending is None


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_stage',['exchange','connected'])
async def test_google_disconnect_stops_callback_before_late_token_or_target_write(tmp_path,monkeypatch,blocked_stage):
    from studycenter_ice.google_calendar import SCOPES
    auth,stored=auth_fixture(tmp_path);servers=[];entered=asyncio.Event();release=asyncio.Event();effects=[]
    async def start(callback,*args,**kwargs):
        server=oauth_listener(callback);servers.append(server);return server
    class Client:
        def __init__(self,*args,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):
            if blocked_stage=='exchange':entered.set();await release.wait()
            return SimpleNamespace(status_code=200,json=lambda:{'scope':' '.join(SCOPES),
                'refresh_token':'new-refresh','access_token':'new-access','expires_in':3600})
    async def connected():
        if blocked_stage=='connected':entered.set();await release.wait()
        effects.append('target-written')
    monkeypatch.setattr('studycenter_ice.google_calendar.asyncio.start_server',start)
    monkeypatch.setattr('studycenter_ice.google_calendar.webbrowser.open',Mock())
    monkeypatch.setattr('studycenter_ice.google_calendar.httpx.AsyncClient',Client)
    await auth.connect(connected)
    reader=SimpleNamespace(readuntil=AsyncMock(return_value=f"GET /callback?state={auth.pending['state']}&code=fixture HTTP/1.1\r\n\r\n".encode()))
    writer=SimpleNamespace(write=Mock(),drain=AsyncMock(),close=Mock())
    callback=asyncio.create_task(servers[0].callback(reader,writer))
    await entered.wait()
    # A repeated callback cannot retire the listener while its owner is exchanging the code.
    await servers[0].callback(reader,SimpleNamespace(write=Mock(),drain=AsyncMock(),close=Mock()))
    assert auth.server is servers[0]
    await auth.close(disconnect=True)
    release.set();await asyncio.gather(callback,return_exceptions=True)
    assert stored=={'client_id':'fixture.apps.googleusercontent.com','client_secret':'fixture-secret'}
    assert effects==[] and writer.close.called and auth.server is None
    # A retired listener cannot consume or close a subsequent login.
    await auth.connect(connected)
    pending=copy.deepcopy(auth.pending)
    await servers[0].callback(reader,writer)
    assert auth.pending==pending and auth.server is servers[1]
    servers[1].close.assert_not_called()
    reader.readuntil.return_value=f"GET /callback?state={pending['state']}&code=fixture HTTP/1.1\r\n\r\n".encode()
    await servers[1].callback(reader,writer)
    assert effects==['target-written'] and stored['refresh_token']=='new-refresh'
    assert auth.server is None and auth.pending is None and not auth.callback_tasks
    await auth.close()


@pytest.mark.asyncio
async def test_service_probes_keep_unfinished_login_and_classify_real_signals(tmp_path):
    html='<div class="usermenu"><a href="logout.php">Ich</a></div><main id="region-main">Kurse</main>'
    assert session_signal('teachcenter',html,'https://tc.tugraz.at/main/my/')=='authenticated'
    assert session_signal('teachcenter','<input type="password">','https://tc.tugraz.at/main/login/')=='login_required'
    assert session_signal('tugrazonline','<div class="profile-username">Ich</div>Meine Lehrveranstaltungen','https://online.tugraz.at/tug_online/')=='authenticated'
    assert session_signal('tugrazonline','Loading','https://online.tugraz.at/tug_online/')=='unknown'
    assert session_signal('mail','DimpCore','https://webmail.tugraz.at/imp/dynamic.php',503)=='unreachable'
    service=BrowserService(Settings(data_dir=tmp_path))
    pages=[]
    async def new_page():
        page=SimpleNamespace(url='https://tc.tugraz.at/main/my/',goto=AsyncMock(return_value=SimpleNamespace(status=200)),
            content=AsyncMock(return_value=html),close=AsyncMock(),is_closed=lambda:False)
        pages.append(page);return page
    service._ensure=AsyncMock(return_value=SimpleNamespace(new_page=new_page))
    service._close=AsyncMock();service.save_session=AsyncMock()
    service.visible=True;service.login_mode=True
    service.login_pages={'online.tugraz.at':SimpleNamespace(url='https://sso.tugraz.at/login')}
    assert (await service.probe(['teachcenter']))['teachcenter']['state']=='authenticated'
    service._close.assert_not_called()  # Unfinished other login stays open.
    pages[0].close.assert_awaited_once()
    service.login_pages={'tc.tugraz.at':SimpleNamespace(url='https://tc.tugraz.at/main/my/')}
    await service.probe(['teachcenter']);service._close.assert_awaited_once()
    await service.lock.acquire()
    assert (await service.probe(['mail']))['mail']['state']=='busy'
    service.lock.release()
    service._ensure.side_effect=TimeoutError()
    # Actual network timeouts inside a tab are unreachable, never a login prompt.
    service._ensure.side_effect=None
    async def failing_page():
        return SimpleNamespace(goto=AsyncMock(side_effect=TimeoutError()),close=AsyncMock(),is_closed=lambda:False)
    service._ensure.return_value=SimpleNamespace(new_page=failing_page)
    assert (await service.probe(['teachcenter']))['teachcenter']['state']=='unreachable'


def local_item(db, **changes):
    from studycenter_ice.models import Candidate
    from studycenter_ice.documents import DocumentStore
    settings=Settings(data_dir=db.path.parent)
    course=db.execute("INSERT INTO courses(title,semester) VALUES('Testkurs','2026W')")
    source=db.execute("INSERT INTO sources(course_id,kind,title,url) VALUES(?,'public','Original','https://example.org/read')",(course,))
    store=DocumentStore(db,settings)
    doc=store.process(store.save(source,'https://example.org/read','Original','text/plain',b'Abgabe am 28. Oktober'))
    candidate=Candidate(title='Abgabe',identity_key='A2',identity_scope='course',kind='task',date='2026-10-28',time='12:00',date_meaning='deadline',obligation='required',page=1,anchor='A2',quote='Abgabe am 28. Oktober')
    candidate=candidate.model_copy(update=changes)
    return db.ingest(doc,candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupted',[False,True])
async def test_shutdown_during_remote_write_keeps_stable_receipt_and_recovers_no_duplicate(tmp_path,interrupted):
    settings=Settings(data_dir=tmp_path);db=Database(tmp_path/'studycenter_ice.sqlite3')
    entries=[];remote={};writes=[]
    service=GoogleCalendar(db,settings,lambda options:(entries,[]));service.initialize()
    options=service.options();options.update(enabled=True,consent=True,calendar_id='owned',last_success='last-complete');service.save(options)
    for n in range(2):
        identity='fixture:'+str(n)
        entries.append({'identity':identity,'role':'closing','event_id':event_id(options['dataset'],identity,'closing'),
            'payload':{'summary':'Own task '+str(n),'start':{'date':'2030-10-28'},'end':{'date':'2030-10-29'},
                       'extendedProperties':{'private':{'studycenter_dataset':options['dataset']}}}})
    async def request(method,path,**kwargs):
        if method=='GET':
            key=path.rsplit('/',1)[-1]
            return httpx.Response(200,json=remote[key]) if key in remote else httpx.Response(404,json={})
        assert method=='POST';payload=copy.deepcopy(kwargs['json']);key=payload.pop('id');writes.append(key)
        assert db.one('SELECT event_id,state FROM google_events')=={'event_id':key,'state':'pending'}
        remote[key]=payload|{'etag':'confirmed'};service.stop.set()
        if interrupted:raise asyncio.CancelledError()
        return httpx.Response(201,json=remote[key])
    service.request=request
    if interrupted:
        with pytest.raises(asyncio.CancelledError):await service.sync()
    else:await service.sync()
    identifier=entries[0]['event_id']
    assert writes==[identifier] and service.options()['last_success']=='last-complete'
    assert db.one('SELECT event_id,state FROM google_events')=={'event_id':identifier,'state':'pending' if interrupted else 'synced'}
    recovered=GoogleCalendar(db,settings,lambda options:(entries[:1],[]));recovered.request=request
    await recovered.sync()
    assert writes==[identifier] and len(remote)==1
    assert db.one('SELECT event_id,state FROM google_events')=={'event_id':identifier,'state':'synced'}
    assert recovered.options()['dataset']==options['dataset'] and recovered.options()['calendar_id']=='owned'


@pytest.mark.asyncio
async def test_shutdown_during_google_read_never_starts_new_write(tmp_path):
    settings=Settings(data_dir=tmp_path);db=Database(tmp_path/'studycenter_ice.sqlite3')
    entry={'identity':'fixture','role':'closing','event_id':'stable','payload':{}}
    service=GoogleCalendar(db,settings,lambda options:([entry],[]));service.initialize()
    options=service.options();options.update(enabled=True,consent=True,calendar_id='owned');service.save(options)
    async def request(method,path,**kwargs):
        assert method=='GET';service.stop.set();return httpx.Response(404,json={})
    service.request=request;await service.sync()
    assert not db.rows('SELECT * FROM google_events') and not service.gate.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize('deleting',[False,True])
async def test_google_stop_keeps_partial_batch_receipts_without_claiming_success(tmp_path,deleting):
    settings=Settings(data_dir=tmp_path);db=Database(tmp_path/'studycenter_ice.sqlite3')
    entries=[{'identity':f'fixture:{n}','role':'deadline','event_id':f'id{n}'} for n in range(3)]
    service=GoogleCalendar(db,settings,lambda options:([] if deleting else entries,[]));service.initialize()
    options=service.options();options.update(enabled=True,consent=True,calendar_id='owned',last_success='previous-complete-batch',selected=9)
    service.save(options)
    if deleting:
        for entry in entries:
            db.execute("INSERT INTO items(identity,payload,status,created_at,changed_at) VALUES(?,?,'done','fixture','fixture')",
                (entry['identity'],encode({'source_state':'active'})))
            service.receipt('owned',entry['identity'],entry['role'],entry['event_id'],{},'fixture','synced')
    published=[]
    async def publish(calendar,row,entry):
        published.append((entry or row)['event_id'])
        service.stop.set()  # The current remote operation completed; stop before the next one.
    service.publish=publish
    await service.sync()
    assert published==['id0']
    assert service.options()['last_success']=='previous-complete-batch'
    assert service.options()['selected']==9
    assert not service.options().get('error')
    assert not service.gate.locked()


@pytest.mark.asyncio
async def test_google_projection_idempotency_move_offline_conflict_cancel_restore(tmp_path):
    settings=Settings(data_dir=tmp_path);db=Database(tmp_path/'studycenter_ice.sqlite3')
    identifier=local_item(db)
    db.execute("UPDATE items SET status='doing',pinned=1,overrides=? WHERE id=?",(encode({'description':'Meine Notiz'}),identifier))
    before=db.one('SELECT status,pinned,overrides FROM items WHERE id=?',(identifier,))
    def provider(options):
        items=db.items();return projection(items,calendar_events(items),{i['id']:action_state(i,datetime.now(VIENNA)) for i in items},options,options['dataset'])
    service=GoogleCalendar(db,settings,provider);service.initialize()
    options=service.options();options.update(enabled=True,consent=True,calendar_id='owned');service.save(options)
    service.auth=SimpleNamespace(token=AsyncMock(return_value='fixture'),read=lambda:{'refresh_token':'fixture'})
    remote={};writes=[];offline=False
    async def transport(request):
        if offline:raise httpx.ConnectError('offline')
        assert '/calendars/owned/events' in request.url.path
        key=request.url.path.rsplit('/',1)[-1]
        if request.method=='GET':return httpx.Response(200,json=remote[key]) if key in remote else httpx.Response(404,json={})
        payload=json.loads(request.content) if request.content else None
        if request.method=='POST':
            key=payload.pop('id')
            if key in remote:return httpx.Response(409,json={})
        if request.method in ('PATCH','DELETE'):
            assert request.headers['If-Match']==remote[key]['etag']
        writes.append(request.method)
        if request.method=='DELETE':remote.pop(key);return httpx.Response(204)
        # Google merges nested PATCH objects and deletes explicit null fields.
        def merge(current, patch):
            for name,value in patch.items():
                if value is None:current.pop(name,None)
                elif isinstance(value,dict):merge(current.setdefault(name,{}),value)
                else:current[name]=value
        remote.setdefault(key,{})
        merge(remote[key],payload)
        remote[key]['etag']=str(len(writes))
        return httpx.Response(200,json=remote[key])
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service.http=client
        await service.sync();assert writes==['POST'] and len(remote)==1
        key=next(iter(remote));assert remote[key]['start']['dateTime'].endswith('+01:00')
        assert remote[key]['end']['dateTime'].startswith('2026-10-28T12:01:00')
        assert remote[key]['transparency']=='transparent' and remote[key]['reminders']=={'useDefault':False}
        await service.sync();assert writes==['POST']
        from studycenter_ice.documents import DocumentStore
        from studycenter_ice.models import Candidate
        item=db.one('SELECT payload FROM items WHERE id=?',(identifier,));payload=json.loads(item['payload'])
        source=db.one('SELECT * FROM sources WHERE course_id=?',(db.one('SELECT course_id FROM items WHERE id=?',(identifier,))['course_id'],))
        store=DocumentStore(db,settings)
        def move_source(day):
            quote='Abgabe am '+day
            doc=store.process(store.save(source['id'],source['url'],'Original','text/plain',quote.encode()))
            db.ingest(doc,Candidate(**(payload|{'date':day,'quote':quote})))
        move_source('2026-10-29')
        offline=True;await service.sync();assert service.error and writes==['POST']
        offline=False;await service.sync();assert writes==['POST','PATCH'] and next(iter(remote))==key
        remote[key]['summary']='Extern verändert'
        await service.sync();assert db.one('SELECT state FROM google_events')['state']=='conflict' and len(writes)==2
        await service.resolve(db.one('SELECT identity FROM items')['identity'],'closing',False)
        assert remote[key]['summary']=='Frist: Abgabe' and writes[-1]=='PATCH'
        remote[key]['colorId']='7'  # Unmanaged Google fields survive subsequent PATCH.
        move_source('2026-10-30')
        await service.sync();assert remote[key]['colorId']=='7'
        # Partial sources do not remove existing events; only conscious deselection does.
        db.execute("UPDATE sources SET state='login_required'");await service.sync();assert len(remote)==1
        options=service.options();options['selection']={db.one('SELECT identity FROM items')['identity']:False};service.save(options)
        await service.sync();assert not remote and writes[-1]=='DELETE'
        service.initialize(restored=True);assert not service.options()['enabled'] and service.options()['restore_paused']
        count=len(writes);await service.sync();assert len(writes)==count
    assert db.one('SELECT status,pinned,overrides FROM items WHERE id=?',(identifier,))==before


@pytest.mark.asyncio
async def test_google_omitted_busy_default_recovers_only_equivalent_conflicts(tmp_path):
    settings=Settings(data_dir=tmp_path)
    db=Database(tmp_path/'studycenter_ice.sqlite3');identifier=local_item(db)
    db.execute("UPDATE items SET status='doing',pinned=1,overrides=? WHERE id=?",(encode({'description':'Meine Notiz'}),identifier))
    before=db.one('SELECT * FROM items WHERE id=?',(identifier,))
    service=GoogleCalendar(db,settings,lambda _: ([entry],[]));service.initialize()
    dataset=service.options()['dataset']
    payload={'summary':'Testtermin','description':'Original',
             'start':{'dateTime':'2026-10-28T09:00:00+01:00','timeZone':'Europe/Vienna'},
             'end':{'dateTime':'2026-10-28T10:00:00+01:00','timeZone':'Europe/Vienna'},
             'transparency':'opaque','reminders':{'useDefault':False},
             'extendedProperties':{'private':{'studycenter_dataset':dataset}}}
    entry={'identity':'probe','role':'start','event_id':event_id(dataset,'probe','start'),'payload':payload}
    service.save({**service.options(),'enabled':True,'consent':True,'calendar_id':'owned'})
    service.auth=SimpleNamespace(token=AsyncMock(return_value='fixture'),read=lambda:{'refresh_token':'fixture'})
    remote={};writes=[]
    def transport(request):
        if request.method=='GET':return httpx.Response(200,json=remote) if remote else httpx.Response(404,json={})
        writes.append(request.method)
        remote.update(json.loads(request.content));remote.pop('transparency',None)
        remote['etag']='unchanged';return httpx.Response(200,json=remote)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service.http=client
        await service.sync();assert writes==['POST']
        assert owned_fields(payload)==owned_fields(remote)
        db.execute("UPDATE google_events SET state='conflict',error='Legacy default comparison'")
        await service.sync()
        assert db.one('SELECT state,error FROM google_events')=={'state':'synced','error':''}
        assert writes==['POST']
        remote['transparency']='transparent'
        await service.sync();assert db.one('SELECT state FROM google_events')['state']=='conflict'
        await service.sync();assert db.one('SELECT state FROM google_events')['state']=='conflict'
        assert writes==['POST']  # A genuine external edit is never overwritten.
        remote.clear()
        await service.sync();assert db.one('SELECT state FROM google_events')['state']=='conflict'
        assert writes==['POST']  # An externally deleted conflict must not be recreated.
    assert db.one('SELECT * FROM items WHERE id=?',(identifier,))==before


def test_google_dates_optional_quiz_no_booking_and_backup(tmp_path):
    from studycenter_ice.backup import create_backup,inspect_archive
    settings=Settings(data_dir=tmp_path)
    db=Database(tmp_path/'studycenter_ice.sqlite3');local_item(db,time=None)
    items=db.items();states={i['id']:action_state(i,datetime.now(VIENNA)) for i in items}
    selected,_=projection(items,calendar_events(items),states,{},'dataset')
    assert selected[0]['payload']['start']=={'date':'2026-10-28'}
    assert selected[0]['payload']['end']=={'date':'2026-10-29'}
    assert event_id('dataset','A2','closing')==event_id('dataset','A2','closing')
    item=items[0];item['data'].update(kind='exam',date_meaning='window',date='2026-10-13',time='18:00',end_date='2026-10-19',end_time='12:00',obligation='optional')
    selected,_=projection([item],[],{item['id']:action_state(item,datetime.now(VIENNA))},{},'dataset')
    assert selected[0]['optional'] and selected[0]['payload']['start']['dateTime'].startswith('2026-10-19T12:00')
    assert 'keine Pflicht' in selected[0]['payload']['description']
    item['data'].update(date_meaning='start',date='2026-10-19',end_date=None,end_time=None)
    selected,skipped=projection([item],[],{item['id']:action_state(item,datetime.now(VIENNA))},{},'dataset')
    assert not selected and skipped
    item['data'].update(kind='task',date_meaning='deadline',obligation='required',group='2')
    item['course_group']='1'
    selected,skipped=projection([item],calendar_events([item]),{item['id']:action_state(item,datetime.now(VIENNA))},{},'dataset')
    assert not selected and skipped[0]['reason']=='Andere Übungsgruppe.'
    service=GoogleCalendar(db,settings,lambda _: ([],[]));service.initialize()
    archive=create_backup(db,tmp_path);manifest=inspect_archive(archive,tmp_path/'checked')
    assert manifest['counts']['google_events']==0
    assert not any('google-' in name or 'dpapi' in name for name in manifest['files'])


def test_desktop_reuses_only_confirmed_backend_and_does_not_replace_port(tmp_path,monkeypatch):
    from studycenter_ice import desktop
    process=Mock();monkeypatch.setattr(desktop.subprocess,'Popen',process)
    monkeypatch.setattr(desktop,'dashboard_ready',lambda:True)
    monkeypatch.setattr(desktop,'backend_status',lambda _: {'active':False})
    desktop.ensure_backend(tmp_path);process.assert_not_called()
    monkeypatch.setattr(desktop,'backend_status',lambda _: None)
    monkeypatch.setattr(desktop.httpx,'get',lambda *args,**kwargs:SimpleNamespace())
    with pytest.raises(RuntimeError,match='Port 18791'):desktop.ensure_backend(tmp_path)
    process.assert_not_called()


def test_desktop_defaults_to_windows_window_with_explicit_browser_fallback(tmp_path,monkeypatch):
    import logging
    from studycenter_ice import desktop
    launched=[]
    monkeypatch.setattr(desktop,'load_settings',lambda:Settings(data_dir=tmp_path))
    monkeypatch.setattr(desktop,'RotatingFileHandler',lambda *a,**kw:logging.NullHandler())
    monkeypatch.setattr(desktop.portalocker,'Lock',lambda *a,**kw:SimpleNamespace(acquire=lambda:None,release=lambda:None))
    monkeypatch.setattr(desktop,'ensure_backend',lambda _:None)
    monkeypatch.setattr(desktop,'Desktop',lambda settings,native:SimpleNamespace(run=lambda silent:launched.append((native,silent))))
    desktop.main([])
    desktop.main(['--silent'])
    desktop.main(['--browser'])
    assert launched==[(True,False),(True,True),(False,False)]


def test_native_close_returns_before_webview_reply_and_keeps_background(tmp_path,monkeypatch):
    import sys,threading
    from studycenter_ice.desktop import Desktop
    class Event:
        def __init__(self):self.handlers=[]
        def __iadd__(self,handler):self.handlers.append(handler);return self
    ui_available=threading.Event();audio_done=threading.Event();calls=[]
    def evaluate(script):
        calls.append('audio')
        assert ui_available.wait(1), 'Close handler is waiting on its own UI thread'
        audio_done.set()
    window=SimpleNamespace(width=1120,height=820,x=10,y=20,
        events=SimpleNamespace(closing=Event(),before_show=Event()),
        evaluate_js=evaluate,hide=lambda:calls.append('hide'))
    view=SimpleNamespace(create_window=lambda *a,**kw:window,settings={},start=lambda *a,**kw:None)
    monkeypatch.setitem(sys.modules,'webview',view)
    desktop=Desktop(Settings(data_dir=tmp_path));desktop.native_window(False)
    assert window.events.closing.handlers[0]() is False
    ui_available.set()  # Windows message loop can process the JavaScript reply now.
    assert audio_done.wait(2)
    assert calls==['hide','audio'] and desktop.running
    desktop.running=False
    assert window.events.closing.handlers[0]() is True


def test_native_window_exit_stops_detached_tray_before_owner_unlock(tmp_path,monkeypatch):
    import sys
    from studycenter_ice.desktop import Desktop
    from PIL import Image
    icon=Mock()
    monkeypatch.setattr(Image,'open',lambda _:None)
    monkeypatch.setitem(sys.modules,'pystray',SimpleNamespace(Menu=lambda *a:None,MenuItem=lambda *a,**kw:None,Icon=lambda *a:icon))
    desktop=Desktop(Settings(data_dir=tmp_path))
    monkeypatch.setattr(desktop,'native_window',lambda _:None)
    desktop.run(False)
    assert desktop.running is False
    icon.stop.assert_called_once()


def test_native_open_does_not_replay_stale_signal_and_forces_fresh_navigation(tmp_path):
    from studycenter_ice.desktop import Desktop
    from urllib.parse import urlsplit,parse_qs
    settings=Settings(data_dir=tmp_path)
    marker=tmp_path/'desktop-open.signal';marker.touch()
    controller=Desktop(settings)
    assert controller.last_signal==marker.stat().st_mtime_ns
    controller.window=SimpleNamespace(show=Mock(),restore=Mock(),load_url=Mock())
    controller.show('/settings#background');controller.show('/settings#background')
    first,second=[call.args[0] for call in controller.window.load_url.call_args_list]
    assert first!=second and urlsplit(second).fragment=='background'
    assert urlsplit(second).path=='/settings' and parse_qs(urlsplit(second).query)['sc_window']
    controller.window.show.assert_called()


def test_native_dashboard_check_rejects_server_error_or_empty_document(monkeypatch):
    from studycenter_ice import desktop
    response=SimpleNamespace(status_code=500,text='Internal Server Error')
    monkeypatch.setattr(desktop.httpx,'get',lambda *args,**kwargs:response)
    assert not desktop.dashboard_ready()
    response.status_code=200;response.text=''
    assert not desktop.dashboard_ready()
    response.text='<main id="main"><h1>Übersicht</h1></main>'
    assert desktop.dashboard_ready()


def test_native_failed_load_has_one_retry_and_truthful_receipt(tmp_path,monkeypatch):
    import sys,json
    from studycenter_ice import desktop as module
    class Event:
        def __init__(self):self.handlers=[]
        def __iadd__(self,handler):self.handlers.append(handler);return self
    window=SimpleNamespace(events=SimpleNamespace(closing=Event(),before_show=Event(),loaded=Event()),
        load_url=Mock(),evaluate_js=Mock(return_value={'ready':False,'server_error':True,'path':'/overview'}))
    view=SimpleNamespace(create_window=Mock(return_value=window),settings={},start=Mock())
    class Timer:
        def __init__(self,seconds,callback):self.callback=callback
        def start(self):self.callback()
    monkeypatch.setitem(sys.modules,'webview',view);monkeypatch.setattr(module.threading,'Timer',Timer)
    controller=module.Desktop(Settings(data_dir=tmp_path));controller.native_window(False)
    load=window.events.loaded.handlers[0];load();load()
    assert window.load_url.call_count==1
    assert json.loads((tmp_path/'desktop-health.json').read_text())['server_error'] is True
    window.evaluate_js.return_value={'ready':True,'server_error':False,'path':'/overview'};load()
    assert json.loads((tmp_path/'desktop-health.json').read_text())['ready'] is True


def test_second_desktop_start_signals_owner_without_starting_another_app(tmp_path,monkeypatch):
    import logging
    from studycenter_ice import desktop
    monkeypatch.setattr(desktop,'load_settings',lambda:Settings(data_dir=tmp_path))
    monkeypatch.setattr(desktop,'backend_status',lambda _: {'active':False})
    monkeypatch.setattr(desktop,'RotatingFileHandler',lambda *a,**kw:logging.NullHandler())
    lock=SimpleNamespace(acquire=Mock(side_effect=desktop.portalocker.exceptions.AlreadyLocked('owned')),release=Mock())
    monkeypatch.setattr(desktop.portalocker,'Lock',lambda *a,**kw:lock)
    backend=Mock();controller=Mock()
    monkeypatch.setattr(desktop,'ensure_backend',backend)
    monkeypatch.setattr(desktop,'Desktop',controller)
    desktop.main([])
    assert (tmp_path/'desktop-open.signal').exists()
    backend.assert_not_called();controller.assert_not_called();lock.release.assert_not_called()
    (tmp_path/'desktop-open.signal').unlink()
    desktop.main(['--silent'])
    assert not (tmp_path/'desktop-open.signal').exists()


def test_desktop_restart_waits_for_stopping_owner_before_reusing_port(tmp_path,monkeypatch):
    import logging
    from studycenter_ice import desktop
    monkeypatch.setattr(desktop,'load_settings',lambda:Settings(data_dir=tmp_path))
    monkeypatch.setattr(desktop,'RotatingFileHandler',lambda *a,**kw:logging.NullHandler())
    monkeypatch.setattr(desktop,'backend_status',lambda _:None)
    monkeypatch.setattr(desktop.time,'sleep',lambda _:None)
    lock=SimpleNamespace(acquire=Mock(side_effect=[desktop.portalocker.exceptions.AlreadyLocked('stopping'),None]),release=Mock())
    monkeypatch.setattr(desktop.portalocker,'Lock',lambda *a,**kw:lock)
    backend=Mock();run=Mock()
    monkeypatch.setattr(desktop,'ensure_backend',backend)
    monkeypatch.setattr(desktop,'Desktop',lambda *a:SimpleNamespace(run=run))
    desktop.main([])
    assert lock.acquire.call_count==2
    backend.assert_called_once_with(tmp_path);run.assert_called_once_with(False);lock.release.assert_called_once()
    assert not (tmp_path/'desktop-open.signal').exists()


def test_desktop_never_spawns_backend_while_old_backend_still_owns_data(tmp_path,monkeypatch):
    from studycenter_ice import desktop
    monkeypatch.setattr(desktop,'backend_status',lambda _:None)
    monkeypatch.setattr(desktop.httpx,'get',Mock(side_effect=httpx.ConnectError('port already closed')))
    lock=SimpleNamespace(acquire=Mock(side_effect=desktop.portalocker.exceptions.AlreadyLocked('shutdown')),release=Mock())
    monkeypatch.setattr(desktop.portalocker,'Lock',lambda *a,**kw:lock)
    spawn=Mock();monkeypatch.setattr(desktop.subprocess,'Popen',spawn)
    with pytest.raises(RuntimeError,match='Kein zweites Backend'):desktop.ensure_backend(tmp_path)
    spawn.assert_not_called();lock.release.assert_not_called()


def test_discord_import_date_provenance_replay_and_private_google_projection(tmp_path):
    import re
    from studycenter_ice.app import create_app
    from fastapi.testclient import TestClient
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    app.state.sync.imported=AsyncMock()
    with TestClient(app) as client:
        token=re.search(r'name="csrf-token" content="([^"]+)"',client.get('/sources').text)[1]
        values={'text':'Morgen bitte vorbereiten.','url':'https://discord.com/channels/1/2/3','title':'Kursnachricht'}
        response=client.post('/import',data=values,headers={'X-CSRF-Token':token})
        assert response.status_code==400
        assert not db.rows('SELECT * FROM sources') and not db.rows('SELECT * FROM documents')
        values['published_at']='2030-10-12'
        assert client.post('/import',data=values,headers={'X-CSRF-Token':token}).status_code==200
        assert client.post('/import',data=values,headers={'X-CSRF-Token':token}).status_code==200
        docs=db.rows('SELECT * FROM documents')
        assert len(docs)==1 and docs[0]['published_at']=='2030-10-12'
        assert docs[0]['title'].startswith('Übernommene Discord-Nachricht')
        assert not db.rows('SELECT * FROM items')
    identifier=local_item(db)
    item=db.items()[0];item['third_party_message']=True
    selected,skipped=projection([item],calendar_events([item]),{item['id']:action_state(item,datetime.now(VIENNA))},{},'dataset')
    assert not selected and 'Dritt-Nachricht' in skipped[0]['reason']
