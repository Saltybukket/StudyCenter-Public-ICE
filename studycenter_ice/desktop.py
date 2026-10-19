"""Small user-owned tray controller. The existing backend is the only writer."""
import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import portalocker

from .config import ROOT, load_settings
from .launcher import configure_components
from .runtime import launch_quit

BASE='http://127.0.0.1:18791'


def health_receipt(data_dir, **values):
    """A previous successful renderer receipt must not mask a new failed start."""
    receipt = {'ready': False, 'server_error': False, 'pid': os.getpid(),
               'checked_at': time.time(), **values}
    (Path(data_dir) / 'desktop-health.json').write_text(json.dumps(receipt), encoding='utf-8')


def fresh_window_url(path):
    address=urlsplit(BASE+path)
    query=address.query+('&' if address.query else '')+'sc_window='+uuid4().hex
    return urlunsplit((address.scheme,address.netloc,address.path,query,address.fragment))


def backend_status(data_dir):
    try:
        result=httpx.get(BASE+'/api/desktop/status',timeout=2).json()
        expected=hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()[:16]
        if result.get('dataset_path')!=expected:return None
        return result
    except (httpx.HTTPError,ValueError):return None


def dashboard_ready():
    try:
        response=httpx.get(BASE+'/overview',timeout=3,headers={'Cache-Control':'no-cache'})
        return response.status_code==200 and '<main' in response.text and '<h1' in response.text
    except httpx.HTTPError:return False


def backend_released(data_dir):
    """The HTTP listener can close before worker receipts have finished."""
    try:
        with portalocker.Lock(str(Path(data_dir)/'app.owner.lock'),timeout=0):return True
    except portalocker.exceptions.LockException:return False


