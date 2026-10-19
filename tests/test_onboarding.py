from fastapi.testclient import TestClient
from bs4 import BeautifulSoup

from studycenter_ice.app import create_app
from studycenter_ice.config import Settings


def browser(client, path):
    return client.get(path, headers={'Accept': 'text/html'})


def test_first_launch_pause_resume_and_completion_survive_restart(tmp_path):
    app = create_app(Settings(data_dir=tmp_path)); client = TestClient(app)
    response = browser(client, '/overview')
    assert response.url.path == '/welcome'
    assert BeautifulSoup(response.text, 'html.parser').select_one('#welcome-heading').get_text(' ', strip=True) == 'Dein Studium. Dein Überblick.'
    csrf = {'X-CSRF-Token': app.state.csrf}
    client.post('/welcome', data={'action': 'move', 'step': 'courses'}, headers=csrf)
    assert 'Ein erster Kurs' in client.get('/welcome').text
    response = client.post('/welcome', data={'action': 'open', 'step': 'courses', 'destination': 'courses'}, headers=csrf)
    assert response.url.path == '/sources' and 'Einrichtung fortsetzen' in response.text
    assert app.state.db.rows('SELECT * FROM courses') == []
    restarted = create_app(Settings(data_dir=tmp_path)); client = TestClient(restarted)
    assert 'Ein erster Kurs' in client.get('/welcome').text
    csrf = {'X-CSRF-Token': restarted.state.csrf}
    client.post('/welcome', data={'action': 'pause', 'step': 'courses'}, headers=csrf)
    assert browser(client, '/overview').url.path == '/overview'
    assert 'Ein erster Kurs' in client.get('/welcome').text
    client.post('/welcome', data={'action': 'complete', 'step': 'finish'}, headers=csrf)
    assert browser(client, '/overview').url.path == '/overview'
    assert restarted.state.db.rows('SELECT * FROM courses') == []
    assert restarted.state.db.rows('SELECT * FROM sources') == []
    assert restarted.state.db.rows('SELECT * FROM items') == []
    assert {row['name'] for row in restarted.state.db.rows('SELECT * FROM app_preferences')} == {'onboarding'}


def test_existing_data_is_not_interrupted_and_tour_can_be_repeated(tmp_path):
    app = create_app(Settings(data_dir=tmp_path)); client = TestClient(app)
    app.state.db.execute("INSERT INTO courses(title,semester) VALUES('Synthetischer Kurs','2030W')")
    assert browser(client, '/overview').url.path == '/overview'
    assert 'Einrichtung' in client.get('/settings').text
    headers = {'X-CSRF-Token': app.state.csrf}
    client.post('/welcome', data={'action': 'complete', 'step': 'finish'}, headers=headers)
    client.post('/welcome', data={'action': 'restart', 'step': 'welcome'}, headers=headers)
    assert BeautifulSoup(client.get('/welcome').text, 'html.parser').select_one('#welcome-heading').get_text(' ', strip=True) == 'Dein Studium. Dein Überblick.'
    assert len(app.state.db.rows('SELECT * FROM courses')) == 1


def test_tour_rejects_foreign_origin_invalid_steps_and_external_redirects(tmp_path):
    app = create_app(Settings(data_dir=tmp_path)); client = TestClient(app)
    data = {'action': 'move', 'step': 'model'}
    assert client.post('/welcome', data=data).status_code == 403
    headers = {'X-CSRF-Token': app.state.csrf}
    assert client.post('/welcome', data=data, headers=headers | {'Origin': 'https://foreign.example'}).status_code == 403
    assert client.post('/welcome', data={'action': 'move', 'step': 'unknown'}, headers=headers).status_code == 422
    assert client.post('/welcome', data={'action': 'open', 'step': 'model', 'destination': 'https://foreign.example'}, headers=headers).status_code == 400
    assert app.state.db.rows("SELECT * FROM app_preferences WHERE name='onboarding'") == []
    assert client.get('/api/status').status_code == 200
    assert client.get('/static/app.js').status_code == 200


def test_enhanced_navigation_and_unavailable_optional_features_do_not_fake_setup(tmp_path):
    app = create_app(Settings(data_dir=tmp_path)); client = TestClient(app)
    headers = {'X-CSRF-Token': app.state.csrf, 'X-Requested-With': 'fetch'}
    response = client.post('/welcome', data={'action': 'move', 'step': 'components'}, headers=headers)
    assert response.json() == {'url': '/welcome'}
    assert 'Diese Tour lädt keine Extras herunter.' in client.get('/welcome').text
    client.post('/welcome', data={'action': 'move', 'step': 'finish'}, headers=headers)
    html = client.get('/welcome').text
    assert 'Später möglich' in html and 'kein Live-Test' in html
    assert not (tmp_path/'components').exists()
    assert not (tmp_path/'speech').exists()


def test_damaged_tour_progress_does_not_break_the_app(tmp_path):
    app = create_app(Settings(data_dir=tmp_path)); client = TestClient(app)
    app.state.db.execute("INSERT INTO app_preferences(name,value) VALUES('onboarding',?)", ('{"step":[],"status":"active"}',))
    assert client.get('/welcome').status_code == 200
    assert client.get('/board').status_code == 200
