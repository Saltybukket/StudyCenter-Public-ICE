"""Empty first run, public forms and own-component integration contracts."""
import json
from unittest.mock import Mock

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from studycenter_ice.app import create_app
from studycenter_ice.config import Settings


def test_empty_first_run_and_native_form_referrer_policy(tmp_path):
    app=create_app(Settings(data_dir=tmp_path))
    client=TestClient(app)
    for path in ('/overview','/','/board','/calendar','/entries','/archive','/sources','/documents','/mail','/model','/settings'):
        response=client.get(path)
        assert response.status_code==200,path
        assert response.headers['referrer-policy']=='same-origin'
        assert '<main' in response.text
    assert app.state.db.rows('SELECT * FROM courses')==[]
    assert app.state.db.rows('SELECT * FROM sources')==[]
    assert app.state.db.rows('SELECT * FROM documents')==[]
    assert app.state.db.rows('SELECT * FROM items')==[]
    assert not app.state.sync.active
    assert client.get('/api/desktop/status').json()['application']=='StudyCenter.Public.ICE'


def test_optional_component_needs_confirmation_and_never_executes_system_setup(tmp_path,monkeypatch):
    app=create_app(Settings(data_dir=tmp_path))
    call=Mock(side_effect=AssertionError('Unconfirmed component setup'))
    monkeypatch.setattr('studycenter_ice.platform.setup.ensure_component',call)
    client=TestClient(app)
    for body in ({'confirm':False},[],{'confirm':'true'}):
        result=client.post('/settings/components/ocr',json=body,headers={'X-CSRF-Token':app.state.csrf})
        assert result.status_code==400
    assert client.post('/settings/components/ocr',json={'confirm':True}).status_code==403
    call.assert_not_called()


def test_api_settings_blank_password_keeps_secret_and_clear_removes_it(tmp_path):
    settings=Settings(data_dir=tmp_path)
    app=create_app(settings)
    client=TestClient(app)
    headers={'X-CSRF-Token':app.state.csrf}
    data={'provider':'chat-completions','llm_base_url':'http://127.0.0.1:9011/v1','llm_model':'synthetic-model','llm_api_key':'synthetic-key-for-local-test'}
    assert client.post('/model/configure',data=data,headers=headers).status_code==200
    assert settings.llm_api_key==data['llm_api_key']
    assert 'synthetic-key-for-local-test' not in (tmp_path/'settings.json').read_text()
    html=client.get('/model').text
    assert 'synthetic-key-for-local-test' not in html
    assert not BeautifulSoup(html,'html.parser').select_one('[name=llm_api_key]').get('value')
    data['llm_api_key']=''
    assert client.post('/model/configure',data=data,headers=headers).status_code==200
    assert settings.llm_api_key=='synthetic-key-for-local-test'
    data['clear_api_key']='true'
    assert client.post('/model/configure',data=data,headers=headers).status_code==200
    assert settings.llm_api_key==''


def test_stop_without_running_instance_creates_no_dataset(tmp_path,monkeypatch):
    from studycenter_ice import launcher
    monkeypatch.setattr(launcher,'owned_status',lambda _:None)
    missing=tmp_path/'new-data'
    assert launcher.main(['--stop','--data-dir',str(missing)])==0
    assert not missing.exists()
