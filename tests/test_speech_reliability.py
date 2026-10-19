import io
import struct
import wave
import weakref
from types import SimpleNamespace

import numpy as np
import pytest

from studycenter_ice.speech import SpeechError, SpeechService


def recording(samples, rate=16000):
    raw = io.BytesIO()
    with wave.open(raw, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.asarray(samples, dtype='<i2').tobytes())
    return raw.getvalue()


def voiced(amplitude=1000):
    return amplitude * np.sin(np.arange(16000) * 2 * np.pi * 220 / 16000)


def test_whisper_mixed_languages_bind_aliases_and_preserve_original_transcript(monkeypatch):
    service = SpeechService(lambda:[{'title':'Computer Organization and Networks','aliases':['CON']}])
    seen = {}
    original = 'Richte mir für KON morgen eine Erinnerung für die Setup Session ein.'
    def transcribe(audio, **options):
        seen.update(options)
        return iter([SimpleNamespace(text=original,no_speech_prob=0,avg_logprob=0)]), None
    monkeypatch.setattr(service,'load',lambda:SimpleNamespace(transcribe=transcribe))
    assert service.recognize(np.ones(16000)) == original
    assert seen['language']=='de' and seen['multilingual'] is True
    assert 'CON: Computer Organization and Networks' in seen['initial_prompt']
    assert 'Deutsch' in seen['initial_prompt'] and 'English' in seen['initial_prompt']
    assert 'CON' in seen['hotwords'] and 'Computer Organization and Networks' in seen['hotwords']
    assert seen['condition_on_previous_text'] is False and seen['beam_size']==5


def test_digital_silence_and_dc_offset_do_not_load_a_model(monkeypatch):
    service = SpeechService()
    monkeypatch.setattr(service, 'recognize', lambda audio: pytest.fail('silence reached inference'))
    for samples in (np.zeros(16000), np.full(16000, 1000)):
        with pytest.raises(SpeechError, match='Keine Sprache aufgenommen'):
            service.transcribe(recording(samples))
        assert not service.lock.locked()
        assert service.model is None and service.fallback is None


def test_quiet_nonconstant_recording_reaches_inference(monkeypatch):
    service = SpeechService()
    seen = []
    monkeypatch.setattr(service, 'recognize', lambda audio: seen.append(audio) or 'Leise Aufnahme')
    assert service.transcribe(recording(voiced(amplitude=3)))['text'] == 'Leise Aufnahme'
    assert len(seen) == 1


def test_progress_reports_decode_and_inference_without_storing_audio(monkeypatch):
    service = SpeechService(engine='whisper', name='small', device='cuda')
    phases = []
    original = service.progress
    def progress(phase):
        phases.append(phase)
        original(phase)
    monkeypatch.setattr(service, 'progress', progress)
    def recognize(audio):
        assert service.started is not None and service.lock.locked()
        service.device='cuda'
        service.progress('Sprache erkennen · GPU')
        return 'CON morgen vorbereiten'
    monkeypatch.setattr(service, 'recognize', recognize)
    result = service.transcribe(recording(voiced()))
    assert phases == ['Aufnahme dekodieren', 'Sprache erkennen · GPU', 'Transkript fertig']
    assert result['text'] == 'CON morgen vorbereiten' and 'decode' in result['timings']
    assert service.started is None and not service.lock.locked()
    assert not any(isinstance(value,(bytes,np.ndarray)) for value in vars(service).values())


def test_fallback_retains_configuration_and_reuses_working_cpu_engine(monkeypatch, tmp_path):
    service = SpeechService(engine='parakeet', name='small', device='auto')
    service.root = tmp_path
    original_directory = service.directory
    calls = []
    class FailedModel:
        pass
    service.model = FailedModel()
    failed_model = weakref.ref(service.model)

    def recognize(self, audio):
        calls.append((self.engine, self.name, self.requested_device))
        if self.engine == 'parakeet':
            raise RuntimeError('fixture engine unavailable')
        self.device = 'cpu'
        return 'Aufgabe morgen'

    monkeypatch.setattr(SpeechService, 'recognize', recognize)
    first = service.transcribe(recording(voiced()))
    second = service.transcribe(recording(voiced()))
    assert calls == [('parakeet', 'small', 'auto'), ('whisper', 'base', 'cpu'), ('whisper', 'base', 'cpu')]
    assert (service.engine, service.name, service.requested_device, service.directory) == ('parakeet', 'small', 'auto', original_directory)
    assert first['configured_engine'] == 'parakeet' and first['engine'] == 'whisper'
    assert first['model'] == 'base' and first['device'] == 'cpu'
    assert second['warning'] == first['warning'] and 'RuntimeError' in first['warning']
    assert 'fixture engine unavailable' in first['warning']
    assert service.effective is service.fallback
    assert service.model is None and failed_model() is None


def test_failed_cpu_base_is_not_retried_and_lock_is_released(monkeypatch):
    service = SpeechService(engine='whisper', name='base', device='cpu')
    service.device = 'cpu'
    calls = []

    def recognize(audio):
        calls.append(True)
        raise RuntimeError('decoder failed')

    monkeypatch.setattr(service, 'recognize', recognize)
    with pytest.raises(SpeechError, match='decoder failed'):
        service.transcribe(recording(voiced()))
    assert len(calls) == 1 and not service.lock.locked()


def test_decoder_bound_applies_to_resampled_audio(monkeypatch):
    service = SpeechService()
    monkeypatch.setattr(service, 'recognize', lambda audio: pytest.fail('overlong audio reached inference'))
    with pytest.raises(SpeechError, match='60 Sekunden'):
        service.transcribe(recording(np.ones(8000 * 61), rate=8000))
    assert not service.lock.locked()


def test_nonfinite_audio_is_rejected_before_inference(monkeypatch):
    service = SpeechService()
    monkeypatch.setattr(service, 'recognize', lambda audio: pytest.fail('invalid audio reached inference'))
    data = np.full(16000, np.nan, dtype='<f4').tobytes()
    fmt = struct.pack('<HHIIHH', 3, 1, 16000, 64000, 4, 32)
    raw = b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' + struct.pack('<I', 16) + fmt + b'data' + struct.pack('<I', len(data)) + data
    with pytest.raises(SpeechError, match='ungültige Audiodaten'):
        service.transcribe(raw)
    assert not service.lock.locked()


def test_failed_fallback_does_not_claim_it_is_active(monkeypatch):
    service = SpeechService(engine='parakeet')
    monkeypatch.setattr(SpeechService, 'recognize', lambda self, audio: (_ for _ in ()).throw(RuntimeError('model failed')))
    with pytest.raises(SpeechError, match='model failed'):
        service.transcribe(recording(voiced()))
    assert service.fallback is None and 'Fallback aktiv' not in service.warning
    assert not service.lock.locked()


def test_repeated_engine_setup_does_not_grow_windows_search_path(tmp_path,monkeypatch):
    import os
    if os.name!='nt':pytest.skip('Windows DLL search path')
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    directory=tmp_path/'StudyCenter-ICE'/'speech-models'/'cuda12';directory.mkdir(parents=True)
    monkeypatch.setenv('PATH',str(directory)+os.pathsep+os.environ.get('PATH',''))
    before=os.environ['PATH']
    services=[SpeechService() for _ in range(3)]
    assert os.environ['PATH']==before and all(s.model is None for s in services)
