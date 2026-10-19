import asyncio
import json
import threading

import httpx
import pytest

from studycenter_ice.app import create_app
from studycenter_ice.config import Settings
from studycenter_ice.speech import SpeechService


def test_speech_settings_keep_environment_defaults_and_persist_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv('STUDYCENTER_ICE_STT_ENGINE', 'parakeet')
    monkeypatch.setenv('STUDYCENTER_ICE_STT_MODEL', 'base')
    monkeypatch.setenv('STUDYCENTER_ICE_STT_DEVICE', 'cpu')
    Settings(data_dir=tmp_path)
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'model_provider': 'chatgpt', 'oauth_model': 'gpt-6-luna', 'oauth_probe_passed': True, 'retained_key': 'fixture'}))
    settings = Settings(data_dir=tmp_path)
    assert (settings.stt_engine, settings.stt_model, settings.stt_device) == ('parakeet', 'base', 'cpu')
    settings.stt_engine, settings.stt_model, settings.stt_device = 'whisper', 'small', 'auto'
    settings.save()
    reloaded = Settings(data_dir=tmp_path)
    assert (reloaded.stt_engine, reloaded.stt_model, reloaded.stt_device) == ('whisper', 'small', 'auto')
    saved = json.loads(path.read_text())
    assert saved['oauth_model'] == 'gpt-6-luna' and saved['oauth_probe_passed'] is True
    assert saved['retained_key'] == 'fixture' and 'llm_api_key' not in saved
    saved['stt_engine'] = 'unknown'
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='Ungültige Spracherkennung'):
        Settings(data_dir=tmp_path)


@pytest.mark.asyncio
async def test_speech_settings_route_is_lazy_validated_and_busy_even_after_request_cancellation(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    headers = {'X-CSRF-Token': app.state.csrf}
    choices = {'stt_engine': 'parakeet', 'stt_model': 'small', 'stt_device': 'cpu'}
    monkeypatch.setattr(SpeechService, 'load', lambda self: pytest.fail('saving settings loaded a model'))
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def transcribe(self, raw):
        started.set()
        try:
            assert release.wait(5)
            return {'text': 'fixture'}
        finally:
            stopped.set()

    monkeypatch.setattr(SpeechService, 'transcribe', transcribe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        assert (await client.post('/settings/speech', data=choices)).status_code == 403
        response = await client.post('/settings/speech', data=choices, headers=headers)
        assert response.status_code == 303 and response.headers['location'] == '/settings#speech'
        current = app.state.speech
        assert current.model is None and current.engine == 'parakeet'
        status = (await client.get('/api/voice/status')).json()
        assert status['configured_engine'] == 'parakeet' and status['configured_device'] == 'cpu'
        saved = (tmp_path / 'settings.json').read_bytes()
        assert (await client.post('/settings/speech', data=choices | {'stt_device': 'bogus'}, headers=headers)).status_code == 400
        assert app.state.speech is current and (tmp_path / 'settings.json').read_bytes() == saved
        request = asyncio.create_task(client.post('/api/voice/transcribe', content=b'fixture', headers=headers | {'Content-Type': 'audio/wav'}))
        assert await asyncio.to_thread(started.wait, 3)
        try:
            assert (await client.post('/settings/speech', data=choices, headers=headers)).status_code == 409
            assert (await client.get('/api/voice/status')).json()['stt_busy'] is True
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert (await client.post('/settings/speech', data=choices, headers=headers)).status_code == 409
            assert app.state.speech is current
        finally:
            release.set()
            assert await asyncio.to_thread(stopped.wait, 3)
        for _ in range(20):
            if not (await client.get('/api/voice/status')).json()['stt_busy']:
                break
            await asyncio.sleep(.01)
        assert (await client.post('/settings/speech', data=choices, headers=headers)).status_code == 303
