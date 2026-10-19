"""Read configured GitLab remotes into owned bare caches; never touch worktrees."""
import asyncio
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from .db import encode, now

MAX_BYTES = 32 * 1024 * 1024
EXCLUDED = {'.git', '.devcontainer', 'node_modules', 'vendor', 'build', 'dist',
            '__pycache__', '.venv', 'asmlib', 'solutions', 'solution'}


class GitLabError(RuntimeError):
    pass


def project_url(url):
    part = urlsplit(url)
    path = part.path.split('/-/')[0].rstrip('/').removesuffix('.git')
    if part.scheme != 'https' or part.username or part.password or part.query or part.fragment:
        raise ValueError('GitLab-Projekt-URL muss HTTPS ohne Zugangsdaten verwenden')
    if not part.hostname or len(path.strip('/').split('/')) < 2 or any(p in ('.', '..', '') for p in path.strip('/').split('/')):
        raise ValueError('GitLab-Projektpfad fehlt')
    return 'https://' + part.netloc + path


def remote_url(project, remote='', *, wsl=False):
    project = project_url(project)
    host, path = urlsplit(project).hostname, urlsplit(project).path
    remote = remote.strip() or (f'git@{host}:{path.lstrip("/")}.git' if wsl else project + '.git')
    if wsl and remote.startswith('https://'):
        raise ValueError('Der WSL-SSH-Agent benötigt einen SSH-Remote; leer lassen für den passenden Standard.')
    if remote == project + '.git' or remote == project:
        return remote
    # SSH is explicit and bound to this project. No local paths, custom commands or tokens.
    if remote == f'git@{host}:{path.lstrip("/")}.git':
        return remote
    part = urlsplit(remote)
    if (part.scheme == 'ssh' and part.username == 'git' and not part.password
            and part.hostname == host and part.path.removesuffix('.git') == path
            and not part.query and not part.fragment):
        return remote
    raise ValueError('Git-Remote muss zum selben Projekt gehören (HTTPS oder SSH als git)')


def relevant(path):
    parts = PurePosixPath(path).parts
    if any(p.lower() in EXCLUDED or p.lower().endswith('.dist-info') for p in parts[:-1]):
        return False
    name = parts[-1].lower()
    return (name.endswith(('.md', '.markdown', '.pdf', '.rst')) or name in ('readme', 'instructions', 'aufgaben')
            or name.endswith('.txt') and any(s in name for s in ('readme', 'assignment', 'exercise', 'deadline', 'aufgabe', 'instruction')))


def wsl_ssh_command(config):
    distro,user=config.get('distribution',''),config.get('user','')
    socket,identity=config.get('socket',''),config.get('identity','')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',distro) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',user) or user=='root':
        raise ValueError('WSL-Distribution und normaler Benutzer fehlen oder sind ungültig.')
    for path in (socket,identity):
        if not re.fullmatch(r'/home/'+re.escape(user)+r'/[A-Za-z0-9_./-]{1,240}',path) or '..' in PurePosixPath(path).parts:
            raise ValueError('SSH-Socket und Schlüssel müssen sichere absolute Pfade im eigenen WSL-Home sein.')
    return (f'wsl.exe --distribution {distro} --user {user} --exec env SSH_AUTH_SOCK={socket} '
            f'ssh -i {identity} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15')


def wsl_ssh_from_form(form):
    if form.get('wsl_enabled')!='on':return None
    config={key:str(form.get('wsl_'+key,'')).strip() for key in ('distribution','user','socket','identity')}
    wsl_ssh_command(config)
    return config


