"""Short in-memory recordings with one reusable local STT engine and CPU fallback."""
import io
import logging
import os
import threading
import time
import weakref
from pathlib import Path
from .config import local_profile_path

MAX_AUDIO = 16 * 1024**2
MAX_SECONDS = 60


class SpeechError(ValueError):
    pass


class SpeechService:
    def __init__(self, courses=lambda:[], *, engine=None, name=None, device=None, data_dir=None):
        self.courses=courses
        self.engine=engine or os.getenv('STUDYCENTER_ICE_STT_ENGINE','whisper')
        if self.engine not in ('whisper','parakeet'):raise ValueError('STUDYCENTER_ICE_STT_ENGINE muss whisper oder parakeet sein.')
        self.name = name or os.getenv('STUDYCENTER_ICE_STT_MODEL', 'small')
        if self.name not in ('base', 'small'):
            raise ValueError('STUDYCENTER_ICE_STT_MODEL muss base oder small sein.')
        # A selected dataset owns its caches; never inspect another app's profile.
        self.root=((Path(data_dir).resolve()/'speech-models') if data_dir is not None
                   else local_profile_path('speech-models'))
        if self.root.is_symlink() or not self.root.resolve().is_relative_to(self.root.parent.resolve()):
            raise ValueError('Sprachmodelle müssen im eigenen ausgewählten Datenverzeichnis bleiben.')
        self.directory = self.root / (self.name if self.engine=='whisper' else 'parakeet-v3')
        self.requested_device=device or os.getenv('STUDYCENTER_ICE_STT_DEVICE','auto')
        if self.requested_device not in ('auto','cpu','cuda'):raise ValueError('STUDYCENTER_ICE_STT_DEVICE muss auto, cpu oder cuda sein.')
        self.device='noch nicht geladen'
        self.warning=''
        self.dll_handles=[]
        cuda_directory=self.root/'cuda12'
        if os.name=='nt' and cuda_directory.exists():
            directory=str(cuda_directory)
            self.dll_handles.append(os.add_dll_directory(directory))
            if os.path.normcase(directory) not in {os.path.normcase(p) for p in os.environ.get('PATH','').split(os.pathsep)}:
                os.environ['PATH']=directory+os.pathsep+os.environ.get('PATH','')
        self.model = None
        self.fallback = None
        self.lock = threading.Lock()
        self.phase = 'Bereit'
        self.started = None
        self.timings = {}
        self._progress_parent = None

    def progress(self, phase):
        self.phase = phase
        parent = self._progress_parent() if self._progress_parent is not None else None
        if parent is not None:
            parent.progress(phase)

    def measured(self, key, started):
        target = (self._progress_parent() if self._progress_parent is not None else None) or self
        target.timings[key] = round(time.perf_counter()-started, 2)

    @property
    def effective(self):
        """The active engine, without losing the configuration selected by the user."""
        return self.fallback or self

    def load(self):
        if self.model is None:
            if self.engine=='whisper' and not (self.directory/'model.bin').is_file():
                raise SpeechError('Whisper-Modell fehlt. Unter Einstellungen / Sprache ausdrücklich herunterladen; Texteingabe ist weiterhin verfügbar.')
            load_started=time.perf_counter()
            self.progress('Modellbibliotheken laden')
            if self.engine=='parakeet':
                if not all((self.directory/f).exists() for f in ('encoder.int8.onnx','decoder.int8.onnx','joiner.int8.onnx','tokens.txt')):
                    raise SpeechError('Parakeet-v3-Modelldateien fehlen; Whisper-Fallback verwenden.')
                if os.name=='nt':
                    # Windows also ships an older onnxruntime.dll. Bind the pinned package's
                    # runtime before loading Sherpa, instead of accidentally loading System32.
                    import ctypes
                    from importlib.util import find_spec
                    directory=Path(find_spec('onnxruntime').origin).parent/'capi'
                    self.dll_handles.append(os.add_dll_directory(str(directory)))
                    self.dll_handles.append(ctypes.WinDLL(str(directory/'onnxruntime.dll')))
                import sherpa_onnx
                self.model=sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=str(self.directory/'encoder.int8.onnx'),decoder=str(self.directory/'decoder.int8.onnx'),
                    joiner=str(self.directory/'joiner.int8.onnx'),tokens=str(self.directory/'tokens.txt'),
                    num_threads=min(4,os.cpu_count() or 2),model_type='nemo_transducer',provider='cpu')
                self.device='cpu';return self.model
            try:
                from faster_whisper import WhisperModel
                import ctranslate2
            except (ImportError,OSError):
                raise SpeechError('Lokale Whisper-Bibliotheken fehlen oder sind nicht verfügbar. Texteingabe ist weiterhin möglich.') from None
            use_cuda=self.requested_device!='cpu' and ctranslate2.get_cuda_device_count()>0
            self.device='cuda' if use_cuda else 'cpu'
            self.progress('Whisper-Modell laden · '+('GPU' if use_cuda else 'CPU'))
            try:
                self.model=WhisperModel(str(self.directory),device=self.device,compute_type='int8_float16' if use_cuda else 'int8',cpu_threads=min(4,os.cpu_count() or 2),num_workers=1,local_files_only=True)
            except RuntimeError as error:
                if not use_cuda:raise
                self.warning='GPU nicht nutzbar: '+str(error)[:160]+'. CPU INT8 aktiv.'
                self.device='cpu'
                self.model=WhisperModel(str(self.directory),device='cpu',compute_type='int8',cpu_threads=min(4,os.cpu_count() or 2),num_workers=1,local_files_only=True)
            self.measured('model_load_'+self.device,load_started)
        return self.model

    def download_model(self):
        """Explicit setup action only; recording and model loading stay offline."""
        if self.engine!='whisper':
            raise SpeechError('Parakeet benötigt separat bereitgestellte lokale Modelldateien; Whisper auswählen.')
        if not self.lock.acquire(blocking=False):
            raise SpeechError('Eine Spracherkennung oder ein Modelldownload läuft bereits.')
        try:
            try:
                from faster_whisper import download_model
            except (ImportError,OSError):
                raise SpeechError('Whisper-Bibliotheken fehlen. Die optionale Sprachkomponente zuerst einrichten.') from None
            self.progress('Whisper-Modell herunterladen')
            self.directory.mkdir(parents=True,exist_ok=True)
            download_model(self.name,output_dir=str(self.directory),cache_dir=str(self.root/'download-cache'),use_auth_token=False)
            if not (self.directory/'model.bin').is_file():
                raise SpeechError('Modelldownload unvollständig; erneut ausdrücklich starten.')
            self.progress('Modelldateien bereit')
            return {'model':self.name,'state':'ready'}
        finally:
            self.lock.release()

    def recognize(self,audio):
        model=self.load()
        self.progress('Sprache erkennen · '+('GPU' if self.device=='cuda' else 'CPU'))
        inference_started=time.perf_counter()
        if self.engine=='parakeet':
            stream=model.create_stream();stream.accept_waveform(16000,audio);model.decode_stream(stream)
            return stream.result.text.strip()
        courses=self.courses()
        names=list(dict.fromkeys([a.upper() for c in courses for a in c.get('aliases',[]) if len(a)>1]+[c['title'] for c in courses]))
        hotwords=', '.join(names)[:1400]
        bindings='; '.join(', '.join(a.upper() for a in c.get('aliases',[]) if len(a)>1)+': '+c['title']
                          if any(len(a)>1 for a in c.get('aliases',[])) else c['title'] for c in courses)[:900]
        prompt='Deutsch und English im Studium. Vorbereitung, Erinnerung, morgen. Setup Session, lecture, assignment, deadline.'
        if bindings:prompt+=' Kursnamen und Kürzel: '+bindings
        # German is the initial default; detect each segment's spoken language and
        # transcribe rather than translate. Hotwords are hints, not substitutions.
        segments,_=model.transcribe(audio,language='de',multilingual=True,beam_size=5,vad_filter=True,condition_on_previous_text=False,
                                   hotwords=hotwords,initial_prompt=prompt)
        result=' '.join(segment.text.strip() for segment in segments if not (segment.no_speech_prob>.8 and segment.avg_logprob < -1)).strip()
        self.measured('recognition_'+self.device,inference_started)
        return result

    def transcribe(self, raw):
        if not raw or len(raw) > MAX_AUDIO:
            raise SpeechError('Aufnahme leer oder größer als 16 MiB.')
        if not self.lock.acquire(blocking=False):
            raise SpeechError('Eine Transkription läuft bereits. Bitte kurz warten.')
        try:
            started=time.perf_counter()
            self.started=started
            self.timings={}
            self.progress('Aufnahme dekodieren')
            import av
            import numpy as np
            chunks, samples = [], 0
            with av.open(io.BytesIO(raw)) as container:
                if not container.streams.audio:
                    raise SpeechError('Keine Audiospur gefunden.')
                resampler = av.AudioResampler(format='flt', layout='mono', rate=16000)
                for frame in container.decode(audio=0):
                    for converted in resampler.resample(frame):
                        samples += converted.samples
                        if samples > MAX_SECONDS * 16000:
                            raise SpeechError('Aufnahme länger als 60 Sekunden. Bitte kürzer aufnehmen.')
                        chunks.append(converted.to_ndarray().flatten())
                for converted in resampler.resample(None):
                    samples += converted.samples
                    chunks.append(converted.to_ndarray().flatten())
            if not chunks or samples > MAX_SECONDS * 16000:
                raise SpeechError('Aufnahme leer oder länger als 60 Sekunden.')
            audio = np.concatenate(chunks)
            if not np.isfinite(audio).all():
                raise SpeechError('Aufnahme enthält ungültige Audiodaten. Bitte erneut aufnehmen.')
            # This only excludes digital silence (including a constant DC offset).
            # A volume threshold would also discard quiet, intelligible speech.
            if float(np.max(np.abs(audio - np.mean(audio)))) <= 1e-7:
                raise SpeechError('Keine Sprache aufgenommen. Bitte Mikrofon prüfen oder Text eingeben.')
            self.measured('decode',started)
            active=self.effective
            try:
                text=active.recognize(audio)
            except Exception as error:
                if active is not self or (self.engine=='whisper' and self.name=='base' and self.device=='cpu'):
                    raise
                reason=' '.join(str(error).split())[:240]
                failure=f'{self.engine}/{self.name}/{self.device} fehlgeschlagen ({type(error).__name__}: {reason})'
                logging.getLogger(__name__).warning('Lokale STT-Engine: %s',failure)
                self.warning=failure+'; Whisper CPU-Fallback wird versucht.'
                self.progress('CPU-Fallback vorbereiten')
                # The failed primary will no longer be used once fallback succeeds.
                # Release its potentially large CPU/GPU model before loading another.
                self.model=None
                active=SpeechService(self.courses,engine='whisper',name=self.name if self.device=='cuda' else 'base',device='cpu',data_dir=self.root.parent)
                active.root=self.root
                active.directory=self.root/active.name
                active._progress_parent=weakref.ref(self)
                text=active.recognize(audio)
                # Cache only a working fallback; subsequent recordings avoid a known
                # broken engine, while status can still show the user's configuration.
                self.fallback=active
                self.warning=failure+'; Whisper CPU-Fallback aktiv.'
            if not text:
                raise SpeechError('Keine verständliche Sprache erkannt. Bitte erneut aufnehmen oder Text eingeben.')
            from .decision import candidates
            self.progress('Transkript fertig')
            return {'text': text,'seconds':round(samples/16000,1),'model':active.name if active.engine=='whisper' else 'parakeet-tdt-0.6b-v3',
                    'engine':active.engine,'device':active.device,'latency':round(time.perf_counter()-started,2),'warning':self.warning or active.warning,
                    'configured_engine':self.engine,'configured_model':self.name,'configured_device':self.requested_device,'timings':dict(self.timings),
                    'course_suggestions':[m.model_dump() for m in candidates(text,self.courses())]}
        except SpeechError:
            self.progress('Spracherkennung fehlgeschlagen')
            raise
        except Exception as error:
            self.progress('Spracherkennung fehlgeschlagen')
            raise SpeechError(f'Lokale Transkription fehlgeschlagen ({type(error).__name__}): {error}. Texteingabe funktioniert weiterhin.') from None
        finally:
            self.started=None
            self.lock.release()