def ensure_backend(data_dir, timeout=45):
    if backend_status(data_dir):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if dashboard_ready():return
            time.sleep(.3)
        raise RuntimeError('StudyCenter ist erreichbar, aber die Startseite konnte nicht geladen werden. Kein zweites Backend gestartet.')
    # Never replace an unrelated listener or kill another user's process.
    try:
        httpx.get(BASE+'/api/status',timeout=1)
        raise RuntimeError('Port 18791 ist belegt oder StudyCenter-ICE benötigt zuerst einen regulären Neustart.')
    except httpx.HTTPError:pass
    owner=portalocker.Lock(str(Path(data_dir)/'app.owner.lock'),timeout=30)
    try:owner.acquire()
    except portalocker.exceptions.LockException:
        raise RuntimeError('StudyCenter wird noch beendet. Kein zweites Backend gestartet; kurz warten und erneut öffnen.') from None
    else:owner.release()
    python=Path(sys.executable).with_name('pythonw.exe') if sys.platform=='win32' else Path(sys.executable)
    platform_flags={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {'start_new_session':True}
    child=subprocess.Popen([str(python),'-m','studycenter_ice'],cwd=ROOT,
          env=os.environ.copy()|{'STUDYCENTER_ICE_DATA_DIR':str(data_dir)},**platform_flags,
          stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if backend_status(data_dir) and dashboard_ready():return
        if child.poll() is not None:raise RuntimeError('StudyCenter-Backendstart fehlgeschlagen; vorhandene Instanz/Logs prüfen.')
        time.sleep(.3)
    raise RuntimeError('StudyCenter ist noch nicht bereit. Kein zweiter Dienst gestartet.')


def action(path, **values):
    with httpx.Client(timeout=10) as client:
        response=client.get(BASE+'/settings');response.raise_for_status()
        match=re.search(r'name="csrf" value="([^"]+)"',response.text)
        if not match:raise RuntimeError('Lokaler Formularschutz fehlt.')
        response=client.post(BASE+path,data={'csrf':match[1],**values},headers={'Origin':BASE})
        if response.is_error:raise RuntimeError(response.json().get('detail','Aktion momentan nicht möglich.'))
        return response.json()


class Desktop:
    def __init__(self, settings, native=True):
        self.settings=settings
        self.native=native
        self.icon=None
        self.window=None
        self.webview=None
        self.gate=threading.Lock()
        self.quit_gate=threading.Lock()
        self.quitting=False
        self.hide_gate=threading.Lock()
        self.running=True
        open_marker=settings.data_dir/'desktop-open.signal'
        self.last_signal=open_marker.stat().st_mtime_ns if open_marker.exists() else 0
        stop_marker=settings.data_dir/'desktop-stop.signal'
        self.last_stop=stop_marker.stat().st_mtime_ns if stop_marker.exists() else 0
        self.message='StudyCenter – bereit'
        self.view_path='/overview'
        self.view_visible=False
        self.view_retry=0

    def show(self, path='/overview'):
        if self.native and self.window:
            self.view_path=path;self.view_visible=True;self.view_retry=0
            self.window.show();self.window.restore()
            # A fresh navigation must replace a previously failed/blank response.
            self.window.load_url(fresh_window_url(path))
        else:webbrowser.open(BASE+path)

    def command(self, path):
        if path=='/settings/stop':
            if not self.quit_gate.acquire(blocking=False):return
            try:
                launch_quit(self.settings.data_dir)
                self.quitting=True
                self.message='Vollständig beenden · laufende Arbeit wird abgebrochen; Hintergrunddienste schließen'
                self.icon.title=self.message[:127]
            except OSError:
                self.quit_gate.release()
                self.icon.notify('Der sichere Beenden-Helfer konnte nicht gestartet werden.','StudyCenter')
            return
        if not self.gate.acquire(blocking=False):return
        def run():
            try:
                result=action(path,**({'confirm':'yes'} if path=='/settings/stop' else {}))
                if path=='/settings/restart':self.message='StudyCenter startet neu'
                else:self.message='Manueller TU-Sync angefordert'
            except Exception as error:
                self.message=str(error)[:120]
                if not self.quitting:self.icon.notify(self.message,'StudyCenter')
            finally:self.gate.release()
        threading.Thread(target=run,daemon=True).start()

    def monitor(self):
        marker=self.settings.data_dir/'desktop-open.signal'
        stop_marker=self.settings.data_dir/'desktop-stop.signal'
        stopping=False
        while self.running:
            try:
                if marker.exists() and marker.stat().st_mtime_ns>self.last_signal:
                    self.last_signal=marker.stat().st_mtime_ns
                    self.show()
                status=backend_status(self.settings.data_dir)
                if stop_marker.exists() and stop_marker.stat().st_mtime_ns>self.last_stop:stopping=True
                if stopping and not status and backend_released(self.settings.data_dir):
                    self.running=False
                    if self.window:self.window.destroy()
                    self.icon.stop();return
                if not self.gate.locked():self.message='StudyCenter – '+('Wird beendet' if stopping else ', '.join(status.get('activities',[])) or 'bereit' if status else 'Backend nicht erreichbar')
                self.icon.title=self.message[:127]
            except Exception:logging.getLogger(__name__).warning('Tray-Status nicht erreichbar.')
            time.sleep(2)

    def run(self, silent):
        if sys.platform!='win32':
            if not silent:self.show()
            return
        from PIL import Image
        import pystray
        image=Image.open(ROOT/'studycenter_ice/static/studycenter.ico')
        menu=pystray.Menu(
            pystray.MenuItem('Dashboard',lambda:self.show(),default=True),
            pystray.MenuItem('Luna',lambda:self.show('/overview#voice')),
            pystray.MenuItem('TU-Sync jetzt',lambda:self.command('/api/sync')),
            pystray.MenuItem('Einstellungen',lambda:self.show('/settings')),
            pystray.MenuItem('Status',lambda:self.show('/settings#background')),
            pystray.MenuItem('Neustart',lambda:self.command('/settings/restart')),
            pystray.MenuItem('Vollständig beenden',lambda:self.command('/settings/stop')))
        self.icon=pystray.Icon('StudyCenter-Public-ICE',image,self.message,menu)
        if self.native:
            try:self.native_window(silent)
            finally:
                # A detached tray must not outlive the WebView event loop and
                # retain an icon after main() releases the instance owner lock.
                self.running=False
                self.icon.stop()
            return
        threading.Thread(target=self.monitor,daemon=True).start()
        if not silent:self.show()
        self.icon.run()

    def native_window(self, silent):
        if sys.platform!='win32':raise ValueError('Das native Windows-Fenster ist hier nicht verfügbar. Browsermodus verwenden.')
        # User-selected Windows default; human microphone acceptance remains separate.
        import webview
        self.webview=webview
        self.view_visible=not silent
        path=self.settings.data_dir/'desktop-window.json'
        try:
            bounds=json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(bounds,dict):raise ValueError('Invalid window bounds')
        except (OSError,ValueError):
            logging.getLogger(__name__).warning('Gespeicherte Fensterposition unlesbar; Standardgröße verwenden.')
            bounds={}
        bounds={k:v for k,v in bounds.items() if k in ('width','height','x','y') and type(v) is int
                and (k not in ('width','height') or 320<=v<=16384)}
        self.window=webview.create_window('StudyCenter-Public-ICE',fresh_window_url('/overview'),hidden=silent,background_color='#252830',**({'width':1120,'height':820}|bounds))
        def closing():
            if not self.running:return True
            bounds={k:getattr(self.window,k) for k in ('width','height','x','y')}
            self.view_visible=False
            self.window.hide()
            # FormClosing runs on the Windows UI thread. evaluate_js waits for a
            # reply on that same thread, so it must run after this handler returns.
            def cleanup():
                try:
                    self.window.evaluate_js("window.dispatchEvent(new Event('studycenter-hide'))")
                    path.write_text(json.dumps(bounds))
                except Exception as error:
                    logging.getLogger(__name__).warning('Fensterfreigabe fehlgeschlagen (%s).',type(error).__name__)
                finally:self.hide_gate.release()
            if self.hide_gate.acquire(blocking=False):
                threading.Thread(target=cleanup,daemon=True).start()
            return False
        def navigation(sender, args):
            url=str(args.Uri)
            address=__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(url)
            if address.scheme in ('http','https') and address.netloc=='127.0.0.1:18791':return
            args.Cancel=True
            if address.scheme in ('http','https'):webbrowser.open(url)
        self.window.events.closing+=closing
        guarded=False
        def guard():
            nonlocal guarded
            if guarded:return
            # Native WebView2 NavigationStarting precedes all document loads.
            self.window.native.webview.NavigationStarting+=navigation
            guarded=True
        self.window.events.before_show+=guard
        def loaded():
            try:
                health=self.window.evaluate_js("({ready:!!document.querySelector('main h1'),server_error:document.body?.textContent.trim()==='Internal Server Error',path:location.pathname})")
                if isinstance(health,str):health=json.loads(health)
                if not isinstance(health,dict):raise ValueError('Missing window load receipt')
                health={k:health.get(k) for k in ('ready','server_error','path')}
                health_receipt(self.settings.data_dir, **health, phase='ready' if health['ready'] else 'load_failed')
                if health['ready']:return
                logging.getLogger(__name__).warning('App-Seite nicht geladen: Serverfehler=%s, Pfad=%s.',health['server_error'],health['path'])
                if self.view_visible and self.view_retry<1:
                    self.view_retry+=1
                    def retry():
                        if self.running and self.view_visible:
                            try:self.window.load_url(fresh_window_url(self.view_path))
                            except Exception:logging.getLogger(__name__).warning('Erneutes Laden des App-Fensters fehlgeschlagen.')
                    timer=threading.Timer(1,retry);timer.daemon=True;timer.start()
            except Exception as error:
                logging.getLogger(__name__).warning('Fensterladeprüfung fehlgeschlagen (%s).',type(error).__name__)
        if hasattr(self.window.events,'loaded'):self.window.events.loaded+=loaded
        webview.settings['ALLOW_DOWNLOADS']=True
        def started():
            threading.Thread(target=self.monitor,daemon=True).start()
            self.icon.run_detached()
        # No js_api / shell bridge. Only the trusted local dashboard is loaded.
        webview.start(started,gui='edgechromium',private_mode=False,storage_path=str(self.settings.data_dir/'desktop-webview'),debug=False,icon=str(ROOT/'studycenter_ice/static/studycenter.ico'))


def main(argv=None):
    parser=argparse.ArgumentParser(prog='StudyCenter-Public-ICE')
    parser.add_argument('--silent',action='store_true')
    parser.add_argument('--data-dir',help='Eigenes ausgewähltes ICE-Datenverzeichnis')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--window',action='store_true',help='StudyCenter im eigenen Windows-Fenster öffnen (Standard)')
    mode.add_argument('--browser',action='store_true',help='Alternativ die Oberfläche im Systembrowser öffnen')
    arguments=parser.parse_args(argv)
    if arguments.data_dir:
        os.environ['STUDYCENTER_ICE_DATA_DIR']=str(Path(arguments.data_dir).expanduser().resolve())
    configure_components()
    if sys.platform=='win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('StudyCenter-Public-ICE.StudyCenter')
    settings=load_settings()
    log_dir=settings.data_dir/'logs';log_dir.mkdir(exist_ok=True)
    handler=RotatingFileHandler(log_dir/'desktop.log',maxBytes=128*1024,backupCount=2,encoding='utf-8')
    logging.getLogger(__name__).addHandler(handler)
    lock=portalocker.Lock(str(settings.data_dir/'desktop.owner.lock'),timeout=0)
    deadline=time.monotonic()+10
    while True:
        try:
            lock.acquire()
            break
        except portalocker.exceptions.LockException:
            status=backend_status(settings.data_dir)
            if status and not status.get('lifecycle_request'):
                if not arguments.silent:(settings.data_dir/'desktop-open.signal').touch()
                return
            # The previous controller may be finishing shutdown, or still
            # starting its backend. Wait for ownership or a reachable owner.
            if time.monotonic()>=deadline:return
            time.sleep(.2)
    try:
        health_receipt(settings.data_dir, phase='starting')
        ensure_backend(settings.data_dir)
        Desktop(settings,sys.platform=='win32' and not arguments.browser).run(arguments.silent)
        health_receipt(settings.data_dir, phase='stopped')
    except Exception as error:
        health_receipt(settings.data_dir, phase='start_failed', error_type=type(error).__name__)
        logging.getLogger(__name__).exception('Desktopstart fehlgeschlagen (%s).',type(error).__name__)
        if not arguments.silent and sys.platform=='win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,str(error),'StudyCenter',0x10)
    finally:lock.release()


if __name__=='__main__':main()