def git(cache, *args, ssh_config=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    # Git for Windows bundles another agent client. Prefer the system client,
    # which can use this Windows user's persistent OpenSSH agent and config.
    native = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32/OpenSSH/ssh.exe'
    client = '"' + native.as_posix() + '"' if os.name == 'nt' and native.is_file() else 'ssh'
    env['GIT_SSH_COMMAND'] = wsl_ssh_command(ssh_config) if ssh_config else client + ' -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15'
    env['GIT_SSH_VARIANT'] = 'ssh'
    if ssh_config:
        env.update(GIT_SSH_VARIANT='ssh',MSYS_NO_PATHCONV='1',MSYS2_ARG_CONV_EXCL='*')
    try:
        result = subprocess.run(['git', '-c', 'protocol.file.allow=never', '-c', 'core.hooksPath=NUL',
                                 '--git-dir=' + str(cache), *args], env=env, capture_output=True,
                                timeout=90, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        raise GitLabError('Git nicht verfügbar oder Abruf nach 90 Sekunden abgebrochen; vorhandene Einträge bleiben erhalten.') from None
    if result.returncode:
        error = result.stderr.decode('utf-8', errors='replace').lower()
        if 'host key verification failed' in error:
            reason = 'SSH-Host nicht im bestehenden known_hosts bestätigt'
        elif 'permission denied' in error or 'authentication' in error or 'username' in error or 'could not read' in error:
            reason = ('WSL-SSH-Zugang nicht bereit; vorhandenen Schlüssel im Agenten entsperren oder Projektfreigabe prüfen'
                      if ssh_config else 'Repository nicht lesbar; vorhandenen SSH-Lesezugang bzw. Projektfreigabe prüfen')
        else:
            reason = 'Git-Abruf fehlgeschlagen; Remote und Netzwerk prüfen'
        raise GitLabError(reason + '. Vorhandene Einträge bleiben erhalten.')
    return result.stdout


def collect(source, db, store):
    config = json.loads(source['config'])
    project = project_url(source['url'])
    remote = remote_url(project, config.get('remote', ''),wsl=bool(config.get('wsl_ssh')))
    root = store.settings.data_dir / 'gitlab'
    root.mkdir(exist_ok=True)
    cache = root / f"source-{source['id']}.git"
    owner = root / f"source-{source['id']}.owner"
    # Only explicitly configured sources use this user's existing WSL agent.
    # Caches and local document reads still belong to the Windows application.
    def read_git(*args):
        return git(cache,*args,ssh_config=config['wsl_ssh']) if config.get('wsl_ssh') else git(cache,*args)
    if cache.exists() and (not owner.exists() or owner.read_text(encoding='utf-8') != project):
        raise GitLabError('Crawler-Verzeichnis gehört nicht zu dieser Quelle; keine fremde Kopie verändert.')
    if not cache.exists():
        read_git('init', '--bare')
        owner.write_text(project, encoding='utf-8')
    # HEAD is the advertised default, not an assumed main/master or a student's branch.
    read_git('fetch', '--no-tags', '--depth=1', remote, 'HEAD')
    revision = read_git('rev-parse', 'FETCH_HEAD').decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40,64}', revision):
        raise GitLabError('Repository-Commit konnte nicht eindeutig bestimmt werden')
    state = json.loads(source['crawl_state'])
    if state.get('revision') != revision or 'paths' not in state:
        files, blocked, skipped = [], [], 0
        for row in read_git('ls-tree', '-rz', '--long', revision).split(b'\0'):
            if not row:
                continue
            meta, raw_path = row.split(b'\t', 1)
            mode, kind, blob, size = meta.decode().split()
            path = raw_path.decode('utf-8')
            if mode not in ('100644', '100755') or kind != 'blob' or not relevant(path):
                skipped += 1
                continue
            if int(size) > MAX_BYTES:
                blocked.append(path)
            else:
                files.append((path, blob))
        state = {'revision': revision, 'remaining': files, 'total': len(files),
                 'blocked': blocked, 'skipped': skipped, 'paths': [p for p, _ in files] + blocked}
    budget = max(1, min(200, int(config.get('max_pages', 30))))
    selected = state['remaining'][:budget]
    for path, blob in selected:
        raw = read_git('cat-file', 'blob', blob)
        locator = project + '/-/blob/HEAD/' + quote(path, safe='/')
        immutable = project + '/-/blob/' + revision + '/' + quote(path, safe='/')
        # Stable locator is for identity; the immutable link/commit belong to this version.
        store.save(source['id'], locator, path, 'application/pdf' if path.lower().endswith('.pdf') else 'text/plain',
                   raw, revision=revision, repository_path=path, source_url=immutable)
    state['remaining'] = state['remaining'][len(selected):]
    complete = not state['remaining'] and not state['blocked']
    message = (f"Commit {revision[:12]} · {state['total']-len(state['remaining'])}/{state['total']} Dokumente abgerufen"
               f" · {state['skipped']} Code-/Konfigurations-/sonstige Dateien nicht ausgewertet.")
    if not state['total']:
        message += ' Keine Aufgabenblätter oder README-Dokumente in diesem Commit vorhanden.'
    if state['remaining']:
        message += f" {len(state['remaining'])} Downloads ausstehend; nächster Sync setzt fort."
    if state['blocked']:
        message += f" {len(state['blocked'])} Dokumente über 32 MiB nicht geladen: " + ', '.join(state['blocked'])
    removed = sorted({d['repository_path'] for d in db.rows('SELECT repository_path FROM documents WHERE source_id=?', (source['id'],))
                      if d['repository_path'] and d['repository_path'] not in state['paths']})
    if removed:
        message += ' Im aktuellen Commit fehlen frühere Dokumente: ' + ', '.join(removed) + '. Belege und Bearbeitungsstände bleiben erhalten; daraus folgt keine Erledigung oder Absage.'
    db.execute('UPDATE sources SET crawl_state=?,state=?,message=?,last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE id=?',
               (encode(state), 'success' if complete else 'partial', message, complete, now(), source['id']))
    return {'source': source['id'], 'complete': complete, 'revision': revision,
            'downloaded': len(selected), 'remaining_downloads': len(state['remaining']),
            'skipped_files': state['skipped'], 'blocked_files': state['blocked'], 'missing_files': removed, 'message': message}


async def collect_repository(source, db, store):
    return await asyncio.to_thread(collect, source, db, store)
