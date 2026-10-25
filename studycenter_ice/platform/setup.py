"""Shared, stdlib-only uv setup for a clone and the slim release bootstrap.

This file can be shipped alone: importing it never imports the application or
installs anything. All mutable setup state belongs to the selected Public root.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
from uuid import uuid4
import zipfile

APPLICATION = 'StudyCenter.Public.ICE'
PYTHON = '3.12'


def regular_directory(path):
    path = Path(path).absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (candidate.exists() and
                getattr(candidate.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError(f'Refusing linked/reparse setup path: {candidate}')
    if path.exists() and not path.is_dir():
        raise ValueError(f'Setup needs a directory: {path}')
    return path


def checked_file(path):
    path = Path(path).absolute()
    regular_directory(path.parent)
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
        raise ValueError(f'Refusing linked/reparse setup file: {path}')
    return path


def atomic_json(path, data):
    path = checked_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temporary.replace(path)


def read_receipt(root):
    path = checked_file(Path(root) / '.local/installation.json')
    if not path.exists():
        return {}
    receipt = json.loads(path.read_text(encoding='utf-8'))
    if receipt.get('application') != APPLICATION:
        raise ValueError('The setup receipt belongs to another application.')
    environment = Path(receipt['environment']).absolute()
    regular_directory(environment)
    if not environment.is_relative_to(Path(root).absolute()):
        raise ValueError('The environment is outside this Public installation.')
    expected = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if Path(receipt['python']).absolute() != expected:
        raise ValueError('The setup receipt does not name its own environment interpreter.')
    return receipt


def environment_python(root):
    receipt = read_receipt(root)
    if receipt:
        return Path(receipt['python'])
    return Path(root) / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def compatible_python(python):
    try:
        result = subprocess.run([str(python), '-I', '-c',
            'import json,platform,struct,sys; print(json.dumps([platform.python_implementation(),list(sys.version_info[:2]),struct.calcsize("P")*8,platform.machine()]))'],
            capture_output=True, text=True, timeout=15, check=True)
        implementation, version, bits, machine = json.loads(result.stdout)
        return implementation == 'CPython' and version == [3, 12] and bits == 64 and machine.lower() in ('amd64', 'x86_64')
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def environment_usable(root, python):
    own = regular_directory(Path(python).parent.parent)
    # Never accept a global interpreter or an environment outside this install.
    if not own.is_relative_to(Path(root).absolute()) or not Path(python).is_file():
        return False
    try:
        config = checked_file(own / 'pyvenv.cfg').read_text(encoding='utf-8').lower()
        if 'include-system-site-packages = false' not in config:
            return False
        result = subprocess.run([str(python), '-I', '-c',
            'import json,sys; print(json.dumps([sys.prefix,sys.base_prefix]))'],
            capture_output=True, text=True, timeout=15, check=True)
        prefix, base = json.loads(result.stdout)
        return Path(prefix).resolve() == own.resolve() and prefix != base and compatible_python(python)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def preserve_environment(root):
    root = regular_directory(root)
    own = regular_directory(root / '.venv')
    local = regular_directory(root / '.local')
    local.mkdir(exist_ok=True)
    previous = local / ('previous-venv-' + uuid4().hex)
    own.rename(previous)
    return previous


@contextmanager
def setup_lock(root, timeout=180):
    local = regular_directory(Path(root) / '.local')
    local.mkdir(parents=True, exist_ok=True)
    path = checked_file(local / 'setup.lock')
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0')
            handle.flush()
        start = time.monotonic()
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() - start >= timeout:
                    raise RuntimeError('Another Public setup is active. Wait and retry the same starter.')
                time.sleep(.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def uv_environment(root):
    root = regular_directory(root)
    env = os.environ.copy()
    for key in list(env):
        if (key.startswith(('UV_', 'PIP_')) and key != 'UV_OFFLINE') or key in ('VIRTUAL_ENV', 'PYTHONHOME', 'PYTHONPATH'):
            env.pop(key, None)
    for name, relative in (('UV_CACHE_DIR', 'uv-cache'), ('UV_PYTHON_INSTALL_DIR', 'python'), ('UV_PYTHON_BIN_DIR', 'bin')):
        path = regular_directory(root / '.local' / relative)
        env[name] = str(path)
    env.update(UV_PYTHON_INSTALL_BIN='0', PYTHONNOUSERSITE='1')
    return env


def run(command, **kwargs):
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def select_python(root, uv):
    env = uv_environment(root)
    try:
        result = run([uv, '--no-config', 'python', 'find', '--system', '--no-python-downloads', '--python-preference', 'only-system', PYTHON], env=env, capture_output=True, text=True)
        python = Path(result.stdout.strip())
        if compatible_python(python):
            return python, 'system'
    except (OSError, subprocess.SubprocessError):
        pass
    managed_root = regular_directory(Path(root) / '.local/python')
    if managed_root.is_dir():
        try:
            result = run([uv, '--no-config', 'python', 'find', '--managed-python', '--no-python-downloads', PYTHON], env=env, capture_output=True, text=True)
            python = Path(result.stdout.strip())
            if python.absolute().is_relative_to(managed_root) and compatible_python(python):
                return python, 'managed'
        except (OSError, subprocess.SubprocessError):
            pass
    env['UV_PYTHON_CPYTHON_BUILD'] = '20250918'
    run([uv, '--no-config', 'python', 'install', '--no-bin', '--no-registry', '3.12.11'], env=env)
    result = run([uv, '--no-config', 'python', 'find', '--managed-python', '3.12.11'], env=env, capture_output=True, text=True)
    python = Path(result.stdout.strip())
    if not compatible_python(python):
        raise RuntimeError('This release requires CPython 3.12 x64. Its private runtime could not be prepared.')
    return python, 'managed'


def sha256(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def download(root, record):
    url, digest = record['url'], record['sha256']
    if not url.startswith('https://') or not re.fullmatch('[a-f0-9]{64}', digest):
        raise ValueError('Downloads require HTTPS and a known SHA256.')
    filename = record.get('filename') or url.rsplit('/', 1)[-1]
    if not filename or Path(filename).name != filename or '/' in filename or '\\' in filename:
        raise ValueError('Invalid download filename.')
    cache = regular_directory(Path(root) / '.local/downloads' / digest)
    cache.mkdir(parents=True, exist_ok=True)
    target = checked_file(cache / filename)
    if target.is_file() and sha256(target) == digest and (record.get('size') is None or target.stat().st_size == record['size']):
        return target
    # Explicitly supplied files from this release, e.g. a downloaded asset set.
    # Read exactly the named asset, never discover or alter another tool's cache.
    supplied = os.environ.get('STUDYCENTER_ICE_RELEASE_ASSETS')
    if supplied:
        candidate = checked_file(regular_directory(supplied) / filename)
        if candidate.is_file():
            if sha256(candidate) != digest or (record.get('size') is not None and candidate.stat().st_size != record['size']):
                raise ValueError('Supplied release asset checksum or size mismatch.')
            temporary = checked_file(cache / (filename + '.' + uuid4().hex + '.tmp'))
            shutil.copyfile(candidate, temporary)
            if sha256(temporary) != digest or (record.get('size') is not None and temporary.stat().st_size != record['size']):
                temporary.unlink()
                raise ValueError('Supplied release asset changed while copying.')
            temporary.replace(target)
            return target
    temporary = checked_file(cache / (filename + '.partial'))
    transferred, success = 0, False
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            if not response.geturl().startswith('https://'):
                raise ValueError('Download redirected outside HTTPS.')
            with temporary.open('wb') as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    transferred += len(chunk)
        if sha256(temporary) != digest or (record.get('size') is not None and transferred != record['size']):
            raise ValueError('Download checksum or size mismatch; retry setup.')
        temporary.replace(target)
        success = True
        return target
    finally:
        ledger_path = checked_file(Path(root) / '.local/downloads.json')
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {'schema': 1, 'transfers': [], 'uv_download_bytes': None}
        ledger['transfers'].append({'url': url, 'bytes': transferred, 'sha256': digest, 'complete': success})
        atomic_json(ledger_path, ledger)
        if temporary.exists():
            temporary.unlink()


def extract_archive(archive, target):
    target = regular_directory(target)
    def validate(name):
        if '\\' in name or ':' in name or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
            raise ValueError('Unsafe archive path.')
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                validate(member.filename)
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError('Links in component archive are forbidden.')
            source.extractall(target)
            for member in source.infolist():
                mode = member.external_attr >> 16
                if mode and not member.is_dir():
                    (target / member.filename).chmod(mode & 0o777)
    else:
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                validate(member.name)
                if not (member.isfile() or member.isdir()):
                    raise ValueError('Links or special files in component archive are forbidden.')
            source.extractall(target, filter='data')


def load_manifest(path):
    data = json.loads(checked_file(path).read_text(encoding='utf-8'))
    expected = ('windows' if os.name == 'nt' else 'linux') + '-x86_64'
    if data.get('schema') != 1 or data.get('platform') != expected or data.get('python') != PYTHON:
        raise ValueError('This manifest does not match this release platform / CPython 3.12 x64.')
    return data


def retain_manifest(root, manifest):
    """Keep a release's exact metadata after its temporary installer exits."""
    load_manifest(manifest)
    payload = checked_file(manifest).read_bytes()
    directory = regular_directory(Path(root) / '.local/manifests')
    directory.mkdir(parents=True, exist_ok=True)
    destination = checked_file(directory / (hashlib.sha256(payload).hexdigest() + '.json'))
    if not destination.exists():
        temporary = checked_file(directory / (uuid4().hex + '.tmp'))
        temporary.write_bytes(payload)
        temporary.replace(destination)
    elif destination.read_bytes() != payload:
        raise ValueError('Saved release manifest is damaged; setup did not promote the environment.')
    return destination


