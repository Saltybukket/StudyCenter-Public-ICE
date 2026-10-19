SYNTHETIC_HOME = '/home' + '/student'
import asyncio
import json
import subprocess

import pytest

from studycenter_ice.config import Settings
from studycenter_ice.db import Database, encode
from studycenter_ice.documents import DocumentStore
from studycenter_ice.gitlab import GitLabError, collect, relevant, remote_url
from studycenter_ice.llm import ModelAdapter
from studycenter_ice.models import Candidate, Extraction


def test_wsl_ssh_configuration_is_bound_and_cannot_inject_commands():
    from studycenter_ice.gitlab import wsl_ssh_command
    config={'distribution':'Ubuntu-Test','user':'student','socket':f'{SYNTHETIC_HOME}/.ssh/studycenter_ice-agent.sock','identity':f'{SYNTHETIC_HOME}/.ssh/id_ed25519'}
    command=wsl_ssh_command(config)
    assert 'BatchMode=yes' in command and 'StrictHostKeyChecking=yes' in command
    assert f'{SYNTHETIC_HOME}/.ssh/id_ed25519' in command
    assert remote_url('https://gitlab.example/course/group',wsl=True)=='git@gitlab.example:course/group.git'
    with pytest.raises(ValueError):remote_url('https://gitlab.example/course/group','https://gitlab.example/course/group.git',wsl=True)
    for field,value in [('distribution','Ubuntu-Test; echo unsafe'),('user','root'),('socket',f'{SYNTHETIC_HOME}/../other/agent'),('identity',f'{SYNTHETIC_HOME}/.ssh/key $(command)')]:
        with pytest.raises(ValueError):wsl_ssh_command(config|{field:value})


def test_git_uses_native_agent_and_keeps_wsl_paths_literal(tmp_path, monkeypatch):
    from studycenter_ice import gitlab
    captured=[]
    def run(command, **kwargs):
        captured.append(kwargs['env'])
        return subprocess.CompletedProcess(command,0,b'HEAD',b'')
    monkeypatch.setattr(gitlab.subprocess,'run',run)
    gitlab.git(tmp_path,'ls-remote','git@gitlab.example:course/group.git','HEAD')
    native=gitlab.Path(gitlab.os.environ.get('WINDIR','C:/Windows'))/'System32/OpenSSH/ssh.exe'
    if gitlab.os.name=='nt' and native.is_file():
        assert native.as_posix() in captured[-1]['GIT_SSH_COMMAND']
    assert 'StrictHostKeyChecking=yes' in captured[-1]['GIT_SSH_COMMAND']
    config={'distribution':'Ubuntu-Test','user':'student','socket':f'{SYNTHETIC_HOME}/.ssh/studycenter_ice-agent.sock','identity':f'{SYNTHETIC_HOME}/.ssh/id_ed25519'}
    gitlab.git(tmp_path,'ls-remote','git@gitlab.example:course/group.git','HEAD',ssh_config=config)
    assert captured[-1]['MSYS_NO_PATHCONV']=='1' and captured[-1]['MSYS2_ARG_CONV_EXCL']=='*'
    assert f'{SYNTHETIC_HOME}/.ssh/id_ed25519' in captured[-1]['GIT_SSH_COMMAND']


