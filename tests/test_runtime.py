from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import ctypes
import pytest

from studycenter_ice import __main__ as entry
from studycenter_ice import runtime


@pytest.mark.asyncio
async def test_shutdown_signals_all_workers_before_waiting_and_releases_owner_after_cleanup_error(tmp_path,monkeypatch):
    import asyncio,portalocker
    from studycenter_ice.app import create_app
    from studycenter_ice.config import Settings
    app=create_app(Settings(data_dir=tmp_path))
    google=app.state.google;tunnel=app.state.tunnel;reminders=app.state.reminders
    started=asyncio.Event();release=asyncio.Event()
    async def google_run(writable):
        started.set();await google.stop.wait();await release.wait()
    async def tunnel_run():await tunnel.stop_event.wait()
    async def reminder_run():await reminders.stop.wait()
    monkeypatch.setattr(google,'run',google_run)
    monkeypatch.setattr(tunnel,'run',tunnel_run)
    monkeypatch.setattr(reminders,'run',reminder_run)
    monkeypatch.setattr(app.state.search,'refresh',lambda:None)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(app.state.sync,'close',AsyncMock(side_effect=RuntimeError('fixture cleanup failure')))
    monkeypatch.setattr(app.state.browser,'close',AsyncMock())
    lifespan=app.router.lifespan_context(app)
    await lifespan.__aenter__();await started.wait()
    ending=asyncio.create_task(lifespan.__aexit__(None,None,None))
    await asyncio.sleep(0)
    try:
        assert google.stop.is_set() and tunnel.stop_event.is_set() and reminders.stop.is_set()
    finally:
        release.set();await asyncio.gather(ending,return_exceptions=True)
    app.state.browser.close.assert_awaited_once()
    with portalocker.Lock(str(tmp_path/'app.owner.lock'),timeout=0):pass


@pytest.mark.parametrize('enabled',[False,True])
@pytest.mark.skipif(runtime.sys.platform!='win32',reason='Native Windows registry and shell shortcuts')
def test_desktop_install_uses_redirected_desktop_and_keeps_autostart(tmp_path,monkeypatch,enabled):
    import winreg
    from contextlib import nullcontext
    menu=tmp_path/'Programs'/'Startup'/'StudyCenter-Public-ICE.lnk'
    desktop=tmp_path/'OneDrive'/'Desktop'
    created=[];autostart=[]
    monkeypatch.setattr(runtime,'shortcut_path',lambda:menu)
    monkeypatch.setattr(runtime,'autostart_enabled',lambda:enabled)
    monkeypatch.setattr(runtime,'create_shortcut',lambda path, **kwargs:created.append(path))
    monkeypatch.setattr(runtime,'set_autostart',lambda value, **kwargs:autostart.append(value))
    monkeypatch.setattr(winreg,'OpenKey',lambda *args:nullcontext('key'))
    monkeypatch.setattr(winreg,'QueryValueEx',lambda *args:(str(desktop),winreg.REG_EXPAND_SZ))
    assert runtime.install_desktop()==str(menu.parent.parent/'StudyCenter-Public-ICE.lnk')
    assert created==[menu.parent.parent/'StudyCenter-Public-ICE.lnk',desktop/'StudyCenter-Public-ICE.lnk']
    assert autostart==([True] if enabled else [])


@pytest.mark.skipif(runtime.sys.platform!='win32',reason='Windows shell link')
def test_shortcut_has_shell_identity_and_is_indexable(tmp_path):
    import base64,subprocess
    path=tmp_path/'StudyCenter-Public-ICE.lnk'
    runtime.create_shortcut(path)
    escaped=str(tmp_path).replace("'","''")
    script=f"$shell=New-Object -ComObject Shell.Application; $item=$shell.NameSpace('{escaped}').ParseName('StudyCenter-Public-ICE.lnk'); $item.ExtendedProperty('System.AppUserModel.ID'); (Get-Item -LiteralPath '{escaped}\\StudyCenter-Public-ICE.lnk').Attributes"
    command=base64.b64encode(script.encode('utf-16le')).decode()
    output=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',command],capture_output=True,text=True,errors='replace',check=True,creationflags=subprocess.CREATE_NO_WINDOW).stdout
    assert 'StudyCenter-Public-ICE.StudyCenter' in output
    assert 'NotContentIndexed' not in output


