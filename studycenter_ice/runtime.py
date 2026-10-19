"""Optional per-user Windows startup; one existing app owner lock, no source job."""
import base64
import ntpath
import os
import subprocess
import sys
from pathlib import Path
from contextlib import contextmanager
from .config import ROOT

QUIT_MESSAGE = 0x8001  # Private backend lifecycle message; never a broadcast.
SHORTCUT_NAME = 'StudyCenter-Public-ICE.lnk'


def _own_shortcut(path):
    path=Path(path)
    if path.name.casefold()!=SHORTCUT_NAME.casefold() or path.is_symlink():
        raise ValueError('Nur die eigene StudyCenter-Public-ICE-Verknüpfung darf geändert werden.')
    return path


def _process_api():
    if sys.platform!='win32':raise ValueError('Windows-Prozessverwaltung ist hier nicht verfügbar.')
    import ctypes
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    signatures={
        'OpenProcess':([w.DWORD,w.BOOL,w.DWORD],w.HANDLE),
        'GetProcessTimes':([w.HANDLE,*([ctypes.POINTER(w.FILETIME)]*4)],w.BOOL),
        'QueryFullProcessImageNameW':([w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)],w.BOOL),
        'TerminateProcess':([w.HANDLE,w.UINT],w.BOOL), 'CloseHandle':([w.HANDLE],w.BOOL)}
    for name,(args,result) in signatures.items():
        fn=getattr(kernel,name);fn.argtypes=args;fn.restype=result
    return kernel


def _identity_on_handle(kernel,handle,pid):
    import ctypes
    from ctypes import wintypes as w
    times=[w.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle,*(ctypes.byref(t) for t in times)):return None
    size=w.DWORD(32768);path=ctypes.create_unicode_buffer(size.value)
    if not kernel.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(size)):return None
    return {'pid':pid,'started':times[0].dwHighDateTime<<32|times[0].dwLowDateTime,'exe':path.value.casefold()}


def process_identity(pid):
    if sys.platform!='win32':return None
    kernel=_process_api();handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:return None
    try:return _identity_on_handle(kernel,handle,pid)
    finally:kernel.CloseHandle(handle)


def save_backend_owner(data_dir,extra=None):
    """PID reuse-safe local ownership, written by the database owner only."""
    if sys.platform!='win32':return
    import json
    directory=Path(data_dir).resolve();path=directory/'backend-owner.json'
    own=process_identity(os.getpid())
    if not own:return
    value={'root':str(ROOT.resolve()),'data_dir':str(directory),'backend':own,'extra':[]}
    if extra is not None:
        try:value=json.loads(path.read_text(encoding='utf8'))
        except (OSError,ValueError):return
        if value.get('backend')!=own:return
        child=process_identity(extra)
        value['extra']=[child] if child else []
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value),encoding='utf8');os.replace(temporary,path)


def owned_backend(data_dir):
    import json
    try:value=json.loads((Path(data_dir)/'backend-owner.json').read_text(encoding='utf8'))
    except (OSError,ValueError):return None
    if not isinstance(value,dict) or value.get('root')!=str(ROOT.resolve()) or value.get('data_dir')!=str(Path(data_dir).resolve()):return None
    def valid(record):
        return (isinstance(record,dict) and type(record.get('pid')) is int and record['pid']>0
                and type(record.get('started')) is int and isinstance(record.get('exe'),str))
    if not valid(value.get('backend')) or not isinstance(value.get('extra',[]),list) or not all(valid(p) for p in value.get('extra',[])):return None
    return value


def process_family(root):
    """Snapshot descendants while the exact parent is alive; never match by name."""
    console_hosts={'conhost.exe','openconsole.exe'}
    if ntpath.basename(root.get('exe','')).casefold() in console_hosts:return []
    if process_identity(root['pid'])!=root:return []
    import ctypes
    from ctypes import wintypes as w
    class Entry(ctypes.Structure):
        _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),
                  ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),('flags',w.DWORD),('exe',w.WCHAR*260)]
    kernel=_process_api()
    kernel.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];kernel.CreateToolhelp32Snapshot.restype=w.HANDLE
    for name in ('Process32FirstW','Process32NextW'):
        fn=getattr(kernel,name);fn.argtypes=[w.HANDLE,ctypes.POINTER(Entry)];fn.restype=w.BOOL
    snapshot=kernel.CreateToolhelp32Snapshot(2,0)
    if snapshot==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    parents={};entry=Entry();entry.size=ctypes.sizeof(entry)
    try:
        more=kernel.Process32FirstW(snapshot,ctypes.byref(entry))
        while more:
            parents[entry.pid]=entry.parent
            more=kernel.Process32NextW(snapshot,ctypes.byref(entry))
    finally:kernel.CloseHandle(snapshot)
    found=[root]
    for parent in found:
        for pid,ppid in parents.items():
            if ppid==parent['pid'] and pid not in {p['pid'] for p in found}:
                child=process_identity(pid)
                if child and child['started']>=parent['started'] and ntpath.basename(child['exe']).casefold() not in console_hosts:
                    found.append(child)
    return found


