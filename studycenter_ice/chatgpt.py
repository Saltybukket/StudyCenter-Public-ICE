"""Local Sign in with ChatGPT; no Codex credentials or alternative billing route."""
import asyncio
import copy
import contextlib
import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
import portalocker

from .config import local_profile_path, reject_linked_path

ISSUER = "https://auth.openai.com"
API = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
TERMINAL_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"}


class ChatGPTError(RuntimeError):
    """Safe, machine-readable errors only; never response bodies or credentials."""


def error_code(value, fallback="unknown_error"):
    error = value.get("error", value) if isinstance(value, dict) else {}
    code = error.get("code", error.get("type", fallback)) if isinstance(error, dict) else error
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", code) else fallback


def require_ok(response):
    if response.is_error:
        try:
            code = error_code(response.json(), f"http_{response.status_code}")
        except ValueError:
            code = f"http_{response.status_code}"
        raise ChatGPTError(code)


def dpapi(data, decrypt=False):
    # Windows CurrentUser encryption: copying this file does not disclose tokens.
    if os.name != "nt":
        raise ChatGPTError("windows_dpapi_required")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ChatGPTError("credential_encryption_failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


_SESSION_CREDENTIALS = {}


def windows_platform():
    return os.name == 'nt'


class SecureJSONStore:
    """Own-instance secrets: DPAPI, explicitly selected Secret Service, or memory.

    Linux defaults to process memory. It never silently falls back to plaintext
    or selects an arbitrary keyring plugin configured by another application.
    """
    def __init__(self, path, *, service='credentials', default_factory=dict):
        self.path = Path(path).expanduser().resolve()
        self.default_factory = default_factory
        instance = hashlib.sha256(str(self.path.parent.parent.resolve()).encode()).hexdigest()[:24]
        # Managed secrets are at most auth/file below profiles below the dataset.
        # Do not search beyond that bounded ancestry for unrelated app profiles.
        for parent in list(self.path.parents)[:3]:
            marker = parent / 'instance.json'
            reject_linked_path(marker)
            if not marker.exists():
                continue
            try:
                value = json.loads(marker.read_text(encoding='utf-8'))
                if value.get('application') != 'StudyCenter.Public.ICE':
                    raise ValueError()
                instance = str(uuid.UUID(value['instance_id']))
            except (ValueError, TypeError, KeyError, AttributeError, OSError):
                raise ChatGPTError('credential_instance_marker_invalid') from None
            break
        self.namespace = f'StudyCenter-Public-ICE/{instance}/{service}'
        self.account = hashlib.sha256(str(self.path).encode()).hexdigest()
        self.backend = 'dpapi' if windows_platform() else os.getenv('STUDYCENTER_ICE_CREDENTIAL_BACKEND', 'session')
        if self.backend not in ('dpapi', 'session', 'keyring'):
            raise ChatGPTError('unsupported_credential_backend')
        self.keyring = None
        if self.backend == 'keyring':
            try:
                from keyring.backends.SecretService import Keyring
                self.keyring = Keyring()
                if self.keyring.priority <= 0:
                    raise ValueError('unavailable')
            except Exception:
                raise ChatGPTError('secure_keyring_unavailable_use_session_or_unlock_secret_service') from None

    def storage_status(self):
        return {'backend': self.backend, 'persistent': self.backend != 'session',
                'message': 'Nur für diesen App-Lauf gespeichert.' if self.backend == 'session' else 'Lokal geschützt gespeichert.'}

    def read(self):
        try:
            if self.backend == 'session':
                return copy.deepcopy(_SESSION_CREDENTIALS.get((self.namespace, self.account), self.default_factory()))
            if self.backend == 'keyring':
                raw = self.keyring.get_password(self.namespace, self.account)
                return json.loads(raw) if raw is not None else self.default_factory()
            return json.loads(dpapi(self.path.read_bytes(), decrypt=True)) if self.path.exists() else self.default_factory()
        except Exception:
            raise ChatGPTError("credential_store_unreadable") from None

    def write(self, data):
        if self.backend == 'session':
            _SESSION_CREDENTIALS[(self.namespace, self.account)] = copy.deepcopy(data)
            return
        if self.backend == 'keyring':
            try:
                self.keyring.set_password(self.namespace, self.account, json.dumps(data))
            except Exception:
                raise ChatGPTError('secure_keyring_write_failed') from None
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = dpapi(json.dumps(data).encode())
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("xb") as file:
                file.write(encrypted)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def lock(self):
        if self.backend == 'session':
            return contextlib.nullcontext()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return portalocker.Lock(str(self.path.with_suffix(".lock")), timeout=0)


class CredentialStore(SecureJSONStore):
    def __init__(self, path=None, *, data_dir=None):
        super().__init__(path if path is not None else local_profile_path('auth/accounts.dpapi', data_dir=data_dir),
                         service='chatgpt', default_factory=lambda: {'host_id': '', 'active': '', 'profiles': []})


class ChatGPTAuth:
    def __init__(self, store=None, client_factory=None):
        self.store = store or CredentialStore()
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=30, trust_env=False))
        self.gate = asyncio.Lock()
        self.pending = None
        self.models = []
        self.message = ""

    def profile(self, data=None):
        data = data or self.store.read()
        return next((p for p in data["profiles"] if p["client_id"] == data["active"]), None)

    def status(self):
        data = self.store.read()
        profile = self.profile(data)
        return {"connected": bool(profile and profile.get("access_token")),
                "plan_enabled": bool(profile and "chatgpt.tokens.use.direct" in profile.get("scopes", [])),
                "account": (profile.get("email") or profile.get("subject", "")) if profile else "",
                "active": data["active"], "models": self.models, "message": self.message,
                'credential_storage': self.store.storage_status(),
                "profiles": [{"client_id": p["client_id"], "label": (p.get("email") or p["subject"]) + " · " + p["client_id"][-8:]} for p in data["profiles"]]}

    async def metadata(self, client):
        response = await client.get(ISSUER + "/.well-known/openid-configuration")
        require_ok(response)
        value = response.json()
        if value.get("issuer") != ISSUER:
            raise ChatGPTError("invalid_issuer")
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri", "revocation_endpoint"):
            if field in value and (urlsplit(value[field]).scheme != "https" or urlsplit(value[field]).netloc != "auth.openai.com"):
                raise ChatGPTError("invalid_auth_endpoint")
        return value

    async def begin(self, redirect_uri, selected=""):
        if urlsplit(redirect_uri).hostname != "127.0.0.1" or urlsplit(redirect_uri).scheme != "http" or urlsplit(redirect_uri).path != "/auth/callback":
            raise ChatGPTError("invalid_loopback_callback")
        async with self.gate:
            with self.store.lock():
                data = self.store.read()
                if not data["host_id"]:
                    data["host_id"] = "urn:uuid:" + str(uuid.uuid4())
                    self.store.write(data)
                profile = next((p for p in data["profiles"] if p["client_id"] == selected), None)
                if selected and not profile:
                    raise ChatGPTError("unknown_account")
            async with self.client_factory() as client:
                metadata = await self.metadata(client)
            verifier = secrets.token_urlsafe(64)
            self.pending = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32), "verifier": verifier,
                            "redirect_uri": redirect_uri, "created": time.time(), "profile": profile, "metadata": metadata}
            params = {"client_id": profile["client_id"] if profile else "dynamic_agent_client", "ext_agent_host_id": data["host_id"],
                      "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES, "resource": API,
                      "state": self.pending["state"], "nonce": self.pending["nonce"], "code_challenge_method": "S256",
                      "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()}
            if profile:
                if profile.get("id_token"):
                    params["id_token_hint"] = profile["id_token"]
                if profile.get("email"):
                    params["login_hint"] = profile["email"]
            else:
                params["agent_name_hint"] = "StudyCenter-ICE"
            return metadata["authorization_endpoint"] + "?" + urlencode(params)

    async def callback(self, params):
        async with self.gate:
            pending = self.pending
            if not pending or time.time() - pending["created"] > 600 or not secrets.compare_digest(params.get("state", ""), pending["state"]):
                raise ChatGPTError("invalid_or_expired_state")
            self.pending = None  # Codes and successful state are consumed exactly once.
            if params.get("error"):
                raise ChatGPTError(error_code({"error": params["error"]}, "authorization_declined"))
            previous = pending["profile"]
            client_id = params.get("client_id") or (previous["client_id"] if previous else "")
            if not client_id or client_id == "dynamic_agent_client" or (previous and client_id != previous["client_id"]):
                raise ChatGPTError("invalid_issued_client")
            if not params.get("code"):
                raise ChatGPTError("missing_authorization_code")
            async with self.client_factory() as client:
                response = await client.post(pending["metadata"]["token_endpoint"], data={"grant_type": "authorization_code", "client_id": client_id,
                                            "code": params["code"], "code_verifier": pending["verifier"], "redirect_uri": pending["redirect_uri"], "resource": API})
                require_ok(response)
                tokens = response.json()
                keys = await client.get(pending["metadata"]["jwks_uri"])
                require_ok(keys)
                try:
                    header = jwt.get_unverified_header(tokens["id_token"])
                    if header.get("alg") != "RS256":
                        raise ValueError("algorithm")
                    key = next(k for k in keys.json()["keys"] if k.get("kid") == header.get("kid") and k.get("kty") == "RSA")
                    identity = jwt.decode(tokens["id_token"], jwt.PyJWK(key, algorithm="RS256").key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                                          options={"require": ["exp", "iat", "sub", "aud", "iss", "nonce"]})
                    if not secrets.compare_digest(identity["nonce"], pending["nonce"]) or not identity["sub"]:
                        raise ValueError("nonce")
                    if (identity.get("azp") and identity["azp"] != client_id) or (isinstance(identity["aud"], list) and len(identity["aud"]) > 1 and identity.get("azp") != client_id):
                        raise ValueError("authorized party")
                    if previous and (identity["sub"] != previous["subject"] or identity["iss"] != previous["issuer"]):
                        raise ValueError("account")
                except (jwt.PyJWTError, KeyError, ValueError, StopIteration, TypeError):
                    raise ChatGPTError("invalid_id_token") from None
            profile = {"client_id": client_id, "subject": identity["sub"], "issuer": identity["iss"], "email": identity.get("email", "")}
            profile.update(self.token_fields(tokens))
            with self.store.lock():
                data = self.store.read()
                existing = next((p for p in data["profiles"] if p["client_id"] == client_id), None)
                if existing and existing["subject"] != identity["sub"]:
                    raise ChatGPTError("account_registration_mismatch")
                data["profiles"] = [p for p in data["profiles"] if p["client_id"] != client_id] + [profile]
                data["active"] = client_id
                self.store.write(data)
            self.models = []
            self.message = "Verbunden" if "chatgpt.tokens.use.direct" in profile["scopes"] else "Angemeldet, aber ChatGPT-Abonnementnutzung nicht freigegeben (chatgpt.tokens.use.direct fehlt)."

    def token_fields(self, tokens, previous=None):
        previous = previous or {}
        if not tokens.get("access_token") or tokens.get("token_type", "").lower() != "bearer" or not tokens.get("refresh_token"):
            raise ChatGPTError("invalid_token_response")
        return {"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"], "id_token": tokens.get("id_token", previous.get("id_token", "")),
                "scopes": tokens["scope"].split() if "scope" in tokens else previous.get("scopes", []),
                "expires_at": time.time() + int(tokens["expires_in"]), "earliest_refresh_at": tokens.get("earliest_refresh_at", 0)}

    async def access_token(self):
        async with self.gate:
            with self.store.lock():
                data = self.store.read()
                profile = self.profile(data)
                if not profile or not profile.get("access_token"):
                    raise ChatGPTError("chatgpt_login_required")
                if "chatgpt.tokens.use.direct" not in profile.get("scopes", []):
                    raise ChatGPTError("chatgpt_plan_permission_missing")
                if profile["expires_at"] <= time.time() + 90:
                    async with self.client_factory() as client:
                        metadata = await self.metadata(client)
                        response = await client.post(metadata["token_endpoint"], data={"grant_type": "refresh_token", "client_id": profile["client_id"], "refresh_token": profile["refresh_token"], "resource": API})
                        try:
                            require_ok(response)
                        except ChatGPTError as error:
                            if str(error) in TERMINAL_REFRESH:
                                self.clear_tokens(profile)
                                self.store.write(data)
                                self.models = []
                            raise
                        profile.update(self.token_fields(response.json(), profile))
                        self.store.write(data)  # Rotating refresh token and access expiry are one atomic write.
                        if "chatgpt.tokens.use.direct" not in profile["scopes"]:
                            raise ChatGPTError("chatgpt_plan_permission_missing")
                return profile["access_token"]

    async def list_models(self):
        token = await self.access_token()
        async with self.client_factory() as client:
            response = await client.get(API + "/models", headers={"Authorization": "Bearer " + token})
            require_ok(response)
            self.models = [{"slug": m["slug"], "display_name": m["display_name"]} for m in response.json()["models"] if m.get("visibility") == "list"]
        return self.models

    def clear_tokens(self, profile):
        for key in ("access_token", "refresh_token", "id_token", "expires_at", "scopes", "earliest_refresh_at"):
            profile.pop(key, None)

    async def logout(self):
        async with self.gate:
            self.pending = None
            with self.store.lock():
                data = self.store.read()
                profile = self.profile(data)
                confirmed = False
                if profile and profile.get("refresh_token"):
                    try:
                        async with self.client_factory() as client:
                            metadata = await self.metadata(client)
                            for attempt in range(3):
                                try:
                                    response = await client.post(metadata["revocation_endpoint"], data={"token": profile["refresh_token"], "token_type_hint": "refresh_token", "client_id": profile["client_id"]})
                                    confirmed = response.status_code == 200
                                    if confirmed or response.status_code < 500:
                                        break
                                except httpx.HTTPError:
                                    pass
                                if attempt < 2:
                                    await asyncio.sleep(0.5 * 2 ** attempt)
                    except (httpx.HTTPError, ChatGPTError, KeyError):
                        pass
                if profile:
                    self.clear_tokens(profile)
                self.store.write(data)
            self.models = []
            self.message = "Abgemeldet." if confirmed else "Lokal abgemeldet. Remote-Widerruf nicht bestätigt; App gegebenenfalls in ChatGPT-Einstellungen trennen."
