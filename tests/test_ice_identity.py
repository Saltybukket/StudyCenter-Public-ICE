from pathlib import Path
import pytest
from studycenter_ice.config import Settings, load_settings, local_profile_path


def test_new_instance_ignores_unprefixed_private_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('STUDYCENTER_ICE_DATA_DIR', str(tmp_path / 'own-data'))
    monkeypatch.setenv('LLM_BASE_URL', 'https://unrelated.invalid')
    monkeypatch.setenv('LLM_MODEL', 'unrelated')
    monkeypatch.setenv('LLM_API_KEY', 'unrelated')
    monkeypatch.setenv('TESSERACT_CMD', 'unrelated')
    settings = load_settings()
    assert (settings.llm_base_url, settings.llm_model, settings.llm_api_key) == ('', '', '')
    assert settings.tesseract != 'unrelated'
    assert local_profile_path('auth/accounts.dpapi').is_relative_to(settings.data_dir)


def test_profile_never_discovers_another_apps_legacy_data(tmp_path, monkeypatch):
    monkeypatch.setattr('studycenter_ice.config.ROOT', tmp_path / 'uninstalled-public-root')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    monkeypatch.delenv('STUDYCENTER_ICE_DATA_DIR', raising=False)
    assert local_profile_path('auth/accounts.dpapi') == (tmp_path / 'StudyCenter-Public-ICE/profiles/auth/accounts.dpapi').resolve()
    with pytest.raises(ValueError):
        local_profile_path('../escape')


def test_model_settings_do_not_disclose_secrets_in_repr(tmp_path):
    settings = Settings(data_dir=tmp_path, llm_api_key='synthetic-model-secret')
    assert 'synthetic-model-secret' not in repr(settings)


def test_linux_session_credentials_remain_usable_without_plaintext_files(tmp_path, monkeypatch):
    from studycenter_ice import chatgpt
    from studycenter_ice.google_calendar import GoogleAuth
    from studycenter_ice.mail import MailCredentials
    from studycenter_ice.mcp import credentials, issue_credential
    monkeypatch.setattr(chatgpt, 'windows_platform', lambda: False)
    monkeypatch.setenv('STUDYCENTER_ICE_CREDENTIAL_BACKEND', 'session')
    a, b = tmp_path / 'a', tmp_path / 'b'
    store = chatgpt.CredentialStore(data_dir=a)
    value = store.read()
    value['host_id'] = 'synthetic-host'
    store.write(value)
    assert chatgpt.CredentialStore(data_dir=a).read()['host_id'] == 'synthetic-host'
    assert chatgpt.CredentialStore(data_dir=b).read()['host_id'] == ''
    mail = MailCredentials(data_dir=a)
    mail.write({'password': 'synthetic-mail-secret', 'username': 'synthetic-user'})
    assert MailCredentials(data_dir=a).status()['configured']
    assert not MailCredentials(data_dir=b).status()['configured']
    google = GoogleAuth(a)
    google.save({'refresh_token': 'synthetic-google-secret'})
    assert GoogleAuth(a).read()['refresh_token'] == 'synthetic-google-secret'
    assert GoogleAuth(b).read() == {}
    token = issue_credential('codex', data_dir=a)
    assert credentials(data_dir=a)['codex'] == token
    assert credentials(data_dir=b) == {}
    assert store.storage_status()['persistent'] is False
    assert not list(tmp_path.rglob('*'))


def test_unavailable_secure_keyring_fails_closed_without_files(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from studycenter_ice import chatgpt
    monkeypatch.setattr(chatgpt, 'windows_platform', lambda: False)
    monkeypatch.setenv('STUDYCENTER_ICE_CREDENTIAL_BACKEND', 'keyring')
    monkeypatch.setitem(sys.modules, 'keyring.backends.SecretService', SimpleNamespace(Keyring=lambda: SimpleNamespace(priority=0)))
    with pytest.raises(chatgpt.ChatGPTError, match='secure_keyring_unavailable'):
        chatgpt.SecureJSONStore(tmp_path / 'secret.dpapi')
    assert not list(tmp_path.rglob('*'))


def test_model_settings_round_trip_encrypted_key_and_plain_nonsecret_choices(tmp_path):
    settings = Settings(data_dir=tmp_path, llm_base_url='https://model.example.invalid/v1',
                        llm_model='own-model', llm_api_key='synthetic-model-secret')
    settings.save()
    raw = (tmp_path / 'settings.json').read_text()
    assert 'synthetic-model-secret' not in raw
    restored = Settings(data_dir=tmp_path)
    assert (restored.llm_base_url, restored.llm_model, restored.llm_api_key) == (
        'https://model.example.invalid/v1', 'own-model', 'synthetic-model-secret')
    if restored._model_credentials.backend == 'dpapi':
        assert b'synthetic-model-secret' not in restored._model_credentials.path.read_bytes()


def test_selected_data_directory_rejects_unowned_records_without_modifying_them(tmp_path):
    data = tmp_path / 'unowned'
    data.mkdir()
    original = data / 'records.txt'
    original.write_text('synthetic unrelated records')
    with pytest.raises(ValueError, match='Instanz'):
        Settings(data_dir=data)
    assert sorted(p.name for p in data.iterdir()) == ['records.txt']
    assert original.read_text() == 'synthetic unrelated records'


def test_reset_same_directory_gets_new_credential_namespace(tmp_path, monkeypatch):
    import json
    import uuid
    from studycenter_ice import chatgpt
    monkeypatch.setattr(chatgpt, 'windows_platform', lambda: False)
    monkeypatch.setenv('STUDYCENTER_ICE_CREDENTIAL_BACKEND', 'session')
    settings = Settings(data_dir=tmp_path / 'dataset')
    old = chatgpt.CredentialStore(data_dir=settings.data_dir)
    value = old.read()
    value['host_id'] = 'synthetic-old-instance'
    old.write(value)
    marker = settings.data_dir / 'instance.json'
    replacement_id = str(uuid.uuid4())
    marker.write_text(json.dumps({'application': 'StudyCenter.Public.ICE', 'instance_id': replacement_id}))
    new = chatgpt.CredentialStore(data_dir=settings.data_dir)
    assert new.read()['host_id'] == ''
    assert replacement_id in new.namespace
    assert old.namespace != new.namespace


def test_default_data_dir_follows_only_own_installation_receipt(tmp_path, monkeypatch):
    import json
    from studycenter_ice import config
    own_root = tmp_path / 'public-install'
    (own_root / '.local').mkdir(parents=True)
    monkeypatch.setattr(config, 'ROOT', own_root)
    monkeypatch.delenv('STUDYCENTER_ICE_DATA_DIR', raising=False)
    selected = tmp_path / 'selected-public-data'
    receipt = own_root / '.local/installation.json'
    receipt.write_text(json.dumps({'application': 'StudyCenter.Public.ICE', 'root': str(own_root), 'data_dir': str(selected)}))
    assert config.default_data_dir() == selected.resolve()
    receipt.write_text(json.dumps({'application': 'StudyCenter.Public.ICE', 'root': str(tmp_path / 'other-install'), 'data_dir': str(selected)}))
    with pytest.raises(ValueError, match='Installation'):
        config.default_data_dir()
    assert not selected.exists()