def test_gitlab_wsl_source_roundtrip_preserves_existing_clients(tmp_path):
    from fastapi.testclient import TestClient
    from studycenter_ice.app import create_app
    app=create_app(Settings(data_dir=tmp_path));db=app.state.db
    course=db.execute("INSERT INTO courses(title,semester) VALUES('SLP','2026W')")
    client=TestClient(app);headers={'X-CSRF-Token':app.state.csrf}
    data={'course_id':str(course),'kind':'gitlab','title':'SLP','url':'https://gitlab.example/course/group','wsl_enabled':'on','wsl_distribution':'Ubuntu-Test','wsl_user':'student','wsl_socket':f'{SYNTHETIC_HOME}/.ssh/studycenter_ice-agent.sock','wsl_identity':f'{SYNTHETIC_HOME}/.ssh/id_ed25519'}
    response=client.post('/sources',data=data,headers=headers,follow_redirects=False)
    assert response.status_code==303
    source=db.source(db.one('SELECT id FROM sources')['id']);config=json.loads(source['config'])
    assert config['wsl_ssh']['distribution']=='Ubuntu-Test' and config['remote'].startswith('git@')
    # An old form without WSL fields must retain the configured access.
    assert client.post('/sources/'+str(source['id']),data={'active':'on','max_pages':'2'},headers=headers,follow_redirects=False).status_code==303
    assert json.loads(db.source(source['id'])['config'])['wsl_ssh']==config['wsl_ssh']
    assert client.post('/sources/'+str(source['id']),data={'active':'on','wsl_settings':'1'},headers=headers,follow_redirects=False).status_code==303
    assert 'wsl_ssh' not in json.loads(db.source(source['id'])['config'])
    bad=data|{'url':'https://gitlab.example/course/other','wsl_distribution':'invalid; command'}
    assert client.post('/sources',data=bad,headers=headers,follow_redirects=False).status_code==400
    assert db.one('SELECT count(*) n FROM sources')['n']==1


def test_gitlab_versions_resume_and_preserve_personal_work(tmp_path, monkeypatch):
    """Local fixture: real Git objects, download cursor and existing extraction/ingestion."""
    work = tmp_path / 'student-work'
    work.mkdir()
    def command(*args):
        return subprocess.check_output(['git', '-C', str(work), *args], stderr=subprocess.STDOUT)
    command('init')
    command('config', 'user.name', 'Fixture')
    command('config', 'user.email', 'fixture@example.invalid')
    instructions = work / 'README.md'
    instructions.write_text('A2 abgeben bis 12. November 2026.', encoding='utf-8')
    (work / 'tasks.md').write_text('Keine weitere Frist.', encoding='utf-8')
    (work / 'solution.c').write_text('STUDENT WORK', encoding='utf-8')
    command('add', '.')
    command('commit', '-m', 'Fixture version 1')
    sha = command('rev-parse', 'HEAD').decode().strip()
    (work / 'solution.c').write_text('UNCOMMITTED STUDENT WORK', encoding='utf-8')
    before = command('status', '--porcelain')
    settings = Settings(data_dir=tmp_path / 'data', llm_base_url='http://fixture.invalid', llm_model='fixture')
    db = Database(settings.data_dir / 'studycenter_ice.sqlite3')
    course = db.execute("INSERT INTO courses(title,semester) VALUES('CON','2026W')")
    sid = db.execute("INSERT INTO sources(course_id,kind,title,url,config) VALUES(?,'gitlab','Fixture','https://gitlab.example/con/upstream',?)",
                     (course, encode({'max_pages': 1})))
    store = DocumentStore(db, settings)
    from studycenter_ice import gitlab
    original = gitlab.git
    def fixture_git(cache, *args):
        if args[0] == 'fetch':
            # Only the test fixture uses a local remote. Product rejects local paths.
            return subprocess.check_output(['git', '--git-dir='+str(cache), 'fetch', '--depth=1', str(work), 'HEAD'], stderr=subprocess.STDOUT)
        return original(cache, *args)
    monkeypatch.setattr(gitlab, 'git', fixture_git)
    first = collect(db.source(sid), db, store)
    assert not first['complete'] and first['remaining_downloads'] == 1
    assert collect(db.source(sid), db, store)['complete']
    doc = db.one("SELECT * FROM documents WHERE title='README.md'")
    assert doc['revision'] == sha and sha in doc['source_url'] and doc['repository_path'] == 'README.md'
    assert db.one('SELECT count(*) n FROM documents')['n'] == 2
    adapter = ModelAdapter(db, settings)
    async def extract(segment, context):
        assert context['repository']['commit'] in (sha, new_sha)
        text = segment[0]['text']
        return Extraction(items=[Candidate(title='A2 abgeben', kind='task', identity_scope='course', identity_key='A2',
                         semester='2026W', date='2026-11-12' if '12.' in text else '2026-11-13', date_meaning='deadline',
                         obligation='required', quote=text, page=1, anchor='A2')])
    monkeypatch.setattr(adapter, 'extract', extract)
    new_sha = sha
    asyncio.run(adapter.analyze(store.process(doc['id'])))
    item = db.one('SELECT * FROM items')
    db.execute("UPDATE items SET status='doing',pinned=1,overrides=? WHERE id=?", (encode({'title': 'Meine A2'}), item['id']))
    collect(db.source(sid), db, store)
    assert db.one('SELECT count(*) n FROM documents')['n'] == 2
    instructions.write_text('A2 abgeben bis 13. November 2026.', encoding='utf-8')
    command('add', 'README.md')
    command('commit', '-m', 'Fixture version 2')
    new_sha = command('rev-parse', 'HEAD').decode().strip()
    collect(db.source(sid), db, store)
    newer = db.one("SELECT * FROM documents WHERE title='README.md' ORDER BY id DESC LIMIT 1")
    asyncio.run(adapter.analyze(store.process(newer['id'])))
    items = db.items()
    assert len(items) == 1 and items[0]['id'] == item['id']
    assert items[0]['status'] == 'doing' and items[0]['pinned'] and items[0]['data']['title'] == 'Meine A2'
    assert items[0]['data']['date'] == '2026-11-13'
    assert db.one('SELECT count(*) n FROM item_evidence')['n'] == 2
    assert command('status', '--porcelain') == before
    assert (work / 'solution.c').read_text() == 'UNCOMMITTED STUDENT WORK'
    command('rm', 'tasks.md')
    command('commit', '-m', 'Fixture removed instructions')
    missing = collect(db.source(sid), db, store)
    assert missing['missing_files'] == ['tasks.md']
    assert db.one('SELECT count(*) n FROM documents')['n'] == 3
    assert db.items()[0]['status'] == 'doing'