def ensure_uv(root, requested=None):
    """Repair the small pinned tool only when a setup operation needs it."""
    if requested and checked_file(requested).is_file():
        return Path(requested).absolute()
    root = regular_directory(root)
    tools = regular_directory(root / '.local/tools/uv-0.8.22')
    executable = checked_file(tools / ('uv.exe' if os.name == 'nt' else 'uv'))
    if executable.is_file():
        return executable
    windows = os.name == 'nt'
    filename = 'uv-x86_64-pc-windows-msvc.zip' if windows else 'uv-x86_64-unknown-linux-gnu.tar.gz'
    digest = '5049375aa2a5162f132b2c1cb992e25d42d47d934cab8c174dbe6f60973dcc12' if windows else '741ff1f5742c5a4a25d2f829e8395355e43f7a5ae2ebc6368e9ae2df0efb69cf'
    archive = download(root, {'url': 'https://github.com/astral-sh/uv/releases/download/0.8.22/' + filename,
                              'filename': filename, 'sha256': digest})
    stage = regular_directory(root / '.local/staging' / uuid4().hex)
    stage.mkdir(parents=True)
    extract_archive(archive, stage)
    binary = stage / ('uv.exe' if windows else 'uv-x86_64-unknown-linux-gnu/uv')
    if not binary.is_file():
        raise ValueError('The verified uv archive does not contain its expected executable.')
    tools.mkdir(parents=True, exist_ok=True)
    binary.replace(executable)
    if not windows:
        executable.chmod(0o755)
    return executable