@pytest.mark.asyncio
async def test_autostart_opaque_app_origin_requires_current_header_token(tmp_path,monkeypatch):
    import httpx
    from studycenter_ice.app import create_app
    from studycenter_ice.config import Settings
    value={'enabled':False};calls=[]
    def save(enabled, **kwargs):value['enabled']=enabled;calls.append(enabled)
    monkeypatch.setattr('studycenter_ice.app.set_autostart',save)
    monkeypatch.setattr('studycenter_ice.app.autostart_enabled',lambda:value['enabled'])
    app=create_app(Settings(data_dir=tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        for headers in ({'Origin':'null'}, {'Origin':'null','X-CSRF-Token':'expired'},
                        {'Origin':'https://other.example','X-CSRF-Token':app.state.csrf}):
            assert (await client.post('/settings/autostart',data={'csrf':app.state.csrf,'enabled':'true'},headers=headers)).status_code==403
        assert not calls
        headers={'Origin':'null','X-CSRF-Token':app.state.csrf,'X-Requested-With':'fetch'}
        result=await client.post('/settings/autostart',data={'enabled':'true'},headers=headers)
        assert result.status_code==200 and result.json()['enabled'] and calls==[True]
        assert (await client.get('/settings')).status_code==200
        result=await client.post('/settings/autostart',data={},headers=headers)
        assert result.status_code==200 and not result.json()['enabled'] and calls==[True,False]
        opened=Mock(return_value=True)
        monkeypatch.setattr('studycenter_ice.app.webbrowser.open',opened)
        result=await client.post('/settings/google/setup',headers=headers)
        assert result.status_code==200
        opened.assert_called_once_with('https://console.cloud.google.com/apis/library/calendar-json.googleapis.com')
        assert not app.state.google.public()['connected'] and not app.state.google.options()['enabled']
        assert not app.state.db.rows('SELECT * FROM google_events')


@pytest.mark.skipif(runtime.sys.platform!='win32',reason='Native Windows hidden process flags')
def test_restart_spawns_hidden_successor_with_same_data_directory_and_surfaces_failure(tmp_path, monkeypatch):
    executable=tmp_path/'python.exe'
    pythonw=tmp_path/'pythonw.exe'
    pythonw.write_bytes(b'fixture')
    monkeypatch.setattr(runtime.sys,'platform','win32')
    monkeypatch.setattr(runtime.sys,'executable',str(executable))
    spawn=Mock(return_value=SimpleNamespace(pid=4321))
    monkeypatch.setattr(runtime.subprocess,'Popen',spawn)
    assert runtime.launch_restart(tmp_path)==4321
    args,kwargs=spawn.call_args
    assert args[0]==[str(pythonw),'-m','studycenter_ice','--after-process',str(runtime.os.getpid())]
    assert kwargs['env']['STUDYCENTER_ICE_DATA_DIR']==str(tmp_path.resolve())
    assert kwargs['creationflags']==runtime.subprocess.CREATE_NO_WINDOW and kwargs['close_fds'] is True
    assert all(kwargs[name]==runtime.subprocess.DEVNULL for name in ('stdin','stdout','stderr'))
    spawn.side_effect=OSError('fixture launch failed')
    with pytest.raises(OSError,match='fixture launch failed'):runtime.launch_restart(tmp_path)


@pytest.mark.skipif(runtime.sys.platform!='win32',reason='Native Windows handle/event loop API')
def test_restart_waits_on_handle_before_app_creation_and_fails_without_starting(tmp_path, monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(entry,'windows_shutdown_listener',lambda callback:nullcontext(False))
    calls=[]
    kernel=SimpleNamespace(OpenProcess=Mock(return_value=123),WaitForSingleObject=Mock(return_value=0),CloseHandle=Mock())
    monkeypatch.setattr(runtime.sys,'platform','win32')
    monkeypatch.setattr(ctypes,'WinDLL',lambda *a,**kw:kernel)
    runtime.wait_for_process_exit(123456)
    kernel.OpenProcess.assert_called_with(0x00100000,False,123456)
    kernel.WaitForSingleObject.assert_called_with(123,60000)
    kernel.CloseHandle.assert_called_once_with(123)
    def wait(pid):
        calls.append(('wait',pid))
    monkeypatch.setattr(entry,'wait_for_process_exit',wait)
    app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path)))
    monkeypatch.setattr(entry,'create_app',lambda:calls.append(('create',None)) or app)
    server=SimpleNamespace(run=lambda:calls.append(('run',None)),should_exit=False)
    monkeypatch.setattr(entry.uvicorn,'Config',lambda *a,**kw:None)
    monkeypatch.setattr(entry,'OwnedServer',lambda *a:server)
    entry.main(['--after-process','123456'])
    assert calls==[('wait',123456),('create',None),('run',None)]
    app.state.stop_callback()
    assert server.should_exit is True
    kernel.WaitForSingleObject.return_value=258
    monkeypatch.setattr(entry,'wait_for_process_exit',runtime.wait_for_process_exit)
    calls.clear()
    with pytest.raises(SystemExit) as timeout:entry.main(['--after-process','123456'])
    assert timeout.value.code==1 and calls==[]
    with pytest.raises(SystemExit) as invalid:entry.main(['--after-process','-1'])
    assert invalid.value.code==2 and calls==[]


