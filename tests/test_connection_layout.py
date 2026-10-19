"""Local template checks; no TU login, model call or production data writes."""
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from studycenter_ice.app import create_app
from studycenter_ice.config import Settings
from studycenter_ice.db import encode


def test_mail_source_receipts_and_both_configuration_forms_remain_available(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path))
    monkeypatch.setattr('studycenter_ice.app.MailCredentials.status', lambda _: {'configured': False})
    cid = app.state.db.execute("INSERT INTO courses(title,semester) VALUES('CON','2026W')")
    receipt = '6 Nachrichten geprüft · Nicht konfigurierte Unterordner sind nicht enthalten.'
    sid = app.state.db.execute(
        "INSERT INTO sources(course_id,kind,title,url,config,state,message) VALUES(?,'mail','CON · INBOX','https://example.test',?,'partial',?)",
        (cid, encode({'allow_model_analysis': True, 'model_document_ids': [4, 8]}), receipt),
    )
    html = BeautifulSoup(TestClient(app).get('/mail').text, 'html.parser')
    source = html.select_one('.mail-source')
    assert source.select_one('.mail-receipt').get_text().find(receipt) >= 0
    assert source.select_one('.source-state').get_text(strip=True) == 'Abruf unvollständig'
    assert '2 ausgewählte Originalmails' in source.get_text()
    assert html.select_one('.mail-config > summary').get_text(' ', strip=True).startswith('Neue Mailquelle TU-Webmail-Quelle')
    expected = {
        '/mail/login': {'csrf'},
        '/mail/webmail': {'csrf', 'include_all', 'all_folders', 'course_id', 'folder', 'since', 'folders', 'keywords', 'senders', 'allow_model_analysis'},
        None: {'csrf', 'host', 'address', 'username', 'password', 'course_id', 'folder', 'since', 'keywords', 'senders', 'allow_model_analysis'},
        f'/api/mail/collect?source_id={sid}': {'csrf'},
        f'/api/sync?source_id={sid}': {'csrf'},
    }
    for action, fields in expected.items():
        form = next(f for f in html.select('.mail-page form') if f.get('action') == action)
        assert form['method'] == 'post'
        assert {node['name'] for node in form.select('[name]')} == fields
        assert form.select_one('[name=csrf]')['value'] == app.state.csrf
    assert not html.select_one('.mail-receipt').has_attr('open')


def test_connected_model_card_preserves_form_hooks_alternatives_and_gotcha_controls(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, model_provider='chatgpt', oauth_model='gpt-6-luna')
    app = create_app(settings)
    auth = app.state.sync.auth
    auth.models = [{'slug': 'gpt-6-luna', 'display_name': 'GPT-6 Luna'}]
    monkeypatch.setattr(auth, 'status', lambda: {
        'connected': True, 'plan_enabled': True, 'profiles': [{'client_id': 'fixture', 'label': 'Fixture account'}],
        'active': 'fixture', 'account': 'person@example.test', 'models': auth.models,
        'message': 'Lokale Verbindungsmeldung',
    })
    app.state.sync.model.gotchas.edit(title='Quizschluss prüfen', lesson='Schluss ist keine Öffnung.', scope='all', active=True)
    html = BeautifulSoup(TestClient(app).get('/model').text, 'html.parser')
    card = html.select_one('.model-account-card')
    assert card.select_one('.connection-badge').get_text(strip=True) == 'Verbunden'
    assert 'Lokale Verbindungsmeldung' in card.get_text() and 'person@example.test' in card.get_text()
    connect = card.select_one('#chatgpt-connect')
    assert connect['action'] == '/model/connect' and connect.select_one('select[name=account] option[selected]')['value'] == 'fixture'
    assert card.select_one('form[action="/model/disconnect"]')
    configure = card.select_one('form[action="/model/configure"]')
    assert configure.select_one('select[name=model] option[selected]')['value'] == 'gpt-6-luna'
    provider = configure.select_one('.model-provider')
    assert not provider.has_attr('open')
    assert {o['value'] for o in provider.select('select[name=provider] option')} == {'chatgpt', 'chat-completions'}
    gotcha = html.select_one('.gotcha-rule')
    assert gotcha.select_one('summary .gotcha-edit-label').get_text() == 'Bearbeiten'
    assert gotcha.select_one('form[data-gotcha-form][action$="/toggle"]')
    assert gotcha.select_one('form[data-gotcha-form][action="/model/gotchas"] [name=id]')
    assert html.select_one('.gotcha-new form[data-gotcha-form]')
    assert html.select_one('form[action="/model/probe"] [name=page_id]')
    assert html.select_one('form[action="/model/probe"] [name=pdf_id]')
    for form in html.select('.model-page form'):
        assert form['method'] == 'post'
        assert form.select_one('[name=csrf]')['value'] == app.state.csrf


def test_alternative_model_provider_and_disconnected_account_are_visible(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path, model_provider='chat-completions'))
    monkeypatch.setattr(app.state.sync.auth, 'status', lambda: {'connected': False, 'plan_enabled': False, 'profiles': [], 'models': [], 'message': 'Anmeldung nötig'})
    html = BeautifulSoup(TestClient(app).get('/model').text, 'html.parser')
    assert html.select_one('.model-provider').has_attr('open')
    assert html.select_one('select[name=provider] option[selected]')['value'] == 'chat-completions'
    assert 'Anmeldung nötig' in html.select_one('.model-account-card').get_text()
    assert 'Continue with ChatGPT' in html.select_one('#chatgpt-connect button').get_text()
    assert html.select_one('form[action="/model/disconnect"]') is None
