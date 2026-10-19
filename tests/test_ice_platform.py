"""Portability contracts; all profiles and components are synthetic and isolated."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from studycenter_ice import runtime


def test_linux_does_not_discover_windows_autostart(tmp_path, monkeypatch):
    shortcut = tmp_path / 'StudyCenter-Public-ICE.lnk'
    shortcut.write_bytes(b'synthetic link')
    monkeypatch.setattr(runtime.sys, 'platform', 'linux')
    monkeypatch.setattr(runtime, 'shortcut_path', lambda: shortcut)
    assert runtime.autostart_enabled() is False
    with pytest.raises(ValueError, match='Windows'):
        runtime.create_shortcut(shortcut)
    assert shortcut.read_bytes() == b'synthetic link'


def test_speech_uses_only_selected_data_and_never_downloads_implicitly(tmp_path, monkeypatch):
    from studycenter_ice.speech import SpeechService, SpeechError
    monkeypatch.setenv('STUDYCENTER_ICE_SPEECH_DIR', str(tmp_path / 'outside'))
    service = SpeechService(data_dir=tmp_path / 'selected')
    assert service.root == (tmp_path / 'selected' / 'speech-models').resolve()
    with pytest.raises(SpeechError, match='herunterladen'):
        service.load()
    assert not service.root.exists()


@pytest.mark.asyncio
async def test_browser_uses_own_profile_and_enabled_sandbox(tmp_path, monkeypatch):
    from studycenter_ice import browser
    settings = SimpleNamespace(data_dir=tmp_path/'selected')
    old = settings.data_dir/'browser-profile'/'Default'/'Local Storage'
    old.mkdir(parents=True)
    (old/'never-migrate').write_text('synthetic old profile')
    monkeypatch.setenv('STUDYCENTER_ICE_BROWSER_DIR', str(tmp_path/'outside'))
    monkeypatch.setenv('STUDYCENTER_ICE_COMPONENT_ROOT', str(tmp_path/'components'))
    monkeypatch.setattr(browser.sys, 'platform', 'win32')
    context = SimpleNamespace(set_default_timeout=lambda value:None, on=lambda *args:None,
                              cookies=AsyncMock(return_value=[]), close=AsyncMock())
    launch = AsyncMock(return_value=context)
    driver = SimpleNamespace(chromium=SimpleNamespace(executable_path=str(tmp_path/'unused'),launch_persistent_context=launch),stop=AsyncMock())
    monkeypatch.setattr(browser, 'async_playwright', lambda:SimpleNamespace(start=AsyncMock(return_value=driver)))
    service = browser.BrowserService(settings)
    await service._ensure()
    assert launch.call_args.args[0] == str(settings.data_dir/'browser-chromium-profile')
    assert launch.call_args.kwargs['chromium_sandbox'] is True
    assert not (settings.data_dir/'browser-chromium-profile'/'Default'/'Local Storage').exists()
    assert not (tmp_path/'outside').exists()
    assert browser.os.environ['PLAYWRIGHT_BROWSERS_PATH'] == str(tmp_path/'components'/'browsers')
    await service._close()


@pytest.mark.asyncio
async def test_linux_sandbox_failure_is_reported_without_retry_or_policy_changes(tmp_path, monkeypatch):
    import portalocker
    from studycenter_ice import browser
    monkeypatch.setattr(browser.sys, 'platform', 'linux')
    monkeypatch.setattr(browser, 'linux_system_chrome', lambda:Path('/usr/bin/chromium'))
    launch = AsyncMock(side_effect=RuntimeError('synthetic AppArmor denial'))
    driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch),stop=AsyncMock())
    monkeypatch.setattr(browser, 'async_playwright', lambda:SimpleNamespace(start=AsyncMock(return_value=driver)))
    service = browser.BrowserService(SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(browser.BrowserUnavailable, match='AppArmor'):
        await service._ensure()
    assert launch.await_count == 1
    assert launch.call_args.kwargs['chromium_sandbox'] is True
    assert launch.call_args.kwargs['executable_path'] == str(Path('/usr/bin/chromium'))
    assert service.playwright is None and service.profile_lock is None
    with portalocker.Lock(str(tmp_path/'browser.owner.lock'),timeout=0):pass


def test_disabling_ice_autostart_preserves_earlier_app_links(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.sys, 'platform', 'win32')
    own = tmp_path/'StudyCenter-Public-ICE.lnk'
    earlier = tmp_path/'StudyCenter-ICE.lnk'
    own.write_bytes(b'ICE link')
    earlier.write_bytes(b'earlier app link')
    monkeypatch.setattr(runtime, 'shortcut_path', lambda:own)
    runtime.set_autostart(False)
    assert not own.exists()
    assert earlier.read_bytes() == b'earlier app link'
    with pytest.raises(ValueError,match='eigene'):
        runtime.create_shortcut(earlier)


def test_linux_desktop_uses_browser_without_tray_or_native_window(tmp_path, monkeypatch):
    from studycenter_ice import desktop
    monkeypatch.setattr(desktop.sys,'platform','linux')
    opened=[]
    controller=desktop.Desktop(SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(controller,'show',lambda:opened.append('own dashboard'))
    controller.run(False)
    assert opened == ['own dashboard']


def test_speech_download_is_an_explicit_owned_action(tmp_path, monkeypatch):
    import sys
    from studycenter_ice.speech import SpeechService
    downloaded=[]
    def download(name,output_dir,**options):
        downloaded.append((name,output_dir))
        assert options == {'cache_dir':str(tmp_path/'speech-models'/'download-cache'),'use_auth_token':False}
        (Path(output_dir)/'model.bin').write_bytes(b'synthetic model')
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(download_model=download))
    service=SpeechService(data_dir=tmp_path,engine='whisper',name='base')
    assert downloaded == []
    assert service.download_model() == {'model':'base','state':'ready'}
    assert downloaded == [('base',str(tmp_path/'speech-models'/'base'))]
    assert not service.lock.locked()


def test_owned_templates_parse_and_keep_seven_settings_tabs():
    from bs4 import BeautifulSoup
    from jinja2 import Environment
    from studycenter_ice.config import ROOT
    for name in ('settings.html','model.html','voice.html'):
        source=(ROOT/'studycenter_ice'/'templates'/name).read_text(encoding='utf-8')
        Environment().parse(source)
        if name=='settings.html':
            panes=BeautifulSoup(source,'html.parser').select('[data-settings-pane]')
            assert [pane['data-settings-pane'] for pane in panes] == ['google','notifications','backups','speech','display','connections','background']


def test_shortcut_carries_selected_dataset_and_silent_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.subprocess,'CREATE_NO_WINDOW',0,raising=False)
    import base64
    from unittest.mock import Mock
    root=tmp_path/'install';root.mkdir()
    selected=tmp_path/'selected dataset'
    python=tmp_path/'python.exe'
    monkeypatch.setattr(runtime.sys,'platform','win32')
    monkeypatch.setattr(runtime.sys,'executable',str(python))
    monkeypatch.setattr(runtime,'ROOT',root)
    command=Mock()
    monkeypatch.setattr(runtime.subprocess,'run',command)
    monkeypatch.setattr(runtime,'register_shortcut',lambda path:None)
    runtime.create_shortcut(tmp_path/'StudyCenter-Public-ICE.lnk',silent=True,data_dir=selected)
    encoded=command.call_args.args[0][-1]
    script=base64.b64decode(encoded).decode('utf-16le')
    assert '-m studycenter_ice.desktop --data-dir "'+str(selected)+'" --silent' in script


def test_desktop_data_selection_precedes_settings_loading(tmp_path,monkeypatch):
    from studycenter_ice import desktop
    selected=tmp_path/'selected';selected.mkdir()
    seen=[]
    monkeypatch.setattr(desktop.sys,'platform','linux')
    monkeypatch.setattr(desktop,'configure_components',lambda:seen.append(('components',desktop.os.environ.get('STUDYCENTER_ICE_DATA_DIR'))))
    monkeypatch.setattr(desktop,'load_settings',lambda:seen.append(desktop.os.environ.get('STUDYCENTER_ICE_DATA_DIR')) or SimpleNamespace(data_dir=selected))
    monkeypatch.setattr(desktop,'ensure_backend',lambda data:None)
    monkeypatch.setattr(desktop,'health_receipt',lambda *args,**kwargs:None)
    monkeypatch.setattr(desktop,'Desktop',lambda settings,native:SimpleNamespace(run=lambda silent:seen.append(silent)))
    monkeypatch.setenv('STUDYCENTER_ICE_DATA_DIR',str(tmp_path/'wrong'))
    desktop.main(['--data-dir',str(selected),'--silent'])
    assert seen == [('components',str(selected.resolve())),str(selected.resolve()),True]


def test_process_family_excludes_os_console_hosts_without_touching_foreign_processes(monkeypatch):
    from unittest.mock import Mock
    records={1:{'pid':1,'started':100,'exe':r'C:\ICE\python.exe'},
             2:{'pid':2,'started':110,'exe':r'C:\ICE\python.exe'},
             3:{'pid':3,'started':120,'exe':r'C:\Windows\System32\conhost.exe'},
             4:{'pid':4,'started':130,'exe':r'C:\Windows\OpenConsole.exe'},
             5:{'pid':5,'started':140,'exe':r'C:\foreign\python.exe'}}
    tree=iter([(1,0),(2,1),(3,1),(4,2),(5,999)])
    def advance(snapshot,pointer):
        try:pid,parent=next(tree)
        except StopIteration:return False
        pointer._obj.pid=pid;pointer._obj.parent=parent
        return True
    kernel=SimpleNamespace(CreateToolhelp32Snapshot=Mock(return_value=123),Process32FirstW=Mock(side_effect=advance),
                           Process32NextW=Mock(side_effect=advance),CloseHandle=Mock())
    monkeypatch.setattr(runtime,'process_identity',lambda pid:records.get(pid))
    monkeypatch.setattr(runtime,'_process_api',lambda:kernel)
    assert runtime.process_family(records[1]) == [records[1],records[2]]