def test_private_quit_message_does_not_need_http_or_windows_shutdown():
    stop=Mock();finished=Mock()
    assert runtime.end_session_message(runtime.QUIT_MESSAGE,False,stop,finished)==0
    stop.assert_called_once();finished.wait.assert_not_called()


def test_tray_full_quit_works_while_command_gate_is_busy(tmp_path,monkeypatch):
    from studycenter_ice import desktop
    controller=desktop.Desktop(SimpleNamespace(data_dir=tmp_path))
    controller.icon=Mock();controller.gate.acquire()
    launch=Mock();monkeypatch.setattr(desktop,'launch_quit',launch)
    monkeypatch.setattr(desktop,'action',Mock(side_effect=AssertionError('Quit must not need HTTP')))
    controller.command('/settings/stop');controller.command('/settings/stop')
    launch.assert_called_once_with(tmp_path)
    assert controller.quitting and 'abgebrochen' in controller.message
    controller.icon.notify.assert_not_called()


def test_terminate_rechecks_exact_creation_time_on_same_handle(monkeypatch):
    kernel=SimpleNamespace(OpenProcess=Mock(return_value=123),TerminateProcess=Mock(return_value=True),CloseHandle=Mock())
    old={'pid':100,'started':1,'exe':'pythonw.exe'}
    monkeypatch.setattr(runtime,'_process_api',lambda:kernel)
    monkeypatch.setattr(runtime,'_identity_on_handle',lambda *args:old|{'started':2})
    assert runtime.terminate_identity(old) is False
    kernel.TerminateProcess.assert_not_called();kernel.CloseHandle.assert_called_once_with(123)
    monkeypatch.setattr(runtime,'_identity_on_handle',lambda *args:old)
    assert runtime.terminate_identity(old) is True
    kernel.TerminateProcess.assert_called_once_with(123,1)


