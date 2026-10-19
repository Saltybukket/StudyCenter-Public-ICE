"""Stored-entry presentation keeps source precision and personal actions separate."""
import re

from fastapi.testclient import TestClient

from studycenter_ice.app import create_app
from studycenter_ice.config import Settings
from studycenter_ice.models import Candidate


def entry(identifier, course_id=1, course_title='Course A', **changes):
    status = changes.pop('status', 'open')
    value = Candidate(title=f'Row {identifier}', kind='exam', date='2027-01-26',
                      date_meaning='start', obligation='optional', quote='Source fixture',
                      description='A source description that stays expandable.', page=1,
                      anchor=str(identifier)).model_dump(mode='json')
    value.update(changes)
    return {'id': identifier, 'data': value, 'status': status, 'ignored': 0,
            'conflict': 0, 'pinned': 0, 'seen': 0, 'course_id': course_id,
            'course_title': course_title, 'identity': f'source:{identifier}'}


def test_information_rows_preserve_order_pagination_filters_and_source_labels(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path))
    rows = [entry(1), entry(2, 2, 'Course B'), entry(3)] + [entry(n) for n in range(4, 22)]
    monkeypatch.setattr(app.state.db, 'items', lambda **kwargs: rows)
    with TestClient(app) as client:
        response = client.get('/entries?view=all&q=Row&include_inactive=true')
        html = response.text
        assert response.status_code == 200
        assert re.findall(r'data-item-id="(\d+)"', html) == [str(n) for n in range(1, 21)]
        assert re.findall(r'class="entry-course-heading"[^>]*>([^<]+)', html) == ['Course A', 'Course B', 'Course A']
        assert '21 Einträge · Seite 1 von 2' in html
        assert html.count('Weitere Einträge') == 2
        assert 'include_inactive=true' in html and 'q=Row' in html and 'view=undated' in html
        assert '26.01.2027' in html and 'ohne Uhrzeit' in html
        assert 'Angebotener Termin, nicht gebucht' in html and 'Optional' in html
        assert 'entry-disclosure' in html and 'entry-proof' in html
        assert 'Wieder öffnen' not in html
        second = client.get('/entries?view=all&q=Row&include_inactive=true&page=2').text
        assert re.findall(r'data-item-id="(\d+)"', second) == ['21']


def test_archive_reopen_is_explicit_personal_status_with_original_closing_date(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path))
    rows = [entry(1, status='done', kind='registration', obligation='required',
                  date='2026-10-01', end_date='2026-10-09', end_time='23:55:00', date_meaning='window'),
            entry(2, status='open'),
            entry(3, status='done', kind='task', date=None, end_date='2026-10-12', date_meaning='deadline')]
    monkeypatch.setattr(app.state.db, 'items', lambda **kwargs: rows)
    with TestClient(app) as client:
        html = client.get('/archive').text
    assert re.findall(r'data-item-id="(\d+)"', html) == ['1', '3']
    assert 'Anmeldeschluss' in html and '09.10.2026 · 23:55' in html and 'Beginn 01.10.2026' in html
    assert 'Wieder öffnen' in html and 'name="status" value="open"' in html
    assert 'action="/items/1"' in html and 'name="action" value="status"' in html
    assert 'Eine Anmeldung oder Buchung wird dadurch nicht bestätigt.' in html
    assert 'Beginn None' not in html and 'Fällig' in html and '12.10.2026' in html
    assert 'Erledigt am' not in html and 'entry-course-heading' not in html