def test_gitlab_filter_access_errors_and_no_foreign_cache(tmp_path, monkeypatch):
    assert relevant('lab1/README.md') and relevant('lab1/assignment.pdf')
    assert not relevant('common/asmlib/README.md') and not relevant('lab1/solution.c')
    assert remote_url('https://gitlab.example/con/private', 'git@gitlab.example:con/private.git').startswith('git@')
    for remote in ('C:/student/repo', 'https://secret@gitlab.example/con/private.git', 'git@other.example:con/private.git'):
        with pytest.raises(ValueError):
            remote_url('https://gitlab.example/con/private', remote)
    settings = Settings(data_dir=tmp_path / 'data')
    db = Database(settings.data_dir / 'studycenter_ice.sqlite3')
    sid = db.execute("INSERT INTO sources(kind,title,url) VALUES('gitlab','Private','https://gitlab.example/con/private')")
    store = DocumentStore(db, settings)
    original = store.save(sid, 'https://gitlab.example/con/private/-/blob/HEAD/README.md', 'README.md', 'text/plain', b'Existing evidence')
    from studycenter_ice import gitlab
    def failed(cache, *args):
        raise GitLabError('SSH-Host nicht im bestehenden known_hosts bestätigt')
    monkeypatch.setattr(gitlab, 'git', failed)
    with pytest.raises(GitLabError, match='SSH-Host'):
        collect(db.source(sid), db, store)
    assert db.one('SELECT id FROM documents')['id'] == original
    cache = settings.data_dir / 'gitlab' / f'source-{sid}.git'
    cache.mkdir()
    with pytest.raises(GitLabError, match='keine fremde Kopie'):
        collect(db.source(sid), db, store)