@pytest.mark.skipif(runtime.sys.platform!='win32',reason='Native own-process termination on Windows')
def test_native_fallback_stops_only_owned_tree_and_rolls_back_uncommitted_sqlite(tmp_path):
    import json,sqlite3,subprocess,sys,time
    database=tmp_path/'studycenter_ice.sqlite3'
    with sqlite3.connect(database) as con:
        con.execute('CREATE TABLE items(id INTEGER PRIMARY KEY,status TEXT)')
        con.execute("INSERT INTO items VALUES(1,'open')")
        con.execute('CREATE TABLE google_events(id TEXT)')
        con.execute("INSERT INTO google_events VALUES('stable-google-id')")
    script='''import sqlite3,sys,time,subprocess
from pathlib import Path
con=sqlite3.connect(sys.argv[1]);con.execute('BEGIN IMMEDIATE');con.execute("UPDATE items SET status='doing'")
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'],creationflags=subprocess.CREATE_NO_WINDOW)
Path(sys.argv[2]).write_text(str(child.pid))
time.sleep(90)
'''
    ready=tmp_path/'ready'
    owned=subprocess.Popen([sys.executable,'-c',script,str(database),str(ready)],creationflags=subprocess.CREATE_NO_WINDOW)
    foreign=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'],creationflags=subprocess.CREATE_NO_WINDOW)
    saved=[]
    try:
        deadline=time.monotonic()+10
        while not ready.exists() and time.monotonic()<deadline:time.sleep(.05)
        assert ready.exists()
        identity=runtime.process_identity(owned.pid);assert identity
        saved=runtime.process_family(identity)
        assert len(saved)>=2
        (tmp_path/'backend-owner.json').write_text(json.dumps({'root':str(runtime.ROOT.resolve()),
            'data_dir':str(tmp_path.resolve()),'backend':identity,'extra':[]}),encoding='utf8')
        assert runtime.quit_owned(tmp_path,grace=.1)
        owned.wait(timeout=3)
        assert all(runtime.process_identity(p['pid'])!=p for p in saved)
        assert foreign.poll() is None
        with sqlite3.connect(database) as con:
            assert con.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert con.execute('SELECT status FROM items').fetchone()[0]=='open'
            assert con.execute('SELECT id FROM google_events').fetchone()[0]=='stable-google-id'
    finally:
        for identity in saved:runtime.terminate_identity(identity)
        foreign.terminate();foreign.wait(timeout=3)
        if owned.poll() is None:owned.terminate();owned.wait(timeout=3)


@pytest.mark.asyncio
async def test_owned_server_bounds_blocked_http_and_signals_workers_before_drain():
    import asyncio,socket,httpx,uvicorn
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    stopped=asyncio.Event();entered=asyncio.Event();cleaned=asyncio.Event()
    @asynccontextmanager
    async def lifespan(app):
        yield
        cleaned.set()
    app=FastAPI(lifespan=lifespan)
    app.state.begin_stop=lambda system:stopped.set()
    @app.get('/held')
    async def held():
        entered.set();await asyncio.Event().wait()
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    address='http://127.0.0.1:'+str(sock.getsockname()[1])
    server=entry.OwnedServer(uvicorn.Config(app,log_config=None,access_log=False,timeout_graceful_shutdown=.1))
    serving=asyncio.create_task(server.serve(sockets=[sock]));request=None
    try:
        while not server.started:await asyncio.sleep(.01)
        async with httpx.AsyncClient(timeout=3) as client:
            request=asyncio.create_task(client.get(address+'/held'))
            await asyncio.wait_for(entered.wait(),3)
            server.should_exit=True
            await asyncio.wait_for(stopped.wait(),2)
            assert not cleaned.is_set()  # Stop workers before waiting for HTTP.
            await asyncio.wait_for(serving,2)
            assert cleaned.is_set()
            await asyncio.gather(request,return_exceptions=True)
    finally:
        if request:request.cancel()
        serving.cancel();await asyncio.gather(serving,return_exceptions=True);sock.close()