def source_digest(source):
    source = Path(source)
    lock = source / ('requirements-windows.lock' if os.name == 'nt' else 'requirements-linux.lock')
    manifest = source / 'packaging' / ('slim-windows-x86_64.json' if os.name == 'nt' else 'slim-linux-x86_64.json')
    extra_wheels = json.loads(checked_file(manifest).read_text()).get('extra_wheels', []) if manifest.is_file() else []
    return hashlib.sha256(checked_file(lock).read_bytes() +
                          checked_file(source / 'pyproject.toml').read_bytes() +
                          json.dumps(extra_wheels, sort_keys=True).encode() +
                          Path(__file__).read_bytes()).hexdigest()


def application_ready(python):
    try:
        result = subprocess.run([str(python), '-I', '-c',
            'from importlib.metadata import version; version("studycenter-public-ice"); from studycenter_ice.web import create_app'],
            capture_output=True, timeout=30)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def save_installation(root, receipt):
    atomic_json(Path(root) / '.local/installation.json', receipt)
    pointer = checked_file(Path(root) / '.local/python-path.txt')
    temporary = pointer.with_name('python-path.' + uuid4().hex + '.tmp')
    temporary.write_text(receipt['python'] + '\n', encoding='utf-8')
    temporary.replace(pointer)


def ensure_environment(root, uv, *, source=None, manifest=None, update=False):
    root = regular_directory(root)
    receipt = read_receipt(root)
    python = environment_python(root)
    source = Path(source or receipt.get('source', '')).absolute() if source or receipt.get('source') else None
    stamp = source_digest(source) if source else None
    if receipt and source and receipt.get('source_digest') != stamp:
        update = True
    if receipt and environment_usable(root, python) and not update:
        return receipt
    if receipt and not environment_usable(root, python) and not update:
        # An interrupted/obsolete setup is repaired on the same next start.
        # New environment promotion remains atomic and preserves the old one.
        update = True
    if not receipt and source and environment_usable(root, python) and application_ready(python) and not update:
        config = checked_file(python.parent.parent / 'pyvenv.cfg')
        values = dict(line.split(' = ', 1) for line in config.read_text().splitlines() if ' = ' in line) if config.exists() else {}
        base_python = values.get('executable') or values.get('home')
        private_python = root / '.local/python'
        base_origin = 'managed' if base_python and Path(base_python).absolute().is_relative_to(private_python) else 'system'
        adopted = {'schema': 1, 'application': APPLICATION, 'root': str(root),
                   'environment': str(python.parent.parent), 'python': str(python),
                   'uv': str(Path(uv).absolute()), 'source': str(source), 'manifest': None,
                   'version': 'source', 'source_digest': stamp, 'extras': [],
                   'python_origin': 'existing-own', 'base_python_origin': base_origin, 'base_python': base_python}
        save_installation(root, adopted)
        return adopted
    # A source checkout always follows its current component metadata. Release
    # receipts instead retain the exact manifest from their own setup.
    manifest = Path(manifest).absolute() if manifest else None
    if not manifest and source:
        candidate = source / 'packaging' / ('slim-windows-x86_64.json' if os.name == 'nt' else 'slim-linux-x86_64.json')
        if checked_file(candidate).is_file():
            manifest = candidate
    if not manifest and receipt.get('manifest'):
        manifest = Path(receipt['manifest']).absolute()
    release = load_manifest(manifest) if manifest else None
    if not source and not release:
        raise ValueError('Setup needs its source checkout or release manifest.')
    if source:
        regular_directory(source)
        lock = checked_file(source / ('requirements-windows.lock' if os.name == 'nt' else 'requirements-linux.lock'))
    else:
        lock = download(root, release['lock'])
    uv = ensure_uv(root, uv)
    interpreter, origin = select_python(root, uv)
    environments = regular_directory(root / '.local/environments')
    environments.mkdir(parents=True, exist_ok=True)
    environment = environments / uuid4().hex
    env = uv_environment(root)
    # Environment paths are permanent. Promotion changes a small atomic receipt,
    # never renames a venv with embedded absolute paths or alters the old one.
    run([uv, '--no-config', 'venv', '--python', interpreter, '--no-python-downloads', environment], env=env)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    common = [uv, '--no-config', 'pip', 'install', '--python', python, '--only-binary', ':all:', '--constraint', lock.absolute().as_uri()]
    for record in (release or {}).get('extra_wheels', []):
        run([*common, download(root, record)], env=env)
    if source:
        # The application's editable build is the only source build exception.
        run([*common, 'setuptools'], env=env)
        run([*common, '--no-build-isolation', '--editable', source], env=env)
    else:
        wheel = download(root, release['wheel'])
        run([*common, wheel], env=env)
    extras = receipt.get('extras', [])
    if extras:
        target = f'{source}[{",".join(extras)}]' if source else f'{wheel}[{",".join(extras)}]'
        extra_flags = ['--no-build-isolation', '--editable'] if source else []
        run([*common, *extra_flags, target], env=env)
    run([uv, '--no-config', 'pip', 'check', '--python', python], env=env)
    run([python, '-I', '-c', 'import studycenter_ice; from studycenter_ice.web import create_app'], env=env)
    if not environment_usable(root, python):
        raise RuntimeError('The prepared private environment is invalid; the previous installation is retained.')
    if manifest and not source:
        manifest = retain_manifest(root, manifest)
    new = {'schema': 1, 'application': APPLICATION, 'root': str(root), 'environment': str(environment),
           'python': str(python), 'uv': str(Path(uv).absolute()), 'source': str(source) if source else None,
           'manifest': str(manifest) if manifest else None, 'python_origin': origin, 'base_python': str(interpreter),
           'version': '0.1.0b1', 'extras': extras,
           'source_digest': stamp, 'lock_sha256': sha256(lock), 'data_dir': receipt.get('data_dir')}
    save_installation(root, new)
    return new