def terminate_identity(identity):
    """Check creation time on the same handle used to terminate: no PID race."""
    import ctypes
    kernel=_process_api();handle=kernel.OpenProcess(0x1001,False,identity['pid'])
    if not handle:return False
    try:
        if _identity_on_handle(kernel,handle,identity['pid'])!=identity:return False
        if not kernel.TerminateProcess(handle,1):raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:kernel.CloseHandle(handle)


def request_backend_quit(identity):
    if process_identity(identity['pid'])!=identity:return False
    import ctypes
    from ctypes import wintypes as w
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.FindWindowW.argtypes=[w.LPCWSTR,w.LPCWSTR];user.FindWindowW.restype=w.HWND
    user.PostMessageW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM];user.PostMessageW.restype=w.BOOL
    hwnd=user.FindWindowW('StudyCenterPublicICEShutdown'+str(identity['pid']),None)
    return bool(hwnd and user.PostMessageW(hwnd,QUIT_MESSAGE,0,0))


def quit_owned(data_dir,desktop=None,grace=25):
    if sys.platform!='win32':raise ValueError('Windows-Beenden-Helfer ist hier nicht verfügbar.')
    import time,logging
    owner=owned_backend(data_dir);roots=([owner['backend'],*owner.get('extra',[])] if owner else [])
    excluded={os.getpid(),os.getppid()}
    captured={p['pid']:p for root in roots for p in process_family(root) if p['pid'] not in excluded}
    ui={p['pid']:p for p in process_family(desktop) if p['pid'] not in excluded} if desktop else {}
    if owner:request_backend_quit(owner['backend'])
    deadline=time.monotonic()+grace
    while owner and process_identity(owner['backend']['pid'])==owner['backend'] and time.monotonic()<deadline:time.sleep(.2)
    # Refresh living families to include cleanup children started during the grace period.
    for root in roots:
        captured.update({p['pid']:p for p in process_family(root) if p['pid'] not in excluded})
    forced=[]
    for identity in captured.values():
        if terminate_identity(identity):forced.append(identity['pid'])
    if desktop:
        (Path(data_dir)/'desktop-stop.signal').touch()
        deadline=time.monotonic()+3
        while process_identity(desktop['pid'])==desktop and time.monotonic()<deadline:time.sleep(.2)
        ui.update({p['pid']:p for p in process_family(desktop) if p['pid'] not in excluded})
        for identity in ui.values():
            if terminate_identity(identity):forced.append(identity['pid'])
    logging.getLogger(__name__).warning('Vollständig beendet; Notfall-PIDs=%s',forced)
    return forced


def launch_quit(data_dir):
    """Independent helper still works with a closed HTTP port or stuck UI thread."""
    if sys.platform!='win32':raise ValueError('Windows-Beenden-Helfer ist hier nicht verfügbar.')
    import json
    identity=process_identity(os.getpid())
    if not identity:raise OSError('Eigener StudyCenter-Prozess konnte nicht sicher zugeordnet werden.')
    environment=os.environ.copy();environment['STUDYCENTER_ICE_DATA_DIR']=str(Path(data_dir).resolve())
    return subprocess.Popen([str(Path(sys.executable).with_name('pythonw.exe')),'-m','studycenter_ice.runtime',
                             '--quit-desktop',json.dumps(identity)],cwd=ROOT,env=environment,
                             creationflags=subprocess.CREATE_NO_WINDOW,stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)


