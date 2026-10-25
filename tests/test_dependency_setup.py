"""The real clone setup must use build requirements compatible with its lock."""
import importlib.util
from pathlib import Path
import sys
import tomllib

from packaging.requirements import Requirement
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("update", [False, True])
def test_source_build_dependency_remains_compatible_with_current_lock(tmp_path, monkeypatch, update):
    spec = importlib.util.spec_from_file_location("dependency_setup", ROOT / "studycenter_ice/platform/setup.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    source = tmp_path / "source with spaces"
    source.mkdir()
    (source / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    platform = "windows" if sys.platform == "win32" else "linux"
    lock = source / f"requirements-{platform}.lock"
    lock.write_bytes((ROOT / lock.name).read_bytes())
    locked = next(Requirement(line) for line in lock.read_text().splitlines()
                  if line.lower().startswith("setuptools=="))
    locked_version = next(iter(locked.specifier)).version
    declared = next(Requirement(item) for item in tomllib.loads(
        (source / "pyproject.toml").read_text())["build-system"]["requires"]
        if Requirement(item).name == "setuptools")
    assert declared.specifier.contains(locked_version)

    calls = []
    monkeypatch.setattr(setup, "ensure_uv", lambda root, uv: uv)
    monkeypatch.setattr(setup, "select_python", lambda *args: (Path(sys.executable), "system"))
    monkeypatch.setattr(setup, "environment_usable", lambda *args: True)
    monkeypatch.setattr(setup, "application_ready", lambda *args: False)
    monkeypatch.setattr(setup, "run", lambda command, **kwargs:
                        calls.append([str(item) for item in command]))
    setup.ensure_environment(tmp_path / "own", "uv", source=source, update=update)

    build_install = next(call for call in calls if any(
        item == "setuptools" or item.startswith("setuptools==") for item in call))
    requested = next(Requirement(item) for item in build_install if
                     item == "setuptools" or item.startswith("setuptools=="))
    assert requested.specifier.contains(locked_version), (
        f"clone setup requests {requested}, but its constraint requires {locked}"
    )
    assert build_install[build_install.index("--constraint") + 1] == lock.as_uri()
    assert any("--editable" in call for call in calls)