def verified_system_browser(python):
    """Reuse only the application's narrowly verified existing Linux Chrome."""
    if sys.platform != 'linux':
        return None
    try:
        result = subprocess.run([str(python), '-I', '-c',
            'from studycenter_ice.browser import linux_system_chrome; print(linux_system_chrome() or "")'],
            capture_output=True, text=True, timeout=15, check=True)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def component_valid(root, name):
    base = regular_directory(Path(root) / '.local/components')
    path = checked_file(base / (name + '.json'))
    if not path.is_file():
        return False
    try:
        receipt = json.loads(path.read_text())
        if receipt.get('name') != name:
            return False
        if not receipt.get('files') and name != 'speech':
            if name != 'browser' or not receipt.get('system_browser'):
                return False
            install = read_receipt(root)
            if not verified_system_browser(install['python']):
                return False
        for relative, record in receipt.get('files', {}).items():
            path = checked_file(base / relative)
            if not path.is_relative_to(base) or not path.is_file() or path.stat().st_size != record['size'] or sha256(path) != record['sha256']:
                return False
        if name in ('browser', 'speech'):
            install = read_receipt(root)
            modules = ['playwright.async_api'] if name == 'browser' else ['faster_whisper', 'av']
            result = subprocess.run([install['python'], '-I', '-c',
                                    'import importlib; ' + '; '.join('importlib.import_module('+repr(module)+')' for module in modules)], capture_output=True)
            if result.returncode:
                return False
        return True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def ensure_component(root, name, *, uv=None, source=None, manifest=None, data_dir=None, allow_download=False):
    if name not in ('browser', 'ocr', 'speech'):
        raise ValueError('Unknown optional component.')
    root = regular_directory(root)
    with setup_lock(root):
        if component_valid(root, name):
            return json.loads((root / '.local/components' / (name + '.json')).read_text())
        if not allow_download:
            raise RuntimeError('Confirm optional component setup before downloading.')
        install = read_receipt(root)
        uv = uv or install.get('uv')
        source = Path(source or install['source']) if source or install.get('source') else None
        manifest = manifest or install.get('manifest')
        if not manifest and source:
            manifest = source / 'packaging' / (('slim-windows' if os.name == 'nt' else 'slim-linux') + '-x86_64.json')
        if not manifest:
            raise RuntimeError('The release component manifest is missing; repair this Public installation.')
        release = load_manifest(manifest)
        definition = release.get('components', {}).get(name)
        if not definition:
            raise RuntimeError('This component is unavailable for this platform.')
        base = regular_directory(root / '.local/components')
        base.mkdir(parents=True, exist_ok=True)
        ledger = checked_file(root / '.local/downloads.json')
        before = sum(row['bytes'] for row in json.loads(ledger.read_text()).get('transfers', [])) if ledger.exists() else 0
        files = {}
        if definition.get('extra'):
            uv = ensure_uv(root, uv)
            install['uv'] = str(uv)
            lock = source / ('requirements-windows.lock' if os.name == 'nt' else 'requirements-linux.lock') if source else download(root, release['lock'])
            target = str(source) if source else str(download(root, release['wheel']))
            common = [uv, '--no-config', 'pip', 'install', '--python', install['python'], '--only-binary', ':all:', '--constraint', lock.absolute().as_uri()]
            if (base / (name + '.json')).is_file():
                for package in (['playwright'] if name == 'browser' else ['faster-whisper', 'av']):
                    common += ['--reinstall-package', package]
            flags = ['--no-build-isolation', '--editable'] if source else []
            run([*common, *flags, target + '[' + definition['extra'] + ']'], env=uv_environment(root))
            install['extras'] = sorted(set(install.get('extras', [])) | {definition['extra']})
            atomic_json(root / '.local/installation.json', install)
        system_browser = name == 'browser' and bool(verified_system_browser(install['python']))
        if definition.get('archive') and not system_browser:
            archive = download(root, definition['archive'])
            stage = regular_directory(root / '.local/staging' / uuid4().hex)
            stage.mkdir(parents=True)
            extract_archive(archive, stage)
            runtime = stage / 'runtime'
            if not runtime.is_dir():
                raise ValueError('Component archive must contain runtime/.')
            directory = 'browsers' if name == 'browser' else name
            target = regular_directory(base / directory)
            previous = None
            if target.exists():
                previous = base / (directory + '.previous-' + uuid4().hex)
                target.rename(previous)
            try:
                runtime.rename(target)
            except OSError:
                if previous:
                    previous.rename(target)
                raise
            for path in target.rglob('*'):
                if path.is_file():
                    files[path.relative_to(base).as_posix()] = {'size': path.stat().st_size, 'sha256': sha256(path)}
            # Keep all bundled license, source and manifest material alongside
            # the runtime, independent of the component builder's naming.
            remaining = list(stage.iterdir())
            if remaining:
                notices = base / (name + '-notices-' + uuid4().hex)
                notices.mkdir()
                for path in remaining:
                    path.rename(notices / path.name)
        after = sum(row['bytes'] for row in json.loads(ledger.read_text()).get('transfers', [])) if ledger.exists() else before
        result = {'schema': 1, 'name': name, 'files': files,
                  'system_browser': system_browser,
                  'archive_sha256': definition.get('archive', {}).get('sha256'),
                  'archive_size': definition.get('archive', {}).get('size'),
                  'downloaded_bytes': after - before, 'uv_download_bytes': None}
        atomic_json(base / (name + '.json'), result)
        if not component_valid(root, name):
            raise RuntimeError('Optional component did not pass verification. Retry its setup.')
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare or start this private StudyCenter Public ICE installation.')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--uv', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--setup-only', action='store_true')
    parser.add_argument('--update', action='store_true', help='Explicitly repair/update the private environment; preserve user data')
    parser.add_argument('--component', choices=['browser', 'ocr', 'speech'])
    parser.add_argument('--allow-download', action='store_true')
    parser.add_argument('--data-dir')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--lock-check', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--prepare-target', type=Path, help=argparse.SUPPRESS)
    args, application_args = parser.parse_known_args(argv)
    root = regular_directory(args.root)
    stopping = args.stop or '--quit' in application_args
    if stopping and not environment_usable(root, environment_python(root)):
        print('No prepared Public environment is running from this installation.')
        return 0
    if args.lock_check:
        with setup_lock(root, timeout=.2):
            return 0
    if not args.uv and not stopping:
        parser.error('--uv is required for setup')
    with setup_lock(root):
        install = read_receipt(root) if stopping else ensure_environment(root, args.uv, source=args.source, manifest=args.manifest, update=args.update)
        if stopping and not install:
            install = {'python': str(environment_python(root))}
        chosen_data = args.data_dir or os.environ.get('STUDYCENTER_ICE_DATA_DIR') or install.get('data_dir')
        if not chosen_data and (args.source or install.get('source')):
            chosen_data = str(root / '.local/data')
        if chosen_data and chosen_data != install.get('data_dir') and not stopping:
            install['data_dir'] = str(Path(chosen_data).absolute())
            save_installation(root, install)
        args.data_dir = chosen_data
    if args.component and not stopping:
        ensure_component(root, args.component, uv=args.uv, source=args.source, manifest=args.manifest, data_dir=args.data_dir, allow_download=args.allow_download)
    if args.setup_only:
        print('Ready. Start StudyCenter Public ICE from the same starter.')
        return 0
    env = os.environ.copy()
    env.update(STUDYCENTER_ICE_INSTALL_ROOT=str(root), STUDYCENTER_ICE_COMPONENT_ROOT=str(root / '.local/components'))
    app = ['--quit'] if stopping else application_args
    if args.data_dir:
        app = ['--data-dir', args.data_dir, *app]
    return subprocess.call([install['python'], '-I', '-m', 'studycenter_ice.launcher', *app], cwd=root, env=env)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        print(f'Public setup failed: {error}\nRetry the same starter. Existing user data and the previous environment are retained.', file=sys.stderr)
        raise SystemExit(1)
