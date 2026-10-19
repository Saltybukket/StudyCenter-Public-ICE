"""Local, credential-free snapshots; restores run only during exclusive startup."""
import hashlib
import json
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

FORMAT = 1
APP_VERSION = '0.1.0'
MAX_ARCHIVE = 2 * 1024**3
TABLES = {'courses', 'sources', 'documents', 'items', 'item_evidence', 'sync_runs'}
OPTIONAL_TABLES = {'assistant_requests','course_aliases','personal_reminders','app_preferences','item_preferences','reminder_jobs','notification_reads','google_events'}  # Older format-1 snapshots remain restorable.
SECRET = re.compile(r'password|passwd|secret|token|cookie|credential|authorization|api[_-]?key', re.I)


def digest(path):
    with Path(path).open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def snapshot(source, target):
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        deadline = time.monotonic() + 60
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError('Datenbank ist zu lange belegt. Verbindung schließen und erneut versuchen.')
        src.backup(dst, pages=256, progress=progress)


def create_backup(db, data_dir):
    directory = Path(data_dir) / 'backups'
    directory.mkdir(exist_ok=True)
    target = directory / (datetime.now().strftime('StudyCenter-ICE-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.zip')
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        database = Path(temporary) / 'studycenter_ice.sqlite3'
        snapshot(db.path, database)
        originals = {}
        with closing(sqlite3.connect(database)) as con:
            for doc_id, original, expected in con.execute('SELECT id,original_path,hash FROM documents').fetchall():
                path = Path(original).resolve()
                if not path.is_relative_to((Path(data_dir) / 'documents').resolve()) or not path.is_file():
                    raise ValueError(f'Dokumentoriginal {doc_id} fehlt oder liegt außerhalb der Dokumentablage.')
                if digest(path) != expected:
                    raise ValueError(f'Dokumentoriginal {doc_id} stimmt nicht mit seiner gespeicherten Version überein.')
                name = 'documents/' + expected + path.suffix
                originals[name] = path
                con.execute('UPDATE documents SET original_path=? WHERE id=?', (name, doc_id))
            # Credentials are stored separately. Fail closed if a future adapter puts them in configuration.
            for config, url in con.execute('SELECT config,url FROM sources'):
                def has_secret(value):
                    if isinstance(value, dict):
                        return any(SECRET.search(k) or has_secret(v) for k, v in value.items())
                    if isinstance(value, list):
                        return any(has_secret(v) for v in value)
                    return isinstance(value, str) and bool(re.search(r'(?:[?&](?:token|password|api_key)=|://[^/]+:[^/]+@)', value, re.I))
                if has_secret(json.loads(config)) or has_secret(url):
                    raise ValueError('Zugangsdaten in einer Quellenkonfiguration: Sicherung abgebrochen.')
            tables = TABLES | (OPTIONAL_TABLES & {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")})
            counts = {table: con.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables}
            con.commit()
        files = {'studycenter_ice.sqlite3': database, **originals}
        if len(files) > 19999 or sum(p.stat().st_size for p in files.values()) > MAX_ARCHIVE - 4 * 1024**2:
            raise ValueError('Backup überschreitet die unterstützte Größe von 2 GiB.')
        manifest = {'format': 'StudyCenter-ICE', 'version': FORMAT, 'app_version': APP_VERSION,
                    'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'counts': counts,
                    'files': {name: {'size': path.stat().st_size, 'sha256': digest(path)} for name, path in files.items()},
                    'excluded': ['OAuth / TU sessions', 'browser profiles', 'passwords', 'local settings / credentials', 'repository caches']}
        try:
            with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
                for name, path in files.items():
                    archive.write(path, name)
        except Exception:
            target.unlink(missing_ok=True)
            raise
    return target


def inspect_archive(path, destination):
    """Validate every member before use; never trust paths or SQL objects from an upload."""
    destination = Path(destination)
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if len(names) != len(set(names)) or len(names) > 20000 or sum(m.file_size for m in members) > MAX_ARCHIVE:
            raise ValueError('Doppelte Dateien oder zu großes Backup.')
        for member in members:
            name = member.filename
            if ('\\' in name or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                    or member.is_dir() or stat.S_ISLNK(member.external_attr >> 16)
                    or not (name in ('manifest.json', 'studycenter_ice.sqlite3') or re.fullmatch(r'documents/[a-f0-9]{64}\.[a-z0-9]+', name))):
                raise ValueError('Nicht erlaubter Archivpfad.')
        if 'manifest.json' not in names or archive.getinfo('manifest.json').file_size > 4 * 1024**2:
            raise ValueError('Manifest fehlt oder ist zu groß.')
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('format') != 'StudyCenter-ICE' or manifest.get('version') != FORMAT or manifest.get('schema_version') != 1:
            raise ValueError('Nicht unterstützte Backup-Version.')
        if set(manifest.get('files', {})) != set(names) - {'manifest.json'} or 'studycenter_ice.sqlite3' not in names:
            raise ValueError('Manifest und Archivinhalt stimmen nicht überein.')
        destination.mkdir(parents=True, exist_ok=True)
        for name, info in manifest['files'].items():
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as src, output.open('wb') as dst:
                shutil.copyfileobj(src, dst)
            if output.stat().st_size != info['size'] or digest(output) != info['sha256']:
                raise ValueError('Dateiprüfsumme stimmt nicht: ' + name)
    database = destination / 'studycenter_ice.sqlite3'
    with closing(sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)) as con:
        objects = con.execute("SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        tables = {name for name, kind, _ in objects if kind == 'table'}
        if (not TABLES <= tables or not tables <= TABLES | OPTIONAL_TABLES
                or any(kind not in ('table', 'index') or 'VIRTUAL' in (sql or '').upper() for _, kind, sql in objects)
                or con.execute('PRAGMA user_version').fetchone()[0] != 1):
            raise ValueError('Nicht unterstützte Datenbankstruktur.')
        # Exact current columns also detect truncated/foreign databases before touching the original.
        from .db import Database
        with tempfile.TemporaryDirectory() as temporary:
            reference = Database(Path(temporary) / 'reference.sqlite3')
            for table in tables:
                actual = [tuple(row[1:6]) for row in con.execute(f'PRAGMA table_info({table})')]
                with reference.connection() as ref:
                    expected = [tuple(row[1:6]) for row in ref.execute(f'PRAGMA table_info({table})')]
                if actual != expected:
                    raise ValueError('Nicht unterstützte Tabellenspalten: ' + table)
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or con.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Datenbankprüfung fehlgeschlagen.')
        for original, expected in con.execute('SELECT original_path,hash FROM documents'):
            if original not in manifest['files'] or not original.startswith('documents/') or digest(destination / original) != expected:
                raise ValueError('Dokumentversion fehlt oder ist beschädigt.')
        counts = {table: con.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables}
        if counts != manifest.get('counts'):
            raise ValueError('Datensatzanzahl stimmt nicht mit Manifest überein.')
    return manifest


def apply_pending(db, data_dir):
    """Called after owner-lock acquisition, before serving requests or starting work."""
    root = Path(data_dir)
    pending = root / 'restore-pending.zip'
    if not pending.exists():
        return
    rollback = None
    try:
        with tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True) as temporary:
            staged = Path(temporary)
            manifest = inspect_archive(pending, staged)
            rollback = create_backup(db, root)
            restored = root / 'documents' / ('restored-' + uuid.uuid4().hex)
            restored.mkdir()
            with closing(sqlite3.connect(staged / 'studycenter_ice.sqlite3')) as con:
                for name in manifest['files']:
                    if name.startswith('documents/'):
                        target = restored / Path(name).name
                        shutil.copy2(staged / name, target)
                        con.execute('UPDATE documents SET original_path=? WHERE original_path=?', (str(target), name))
                con.commit()
            # SQLite's backup transaction preserves the current DB on failure; no WAL files are deleted.
            snapshot(staged / 'studycenter_ice.sqlite3', db.path)
        message = f'Backup wiederhergestellt. Rückfallsicherung: {rollback.name}. Lokale Anmeldungen unverändert.'
    except Exception as error:
        message = f'Wiederherstellung fehlgeschlagen ({type(error).__name__}): {error}. Aktueller Bestand bleibt erhalten.'
    pending.rename(root / ('restore-processed-' + uuid.uuid4().hex + '.zip'))
    (root / 'restore-result.json').write_text(json.dumps({'message': message, 'rollback': rollback.name if rollback else None}), encoding='utf-8')