def end_session_message(message,confirmed,stop,finished):
    if message==QUIT_MESSAGE:stop();return 0
    if message==0x11:return 1  # WM_QUERYENDSESSION: never veto Windows shutdown.
    if message==0x16:  # WM_ENDSESSION: an aborted shutdown must not stop the app.
        if confirmed:
            stop()
            finished.wait(4)  # Windows imposes its own deadline; durable receipts recover later.
        return 0
    return None


@contextmanager
def windows_shutdown_listener(stop):
    """One hidden top-level window receives shutdown even for pythonw/headless."""
    if sys.platform!='win32':yield False;return
    import ctypes,logging,threading
    from ctypes import wintypes as w
    ready=threading.Event();finished=threading.Event();state={}
    def listen():
        user=ctypes.WinDLL('user32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        proc_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,w.HWND,w.UINT,w.WPARAM,w.LPARAM)
        class WindowClass(ctypes.Structure):
            _fields_=[('style',w.UINT),('proc',proc_type),('class_extra',ctypes.c_int),('window_extra',ctypes.c_int),
                      ('instance',w.HINSTANCE),('icon',w.HICON),('cursor',w.HANDLE),('background',w.HBRUSH),('menu',w.LPCWSTR),('name',w.LPCWSTR)]
        signatures={
            'RegisterClassW':([ctypes.POINTER(WindowClass)],w.WORD),
            'UnregisterClassW':([w.LPCWSTR,w.HINSTANCE],w.BOOL),
            'CreateWindowExW':([w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.HWND,w.HMENU,w.HINSTANCE,w.LPVOID],w.HWND),
            'DefWindowProcW':([w.HWND,w.UINT,w.WPARAM,w.LPARAM],ctypes.c_ssize_t),
            'DestroyWindow':([w.HWND],w.BOOL),'PostQuitMessage':([ctypes.c_int],None),
            'PostMessageW':([w.HWND,w.UINT,w.WPARAM,w.LPARAM],w.BOOL),
            'GetMessageW':([ctypes.POINTER(w.MSG),w.HWND,w.UINT,w.UINT],w.BOOL),
            'TranslateMessage':([ctypes.POINTER(w.MSG)],w.BOOL),'DispatchMessageW':([ctypes.POINTER(w.MSG)],ctypes.c_ssize_t)}
        for name,(args,result) in signatures.items():
            fn=getattr(user,name);fn.argtypes=args;fn.restype=result
        kernel.GetModuleHandleW.argtypes=[w.LPCWSTR];kernel.GetModuleHandleW.restype=w.HMODULE
        instance=kernel.GetModuleHandleW(None);name='StudyCenterPublicICEShutdown'+str(os.getpid());registered=False
        @proc_type
        def procedure(hwnd,message,wp,lp):
            try:
                result=end_session_message(message,bool(wp),stop,finished)
                if result is not None:return result
                if message==0x10:
                    if not finished.is_set():stop()
                    user.DestroyWindow(hwnd);return 0
                if message==2:user.PostQuitMessage(0);return 0
            except Exception:logging.getLogger(__name__).exception('Windows-Beenden konnte nicht signalisiert werden.')
            return user.DefWindowProcW(hwnd,message,wp,lp)
        try:
            registered=bool(user.RegisterClassW(ctypes.byref(WindowClass(0,procedure,0,0,instance,None,None,None,None,name))))
            if not registered:raise ctypes.WinError(ctypes.get_last_error())
            # Not HWND_MESSAGE: message-only windows never receive shutdown broadcasts.
            hwnd=user.CreateWindowExW(0x80,name,'',0,0,0,0,0,None,None,instance,None)
            if not hwnd:raise ctypes.WinError(ctypes.get_last_error())
            state.update(hwnd=hwnd,user=user);ready.set();msg=w.MSG()
            while user.GetMessageW(ctypes.byref(msg),None,0,0)>0:
                user.TranslateMessage(ctypes.byref(msg));user.DispatchMessageW(ctypes.byref(msg))
        except Exception:logging.getLogger(__name__).exception('Windows-Shutdown-Empfänger nicht verfügbar.')
        finally:
            ready.set()
            if registered:user.UnregisterClassW(name,instance)
    def guarded():
        try:listen()
        except Exception:logging.getLogger(__name__).exception('Windows-Shutdown-Empfänger konnte nicht gestartet werden.')
        finally:ready.set()
    thread=threading.Thread(target=guarded,name='StudyCenter shutdown listener',daemon=True);thread.start();ready.wait(3)
    try:yield bool(state.get('hwnd'))
    finally:
        finished.set()
        if state.get('hwnd'):state['user'].PostMessageW(state['hwnd'],0x10,0,0)
        thread.join(timeout=2)


def shortcut_path():return Path(os.environ.get('APPDATA',Path.home()))/'Microsoft/Windows/Start Menu/Programs/Startup/StudyCenter-Public-ICE.lnk'


def autostart_enabled():
    return sys.platform=='win32' and shortcut_path().exists()


def register_shortcut(path):
    """Give the shell link the same identity as the tray app, then notify Explorer."""
    if sys.platform!='win32':raise ValueError('Windows-Startmenü ist hier nicht verfügbar.')
    path=_own_shortcut(path)
    import ctypes
    from ctypes import wintypes
    from uuid import UUID
    class PropertyKey(ctypes.Structure):
        _fields_=[('fmtid',ctypes.c_byte*16),('pid',wintypes.DWORD)]
    class PropVariant(ctypes.Structure):
        _fields_=[('vt',ctypes.c_ushort),('reserved',ctypes.c_ushort*3),('value',ctypes.c_uint64*2)]
    ole=ctypes.WinDLL('ole32')
    shell=ctypes.WinDLL('shell32')
    def check(result):
        if result<0:raise OSError(f'Windows-Startmenüregistrierung fehlgeschlagen: {result & 0xffffffff:08x}')
    initialized=ole.CoInitializeEx(None,2)
    if initialized not in (0,1,-2147417850):check(initialized)
    store=ctypes.c_void_p()
    variant=PropVariant()
    iid=(ctypes.c_byte*16).from_buffer_copy(UUID('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99').bytes_le)
    key=PropertyKey((ctypes.c_byte*16).from_buffer_copy(UUID('9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3').bytes_le),5)
    shell.SHGetPropertyStoreFromParsingName.argtypes=[wintypes.LPCWSTR,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
    identity=ctypes.create_unicode_buffer('StudyCenter-Public-ICE.StudyCenter')
    variant.vt=31  # VT_LPWSTR; SetValue copies the string into the property store.
    variant.value[0]=ctypes.cast(identity,ctypes.c_void_p).value
    try:
        check(shell.SHGetPropertyStoreFromParsingName(str(path),None,2,ctypes.byref(iid),ctypes.byref(store)))
        table=ctypes.cast(store,ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        set_value=ctypes.WINFUNCTYPE(ctypes.c_long,ctypes.c_void_p,ctypes.POINTER(PropertyKey),ctypes.POINTER(PropVariant))(table[6])
        commit=ctypes.WINFUNCTYPE(ctypes.c_long,ctypes.c_void_p)(table[7])
        check(set_value(store,ctypes.byref(key),ctypes.byref(variant)))
        check(commit(store))
    finally:
        if store:
            table=ctypes.cast(store,ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_ulong,ctypes.c_void_p)(table[2])(store)
        if initialized in (0,1):ole.CoUninitialize()
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetFileAttributesW.argtypes=[wintypes.LPCWSTR]
    kernel.GetFileAttributesW.restype=wintypes.DWORD
    kernel.SetFileAttributesW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD]
    attributes=kernel.GetFileAttributesW(str(path))
    if attributes==0xffffffff or not kernel.SetFileAttributesW(str(path),attributes & ~0x2000):
        raise ctypes.WinError(ctypes.get_last_error())
    shell.SHChangeNotify.argtypes=[ctypes.c_long,ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p]
    shell.SHChangeNotify(0x00000002,0x1005,ctypes.cast(ctypes.c_wchar_p(str(path)),ctypes.c_void_p),None)
    shell.SHChangeNotify(0x00001000,0x1005,ctypes.cast(ctypes.c_wchar_p(str(path.parent)),ctypes.c_void_p),None)


def create_shortcut(path, silent=False, data_dir=None):
    if sys.platform!='win32':raise ValueError('Windows-Startmenü ist hier nicht verfügbar.')
    path=_own_shortcut(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    executable=ROOT/'.local/desktop/StudyCenter-Public-ICE.exe'
    python=Path(sys.executable).with_name('pythonw.exe')
    target=executable if executable.exists() else python
    from .config import default_data_dir
    selected=Path(data_dir).resolve() if data_dir is not None else default_data_dir()
    arguments=subprocess.list2cmdline(([str('--data-dir'),str(selected)]+(['--silent'] if silent else []))
                 if executable.exists() else ['-m','studycenter_ice.desktop','--data-dir',str(selected)]+(['--silent'] if silent else []))
    def quote(value):return "'"+str(value).replace("'","''")+"'"
    script=f"$shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut({quote(path)});$shortcut.TargetPath={quote(target)};$shortcut.Arguments={quote(arguments)};$shortcut.WorkingDirectory={quote(ROOT)};$shortcut.IconLocation={quote(ROOT/'studycenter_ice/static/studycenter.ico')};$shortcut.WindowStyle=7;$shortcut.Save()"
    command=base64.b64encode(script.encode('utf-16le')).decode()
    subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',command],check=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW,capture_output=True)
    register_shortcut(path)


def install_desktop(data_dir=None):
    if sys.platform!='win32':raise ValueError('Windows-Startmenü ist nur unter Windows verfügbar.')
    import winreg
    enabled=autostart_enabled()
    path=shortcut_path().parent.parent/'StudyCenter-Public-ICE.lnk'
    create_shortcut(path,data_dir=data_dir)
    # The desktop may be redirected to OneDrive; do not assume ~/Desktop.
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
        desktop=Path(os.path.expandvars(winreg.QueryValueEx(key,'Desktop')[0]))
    create_shortcut(desktop/'StudyCenter-Public-ICE.lnk',data_dir=data_dir)
    if enabled:set_autostart(True,data_dir=data_dir)
    return str(path)


def set_autostart(enabled,data_dir=None):
    if sys.platform!='win32':raise ValueError('Windows-Autostart ist nur unter Windows verfügbar.')
    path=shortcut_path()
    if not enabled:
        path.unlink(missing_ok=True);return
    python=Path(sys.executable).with_name('pythonw.exe')
    if not python.exists():raise ValueError('pythonw.exe fehlt; manuellen Start verwenden.')
    create_shortcut(path,silent=True,data_dir=data_dir)


def launch_restart(data_dir):
    """Launch one hidden successor which waits for this owner's process to exit."""
    if sys.platform!='win32':raise ValueError('Neustart ist nur unter Windows verfügbar.')
    python=Path(sys.executable).with_name('pythonw.exe')
    if not python.exists():raise OSError('pythonw.exe fehlt; StudyCenter-Public-ICE bleibt geöffnet.')
    environment=os.environ.copy()
    environment['STUDYCENTER_ICE_DATA_DIR']=str(Path(data_dir).resolve())
    child=subprocess.Popen([str(python),'-m','studycenter_ice','--after-process',str(os.getpid())],
                           cwd=ROOT,env=environment,creationflags=subprocess.CREATE_NO_WINDOW,
                           stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
    return child.pid


def wait_for_process_exit(pid,timeout=60):
    """Wait on a Windows handle, never terminate a process or delete its lock."""
    if sys.platform!='win32':raise ValueError('Neustart ist nur unter Windows verfügbar.')
    if not isinstance(pid,int) or pid<=0 or pid==os.getpid():raise ValueError('Ungültige Vorgänger-Prozess-ID.')
    if not 0<timeout<=60:raise ValueError('Wartezeit muss zwischen 0 und 60 Sekunden liegen.')
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x00100000,False,pid)  # SYNCHRONIZE only.
    if not handle:
        error=ctypes.get_last_error()
        if error==87:return  # The parent already exited before the child opened it.
        raise ctypes.WinError(error)
    try:
        result=kernel.WaitForSingleObject(handle,int(timeout*1000))
        if result==258:raise TimeoutError('Vorherige StudyCenter-Public-ICE-Instanz läuft nach 60 Sekunden weiterhin; kein Doppelstart.')
        if result!=0:raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


if __name__=='__main__':
    import argparse,json,logging
    from logging.handlers import RotatingFileHandler
    from .config import load_settings
    parser=argparse.ArgumentParser();parser.add_argument('--quit-desktop',required=True)
    arguments=parser.parse_args();settings=load_settings()
    directory=settings.data_dir/'logs';directory.mkdir(exist_ok=True)
    logging.getLogger(__name__).addHandler(RotatingFileHandler(directory/'backend.log',maxBytes=256*1024,backupCount=2,encoding='utf8'))
    quit_owned(settings.data_dir,json.loads(arguments.quit_desktop))