@pytest.mark.asyncio
async def test_pending_restore_allows_confirmed_lifecycle_but_not_edits(tmp_path,monkeypatch):
    import asyncio,httpx
    from studycenter_ice.app import create_app
    from studycenter_ice.config import Settings
    app=create_app(Settings(data_dir=tmp_path))
    launched,stopped=[],[]
    app.state.stop_callback=lambda:stopped.append(True)
    monkeypatch.setattr('studycenter_ice.app.launch_restart',lambda directory:launched.append(directory))
    (tmp_path/'restore-pending.zip').write_bytes(b'pending marker; lifespan deliberately not started')
    headers={'X-CSRF-Token':app.state.csrf}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        assert (await client.post('/settings/restart')).status_code==403
        assert (await client.post('/settings/aliases',data={'alias':'test','course_id':1},headers=headers)).status_code==409
        result=await client.post('/settings/restart',headers=headers)
        assert result.status_code==200 and result.json()['instance_id']==app.state.instance_id
        assert (await client.post('/settings/restart',headers=headers)).status_code==200
        assert len(launched)==1 and app.state.lifecycle_request=='restart'
        assert (await client.post('/settings/backups/cancel',headers=headers)).status_code==409
        await asyncio.sleep(1.05)
        assert stopped==[True] and (tmp_path/'restore-pending.zip').exists()
        assert (await client.get('/api/status')).json()['instance_id']==app.state.instance_id
    # A fresh app owner may also be explicitly stopped with a pending restore.
    other=create_app(Settings(data_dir=tmp_path));other.state.stop_callback=lambda:None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other),base_url='http://testserver') as client:
        headers={'X-CSRF-Token':other.state.csrf}
        assert (await client.post('/settings/stop',headers=headers)).status_code==400
        assert (await client.post('/settings/stop',data={'confirm':'yes'},headers=headers)).status_code==200
        assert other.state.lifecycle_request=='stop'


@pytest.mark.asyncio
async def test_restart_rejects_busy_work_and_keeps_app_alive_when_launch_fails(tmp_path,monkeypatch):
    import httpx
    from studycenter_ice.app import create_app
    from studycenter_ice.config import Settings
    app=create_app(Settings(data_dir=tmp_path));stopped=[]
    app.state.stop_callback=lambda:stopped.append(True)
    spawn=Mock(side_effect=OSError('fixture unavailable'))
    monkeypatch.setattr('studycenter_ice.app.launch_restart',spawn)
    headers={'X-CSRF-Token':app.state.csrf}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
        app.state.maintenance=True
        assert (await client.post('/settings/restart',headers=headers)).status_code==409
        app.state.maintenance=False
        await app.state.assistant.gate.acquire()
        assert (await client.post('/settings/restart',headers=headers)).status_code==409
        app.state.assistant.gate.release()
        app.state.speech.lock.acquire()
        assert (await client.post('/settings/restart',headers=headers)).status_code==409
        app.state.speech.lock.release()
        assert spawn.call_count==0
        assert (await client.post('/settings/restart',headers=headers)).status_code==500
        assert app.state.lifecycle_request=='' and not stopped
        assert (await client.get('/overview')).status_code==200


@pytest.mark.asyncio
async def test_quit_during_google_signals_stop_keeps_settings_and_blocks_backup(tmp_path):
    import asyncio,httpx
    from studycenter_ice.app import create_app
    from studycenter_ice.config import Settings
    app=create_app(Settings(data_dir=tmp_path));google=app.state.google;db=app.state.db
    app.state.stop_callback=Mock();before=db.rows('SELECT * FROM app_preferences')
    await google.gate.acquire()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
            headers={'X-CSRF-Token':app.state.csrf}
            blocked=await client.post('/settings/backups/create',headers=headers)
            assert blocked.status_code==409 and 'Google-Kalenderabgleich' in blocked.json()['detail']
            status=(await client.get('/api/desktop/status')).json()
            assert status['activities']==['Google-Kalenderabgleich']
            result=await client.post('/settings/stop',data={'confirm':'yes'},headers=headers)
            assert result.status_code==200 and 'Google-Kalenderabgleich läuft aus' in result.json()['message']
            assert google.stop.is_set() and app.state.tunnel.stop_event.is_set() and app.state.reminders.stop.is_set()
            assert app.state.lifecycle_request=='stop' and db.rows('SELECT * FROM app_preferences')==before
            await asyncio.sleep(1.05);app.state.stop_callback.assert_called_once()
    finally:google.gate.release()


