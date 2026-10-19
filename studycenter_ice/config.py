import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
VIENNA = ZoneInfo("Europe/Vienna")


def default_data_dir():
    """A fresh public installation never discovers any other app's profile."""
    explicit = os.getenv('STUDYCENTER_ICE_DATA_DIR')
    if explicit:
        return Path(explicit).expanduser().absolute()
    receipt_path = ROOT / '.local/installation.json'
    reject_linked_path(receipt_path)
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
            if (receipt.get('application') != 'StudyCenter.Public.ICE' or
                    Path(receipt['root']).resolve() != ROOT.resolve()):
                raise ValueError()
            if receipt.get('data_dir'):
                selected = Path(receipt['data_dir']).expanduser()
                if not selected.is_absolute():
                    raise ValueError()
                reject_linked_path(selected)
                return selected.absolute()
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            raise ValueError('StudyCenter-ICE-Installation hat keinen gültigen eigenen Datenpfad.') from None
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
    else:
        base = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    return (base / 'StudyCenter-Public-ICE').absolute()


def local_profile_path(relative, *, markers=(), data_dir=None):
    """Resolve only this instance's profile. Legacy markers are never inspected."""
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Profilpfad muss innerhalb dieser StudyCenter-ICE-Instanz bleiben.')
    root = Path(data_dir).expanduser().absolute() if data_dir is not None else default_data_dir()
    reject_linked_path(root / 'profiles' / relative)
    base = root.resolve() / 'profiles'
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError('Profilpfad muss innerhalb dieser StudyCenter-ICE-Instanz bleiben.')
    return path


def reject_linked_path(path):
    for candidate in (Path(path), *Path(path).parents):
        if candidate.is_symlink() or (candidate.exists() and
                getattr(candidate.stat(follow_symlinks=False), 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise ValueError('StudyCenter-ICE-Instanz darf keine verknüpften Datenpfade verwenden.')


def claim_data_directory(directory):
    """Refuse links and pre-existing unowned records before creating anything."""
    raw = Path(directory).expanduser().absolute()
    marker = raw / 'instance.json'
    reject_linked_path(marker)
    if marker.exists():
        try:
            value = json.loads(marker.read_text(encoding='utf-8'))
            if value.get('application') != 'StudyCenter.Public.ICE':
                raise ValueError()
            uuid.UUID(value['instance_id'])
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            raise ValueError('Datenordner gehört nicht zu einer gültigen StudyCenter-ICE-Instanz.') from None
    else:
        if raw.exists() and (not raw.is_dir() or any(raw.iterdir())):
            raise ValueError('Datenordner enthält fremde Daten; für diese Instanz einen neuen leeren Ordner wählen.')
        raw.mkdir(parents=True, exist_ok=True)
        with marker.open('x', encoding='utf-8') as handle:
            json.dump({'application': 'StudyCenter.Public.ICE', 'instance_id': str(uuid.uuid4())}, handle)
    return raw.resolve()


@dataclass
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    llm_base_url: str = field(default_factory=lambda: os.getenv("STUDYCENTER_ICE_LLM_BASE_URL", "").rstrip("/"))
    llm_model: str = field(default_factory=lambda: os.getenv("STUDYCENTER_ICE_LLM_MODEL", ""))
    llm_api_key: str = field(default_factory=lambda: os.getenv("STUDYCENTER_ICE_LLM_API_KEY", ""), repr=False)
    teachcenter_url: str = field(default_factory=lambda: os.getenv("STUDYCENTER_ICE_TEACHCENTER_URL", "https://tc.tugraz.at/main/"))
    ocr_lang: str = field(default_factory=lambda: os.getenv("STUDYCENTER_ICE_OCR_LANG", "eng"))
    tesseract: str | None = field(default_factory=lambda: os.getenv("STUDYCENTER_ICE_TESSERACT_CMD") or None)
    model_provider: str = "chat-completions"
    oauth_model: str = ""
    oauth_probe_passed: bool = False
    stt_engine: str = field(default_factory=lambda: os.getenv('STUDYCENTER_ICE_STT_ENGINE', 'whisper'))
    stt_model: str = field(default_factory=lambda: os.getenv('STUDYCENTER_ICE_STT_MODEL', 'small'))
    stt_device: str = field(default_factory=lambda: os.getenv('STUDYCENTER_ICE_STT_DEVICE', 'auto'))
    chatgpt: object = field(default=None, repr=False)

    def __post_init__(self):
        self.data_dir = claim_data_directory(self.data_dir)
        reject_linked_path(self.data_dir / 'documents')
        (self.data_dir / "documents").mkdir(exist_ok=True)
        path = self.data_dir / "settings.json"
        reject_linked_path(path)
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.llm_base_url = saved.get('llm_base_url', self.llm_base_url).rstrip('/')
            self.llm_model = saved.get('llm_model', self.llm_model)
            self.teachcenter_url = saved.get('teachcenter_url', self.teachcenter_url)
            self.ocr_lang = saved.get('ocr_lang', self.ocr_lang)
            self.tesseract = saved.get('tesseract', self.tesseract)
            self.model_provider = saved.get("model_provider", self.model_provider)
            self.oauth_model = saved.get("oauth_model", "")
            self.oauth_probe_passed = saved.get("oauth_probe_passed", False)
            self.stt_engine = saved.get('stt_engine', self.stt_engine)
            self.stt_model = saved.get('stt_model', self.stt_model)
            self.stt_device = saved.get('stt_device', self.stt_device)
        self.validate_speech(self.stt_engine, self.stt_model, self.stt_device)
        from .chatgpt import SecureJSONStore
        self._model_credentials = SecureJSONStore(local_profile_path('auth/model.dpapi', data_dir=self.data_dir), service='model')
        self.llm_api_key = self.llm_api_key or self._model_credentials.read().get('api_key', '')

    @staticmethod
    def validate_speech(engine, model, device):
        if engine not in ('whisper', 'parakeet') or model not in ('base', 'small') or device not in ('auto', 'cpu', 'cuda'):
            raise ValueError('Ungültige Spracherkennung: Engine whisper/parakeet, Modell base/small und Gerät auto/cpu/cuda wählen.')

    @property
    def model_ready(self):
        if self.model_provider == "chatgpt":
            try:
                status = self.chatgpt.status() if self.chatgpt else {}
            except (RuntimeError, OSError, ValueError):
                return False
            return bool(status.get("connected") and status.get("plan_enabled") and self.oauth_model and self.oauth_probe_passed)
        return bool(self.llm_base_url and self.llm_model)

    def save(self):
        self.validate_speech(self.stt_engine, self.stt_model, self.stt_device)
        path = self.data_dir / "settings.json"
        reject_linked_path(path)
        temporary = path.with_suffix(".tmp")
        reject_linked_path(temporary)
        saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        # Retired scheduling switches remain inert and are removed on the next save.
        for legacy in ('auto_sync','daily_time'):saved.pop(legacy,None)
        saved.pop('llm_api_key', None)
        self._model_credentials.write({'api_key': self.llm_api_key})
        saved.update(llm_base_url=self.llm_base_url, llm_model=self.llm_model,
                     teachcenter_url=self.teachcenter_url, ocr_lang=self.ocr_lang, tesseract=self.tesseract)
        saved.update({"model_provider": self.model_provider, "oauth_model": self.oauth_model,
                      "oauth_probe_passed": self.oauth_probe_passed,
                      'stt_engine': self.stt_engine, 'stt_model': self.stt_model, 'stt_device': self.stt_device})
        temporary.write_text(json.dumps(saved), encoding="utf-8")
        temporary.replace(path)


def load_settings():
    return Settings()
