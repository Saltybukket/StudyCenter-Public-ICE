"""Small performance contracts: no state cache, no source/network/model work."""
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from studycenter_ice.app import create_app
from studycenter_ice.config import Settings
from studycenter_ice.db import encode, now
from studycenter_ice.documents import DocumentStore
from studycenter_ice.models import Candidate
from studycenter_ice.page_images import PageImages


def pdf_bytes(text):
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((40, 40), text)
        return pdf.tobytes()


def test_pdf_preview_cache_revalidates_bytes_and_is_bounded(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    db = app.state.db
    sid = db.execute("INSERT INTO sources(kind,title,url) VALUES('public','Probe','https://example.org')")
    identifier = DocumentStore(db, settings).save(sid, 'https://example.org/a.pdf', 'PDF', 'application/pdf', pdf_bytes('First'))
    original = pymupdf.Page.get_pixmap
    rendered = []
    def render(page, *args, **kwargs):
        rendered.append(page.number)
        return original(page, *args, **kwargs)
    monkeypatch.setattr(pymupdf.Page, 'get_pixmap', render)
    client = TestClient(app)
    url = f'/documents/{identifier}/pages/1.png'
    first = client.get(url)
    repeated = client.get(url)
    assert first.status_code == 200 and first.content == repeated.content and len(rendered) == 1
    assert first.headers['cache-control'] == 'private, max-age=0, must-revalidate'
    assert 'content-encoding' not in first.headers  # PNG is already compressed.
    unchanged = client.get(url, headers={'If-None-Match': 'W/' + first.headers['etag']})
    assert unchanged.status_code == 304 and not unchanged.content and len(rendered) == 1
    path = Path(db.one('SELECT original_path FROM documents WHERE id=?', (identifier,))['original_path'])
    path.write_bytes(pdf_bytes('Replacement'))
    changed = client.get(url, headers={'If-None-Match': first.headers['etag']})
    assert changed.status_code == 200 and changed.content != first.content and changed.headers['etag'] != first.headers['etag']
    assert client.get(url.replace('/1.png', '/0.png')).status_code == 404
    assert client.get(url.replace('/1.png', '/2.png')).status_code == 404
    cache = PageImages(tmp_path, max_entries=1)
    cache.image(path, 1)
    assert len(list(cache.directory.glob('*.png'))) <= 2  # Existing entries are pruned on new writes.
    path.write_bytes(pdf_bytes('Third'))
    cache.image(path, 1)
    assert len(list(cache.directory.glob('*.png'))) == 1
    cache = PageImages(tmp_path, max_bytes=1)
    path.write_bytes(pdf_bytes('Oversize'))
    assert cache.image(path, 1)[0].startswith(b'\x89PNG')


def test_document_list_selects_counts_and_compressed_feed_keeps_fresh_status(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    db = app.state.db
    sid = db.execute("INSERT INTO sources(kind,title,url) VALUES('public','Probe','https://example.org')")
    identifier = DocumentStore(db, settings).save(sid, 'https://example.org/doc', 'Readable', 'text/plain', b'Original ' * 1000)
    db.execute("UPDATE documents SET pages=?,processing_state='success',analysis_state='success' WHERE id=?",
               (encode([{'page': 1, 'text': 'long text' * 10000}, {'page': 2, 'text': 'second'}]), identifier))
    selected = []
    rows = db.rows
    def traced(sql, *args, **kwargs):
        result = rows(sql, *args, **kwargs)
        if 'page_count' in sql:
            selected.extend(result)
        return result
    monkeypatch.setattr(db, 'rows', traced)
    client = TestClient(app)
    response = client.get('/documents')
    assert response.status_code == 200 and 'Readable' in response.text
    original = client.get(f'/documents/{identifier}/original')
    assert original.content == b'Original ' * 1000 and 'content-encoding' not in original.headers
    assert selected[0]['page_count'] == 2 and 'pages' not in selected[0] and 'progress' not in selected[0]
    value = Candidate(title='Own appointment', kind='event', date='2030-10-14', time='09:00',
                      date_meaning='start', obligation='required', quote='Personal event', page=1,
                      anchor='own', description='Preserved description ' * 150)
    item = db.execute('INSERT INTO items(identity,payload,created_at,changed_at) VALUES(?,?,?,?)',
                      ('note:probe', value.model_dump_json(), now(), now()))
    feed = client.get('/api/calendar', headers={'Accept-Encoding': 'gzip'})
    assert feed.headers['content-encoding'] == 'gzip' and 'Accept-Encoding' in feed.headers['vary']
    assert feed.json()[0]['extendedProps']['description'] == value.description
    db.execute("UPDATE items SET status='done' WHERE id=?", (item,))
    assert client.get('/api/calendar').json()[0]['extendedProps']['status'] == 'done'
    assert len(feed.content) > int(feed.headers['content-length']) * 5
    from fastapi.responses import Response
    @app.get('/performance-event-stream')
    async def event_stream():
        return Response('data: preserved\n\n' * 100, media_type='text/event-stream')
    assert 'content-encoding' not in client.get('/performance-event-stream').headers


@pytest.mark.skipif(__import__('sys').platform!='win32',reason='Native Windows desktop failure receipt')
def test_desktop_start_failure_replaces_previous_ready_receipt(tmp_path, monkeypatch):
    import json
    import os
    import logging
    from types import SimpleNamespace
    from unittest.mock import Mock
    from studycenter_ice import desktop
    settings = Settings(data_dir=tmp_path)
    (tmp_path / 'desktop-health.json').write_text('{"ready":true}')
    monkeypatch.setattr(desktop, 'load_settings', lambda: settings)
    monkeypatch.setattr(desktop, 'RotatingFileHandler', lambda *args, **kwargs: logging.NullHandler())
    monkeypatch.setattr(desktop.portalocker, 'Lock', lambda *args, **kwargs: SimpleNamespace(acquire=Mock(), release=Mock()))
    monkeypatch.setattr(desktop, 'ensure_backend', Mock(side_effect=RuntimeError('Local startup failure')))
    desktop.main(['--silent'])
    receipt = json.loads((tmp_path / 'desktop-health.json').read_text())
    assert receipt['ready'] is False and receipt['phase'] == 'start_failed'
    assert receipt['pid'] == os.getpid() and receipt['error_type'] == 'RuntimeError'


@pytest.mark.skipif(__import__('sys').platform!='win32',reason='Native Windows webview bounds')
def test_desktop_unreadable_saved_bounds_uses_safe_default(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock
    from studycenter_ice import desktop
    class Event:
        def __iadd__(self, callback):
            return self
    window = SimpleNamespace(events=SimpleNamespace(closing=Event(), before_show=Event(), loaded=Event()))
    view = SimpleNamespace(create_window=Mock(return_value=window), settings={}, start=Mock())
    monkeypatch.setitem(sys.modules, 'webview', view)
    settings=Settings(data_dir=tmp_path)
    (tmp_path / 'desktop-window.json').write_text('{invalid')
    desktop.Desktop(settings).native_window(False)
    assert view.create_window.call_args.kwargs['width'] == 1120
    assert (tmp_path / 'desktop-window.json').read_text() == '{invalid'