@pytest.mark.asyncio
@pytest.mark.parametrize('busy,label',[('sync','TU-Quellen-Synchronisierung'),('assistant','Luna-Anfrage'),('speech','Spracherkennung')])
async def test_quit_still_protects_other_work_and_names_the_reason(tmp_path,busy,label):
    import asyncio,httpx
    from studycenter_ice.app import create_app
    from studycenter_ice.config import Settings
    app=create_app(Settings(data_dir=tmp_path));app.state.stop_callback=Mock()
    task=None
    if busy=='sync':task=app.state.sync.task=asyncio.create_task(asyncio.Event().wait())
    elif busy=='assistant':await app.state.assistant.gate.acquire()
    else:app.state.speech.lock.acquire()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as client:
            result=await client.post('/settings/stop',data={'confirm':'yes'},headers={'X-CSRF-Token':app.state.csrf})
            assert result.status_code==409 and label.lower() in result.json()['detail'].lower()
            assert app.state.lifecycle_request=='' and not app.state.google.stop.is_set()
            app.state.stop_callback.assert_not_called()
    finally:
        if task:task.cancel();await asyncio.gather(task,return_exceptions=True)
        elif busy=='assistant':app.state.assistant.gate.release()
        else:app.state.speech.lock.release()


def test_end_session_respects_aborted_shutdown_and_waits_only_on_confirmed_end():
    stop=Mock();finished=Mock()
    assert runtime.end_session_message(0x11,False,stop,finished)==1
    assert runtime.end_session_message(0x16,False,stop,finished)==0
    stop.assert_not_called();finished.wait.assert_not_called()
    assert runtime.end_session_message(0x16,True,stop,finished)==0
    stop.assert_called_once();finished.wait.assert_called_once_with(4)


def test_closed_http_is_not_released_database_owner(tmp_path):
    import portalocker
    from studycenter_ice.desktop import backend_released
    with portalocker.Lock(str(tmp_path/'app.owner.lock'),timeout=0):
        assert not backend_released(tmp_path)
    assert backend_released(tmp_path)


@pytest.mark.asyncio
async def test_confirmed_system_shutdown_signals_workers_and_cancels_source_task(tmp_path,monkeypatch):
    import asyncio
    from studycenter_ice.app import create_app
    from studycenter_ice.config import Settings
    from unittest.mock import AsyncMock
    app=create_app(Settings(data_dir=tmp_path));app.state.stop_callback=Mock()
    async def idle(stop):await stop.wait()
    monkeypatch.setattr(app.state.google,'run',lambda writable:idle(app.state.google.stop))
    monkeypatch.setattr(app.state.tunnel,'run',lambda:idle(app.state.tunnel.stop_event))
    monkeypatch.setattr(app.state.reminders,'run',lambda:idle(app.state.reminders.stop))
    monkeypatch.setattr(app.state.search,'refresh',lambda:None)
    monkeypatch.setattr(app.state.browser,'close',AsyncMock())
    async with app.router.lifespan_context(app):
        app.state.sync.task=asyncio.create_task(asyncio.Event().wait())
        await app.state.google.gate.acquire()
        try:
            app.state.system_shutdown()
            await asyncio.sleep(.15)
            assert app.state.lifecycle_request=='stop' and app.state.sync.task.cancelled()
            assert app.state.google.stop.is_set() and app.state.reminders.stop.is_set()
            app.state.stop_callback.assert_called_once()
        finally:app.state.google.gate.release()


@pytest.mark.skipif(runtime.sys.platform!='win32',reason='Windows shutdown receiver registration')
def test_native_shutdown_receiver_registers_without_a_visible_window():
    # Install/remove only our own listener. No shutdown message or PC shutdown.
    with runtime.windows_shutdown_listener(Mock()) as ready:assert ready
