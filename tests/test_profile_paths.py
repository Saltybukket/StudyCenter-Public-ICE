from pathlib import Path

import pytest

from studycenter_ice.chatgpt import CredentialStore
from studycenter_ice.config import local_profile_path
from studycenter_ice.google_calendar import GoogleAuth
from studycenter_ice.mail import MailCredentials
from studycenter_ice.mcp import credential_path
from studycenter_ice.tunnel import local_directory


def test_each_component_uses_only_its_explicit_public_dataset(tmp_path):
    a, b = tmp_path / 'instance-a', tmp_path / 'instance-b'
    for data in (a, b):
        paths = [CredentialStore(data_dir=data).path, GoogleAuth(data).path,
                 MailCredentials(data_dir=data).path, credential_path(data), local_directory(data)]
        assert all(path.is_relative_to(data.resolve()) for path in paths)
        assert not data.exists()
    assert credential_path(a) != credential_path(b)


def test_profile_does_not_follow_markers_from_other_locations(tmp_path, monkeypatch):
    monkeypatch.setenv('STUDYCENTER_ICE_DATA_DIR', str(tmp_path / 'own-instance'))
    expected = tmp_path / 'own-instance/profiles/auth/accounts.dpapi'
    assert local_profile_path('auth/accounts.dpapi', markers=('anything',)) == expected.resolve()
    assert not expected.exists()


def test_profile_rejects_escape_and_absolute_path(tmp_path):
    for relative in ('../elsewhere', str(tmp_path / 'absolute')):
        with pytest.raises(ValueError):
            local_profile_path(relative, data_dir=tmp_path / 'own')
