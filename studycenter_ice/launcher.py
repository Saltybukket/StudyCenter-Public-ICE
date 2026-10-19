"""Start/stop only this independently owned ICE application."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import webbrowser

import httpx

from .config import ROOT, Settings

BASE = 'http://127.0.0.1:18791'
APPLICATION = 'StudyCenter.Public.ICE'


def owned_status(data_dir):
    try:
        response = httpx.get(BASE + '/api/desktop/status', timeout=2)
        response.raise_for_status()
        value = response.json()
        dataset = hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()[:16]
        return value if value.get('application') == APPLICATION and value.get('dataset_path') == dataset else None
    except (httpx.HTTPError, ValueError):
        return None


def configure_components():
    """Expose only ICE's own portable components to this process."""
    root = Path(os.environ.get('STUDYCENTER_ICE_INSTALL_ROOT', ROOT))
    components = root / '.local/components'
    os.environ['STUDYCENTER_ICE_COMPONENT_ROOT'] = str(components)
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(components / 'browsers')
    ocr = components / 'ocr'
    if os.name == 'nt':
        command = ocr / 'tesseract.exe'
    else:
        command = ocr / 'usr/bin/tesseract'
        libraries = ocr / 'usr/lib/x86_64-linux-gnu'
        if libraries.is_dir():
            os.environ['LD_LIBRARY_PATH'] = str(libraries)
    if command.is_file():
        os.environ['STUDYCENTER_ICE_TESSERACT_CMD'] = str(command)
        tessdata = ocr / ('tessdata' if os.name == 'nt' else 'usr/share/tesseract-ocr/5/tessdata')
        if tessdata.is_dir():
            os.environ['TESSDATA_PREFIX'] = str(tessdata)


def main(argv=None):
    parser = argparse.ArgumentParser(description='StudyCenter Public ICE')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--quit', '--stop', action='store_true')
    parser.add_argument('--no-browser', '--headless', '--background', action='store_true')
    parser.add_argument('--browser', action='store_true', help='Use the normal system browser on Windows as well')
    args = parser.parse_args(argv)
    configure_components()
    if args.data_dir:
        os.environ['STUDYCENTER_ICE_DATA_DIR'] = str(args.data_dir.absolute())
    # --stop must not create an empty data directory or download components.
    data_dir = args.data_dir or Path(os.environ.get('STUDYCENTER_ICE_DATA_DIR', ROOT / '.local/data'))
    if args.quit:
        if not owned_status(data_dir):
            print('No matching ICE instance is running. Other applications remain untouched.')
            return 0
        with httpx.Client(timeout=15) as client:
            page = client.get(BASE + '/settings')
            page.raise_for_status()
            import re
            token = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
            if not token:
                raise RuntimeError('ICE form token is unavailable; no process was stopped.')
            response = client.post(BASE + '/settings/stop', data={'confirm': 'yes'}, headers={'X-CSRF-Token': token[1]})
            response.raise_for_status()
        print('ICE shutdown requested.')
        return 0
    os.environ['STUDYCENTER_ICE_DATA_DIR'] = str(data_dir.absolute())
    if os.name == 'nt' and not args.browser and not args.no_browser:
        from .desktop import main as desktop_main
        return desktop_main(['--window'])
    if not owned_status(data_dir):
        # Bind errors do not authorize stopping/replacing an unrelated listener.
        import socket
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', 18791)) == 0:
                raise RuntimeError('ICE port 18791 is occupied by another instance/application. It was left unchanged.')
        settings = Settings(data_dir=data_dir)
        logs = settings.data_dir / 'logs'
        logs.mkdir(exist_ok=True)
        with (logs / 'launcher.log').open('ab') as output:
            child = subprocess.Popen([sys.executable, '-I', '-m', 'studycenter_ice'], cwd=ROOT,
                env=os.environ.copy(), stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}))
        deadline = time.monotonic() + 30
        while not owned_status(data_dir):
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('ICE did not become ready; see its own logs/launcher.log. Retry the same starter.')
            time.sleep(.2)
    if not args.no_browser:
        if not webbrowser.open(BASE + '/overview'):
            print('Open ' + BASE + '/overview in your browser.')
    else:
        print('ICE ready at ' + BASE)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
