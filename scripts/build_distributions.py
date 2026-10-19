"""Package reviewed, tracked ICE source. Never executes an installer."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
import zipfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
NAME = 'StudyCenter-Public-ICE'


def build(iscc=None):
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    stage = ROOT / 'build/distribution' / uuid4().hex / NAME
    stage.mkdir(parents=True, exist_ok=False)
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    selected = [name for name in names if name and not name.startswith(('.github/', 'tests/')) and name != 'AGENTS.md']
    for name in selected:
        source = ROOT / name
        if source.is_symlink():
            raise ValueError('Links are not distribution source: ' + name)
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    archives = []
    windows = output / 'studycenter-public-ice-0.1.0b1-windows-x86_64-portable.zip'
    with zipfile.ZipFile(windows, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in selected:
            archive.write(stage / name, NAME + '/' + name)
    archives.append(windows)
    linux = output / 'studycenter-public-ice-0.1.0b1-linux-x86_64.tar.gz'
    with tarfile.open(linux, 'w:gz') as archive:
        for name in selected:
            info = archive.gettarinfo(str(stage / name), NAME + '/' + name)
            info.mode = 0o755 if name.endswith('.sh') else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            with (stage / name).open('rb') as stream:
                archive.addfile(info, stream)
    archives.append(linux)
    if iscc:
        subprocess.run([str(iscc), '/DSourceRoot=' + str(stage), '/DOutputDir=' + str(output),
                        str(ROOT / 'packaging/windows/installer.iss')], check=True)
        archives.append(output / 'studycenter-public-ice-0.1.0b1-windows-x86_64-setup.exe')
    lines = []
    for archive in archives:
        with archive.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        lines.append(digest + '  ' + archive.name)
        print(f'{archive.name}: {archive.stat().st_size} bytes')
    (output / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--iscc', type=Path)
    build(parser.parse_args().iscc)
