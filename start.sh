#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
stopping=''
for argument in "$@"; do
case "$argument" in
  --help|-h) echo 'StudyCenter Public ICE: sh start.sh [--setup-only] [--no-browser] [--stop] [--update] [--data-dir folder]'; exit 0;;
  --stop|--quit) stopping=1;;
esac
done
python=''
if [ -f "$root/.local/python-path.txt" ]; then IFS= read -r python < "$root/.local/python-path.txt" || true; fi
if [ -x "$python" ] && ! "$python" -I -c 'import sys,struct; assert sys.version_info[:2] == (3,12) and struct.calcsize("P") == 8' >/dev/null 2>&1; then python=''; fi
if [ ! -x "$python" ] && [ -x "$root/.venv/bin/python" ]; then
  if "$root/.venv/bin/python" -I -c 'import sys,struct; assert sys.version_info[:2] == (3,12) and struct.calcsize("P") == 8' >/dev/null 2>&1; then python="$root/.venv/bin/python"; fi
fi
if [ ! -x "$python" ]; then
  if [ -n "$stopping" ]; then echo 'No prepared Public environment. Nothing started or downloaded.'; exit 0; fi
  if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
    echo 'This starter supports Linux x64 (Ubuntu 24.04) and CPython 3.12.' >&2; exit 1
  fi
  for directory in "$root/.local" "$root/.local/tools" "$root/.local/tools/uv-0.8.22" "$root/.local/python" "$root/.local/uv-cache" "$root/.local/bin"; do
    if [ -L "$directory" ] || { [ -e "$directory" ] && [ ! -d "$directory" ]; }; then echo 'Linked setup directory refused.' >&2; exit 1; fi
    mkdir -p "$directory"
  done
  uv="$root/.local/tools/uv-0.8.22/uv"
  if [ ! -x "$uv" ]; then
    for tool in curl sha256sum tar; do command -v "$tool" >/dev/null 2>&1 || { echo "First setup needs $tool." >&2; exit 1; }; done
    archive="$root/.local/tools/uv-0.8.22-linux.tar.gz"
    if [ ! -f "$archive" ]; then
      curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 'https://github.com/astral-sh/uv/releases/download/0.8.22/uv-x86_64-unknown-linux-gnu.tar.gz' --output "$archive.$$.partial"
      mv "$archive.$$.partial" "$archive"
    fi
    if ! printf '%s  %s\n' '741ff1f5742c5a4a25d2f829e8395355e43f7a5ae2ebc6368e9ae2df0efb69cf' "$archive" | sha256sum --check --status; then
      rm -f "$archive"
      echo 'Damaged own uv archive removed. Retry the same starter.' >&2; exit 1
    fi
    tar -xzf "$archive" --strip-components=1 -C "$root/.local/tools/uv-0.8.22" uv-x86_64-unknown-linux-gnu/uv
  fi
  export UV_CACHE_DIR="$root/.local/uv-cache" UV_PYTHON_INSTALL_DIR="$root/.local/python" UV_PYTHON_BIN_DIR="$root/.local/bin" UV_PYTHON_INSTALL_BIN=0 UV_PYTHON_CPYTHON_BUILD=20250918
  unset VIRTUAL_ENV PYTHONHOME PYTHONPATH
  unset UV_PYTHON_INSTALL_MIRROR UV_PYTHON_DOWNLOADS_JSON_URL UV_INSECURE_HOST UV_CONFIG_FILE UV_PYTHON_PREFERENCE
  if ! python=$("$uv" --no-config python find --system --no-python-downloads 'cpython@3.12' 2>/dev/null); then
    "$uv" --no-config python install --no-bin --no-registry 3.12.11
    python=$("$uv" --no-config python find --managed-python 3.12.11)
  fi
fi
unset VIRTUAL_ENV PYTHONHOME PYTHONPATH
export PYTHONNOUSERSITE=1
uv="$root/.local/tools/uv-0.8.22/uv"
if [ -f "$root/studycenter_ice/platform/setup.py" ]; then
  exec "$python" -I "$root/studycenter_ice/platform/setup.py" --root "$root" --source "$root" --uv "$uv" "$@"
else
  exec "$python" -I "$root/setup.py" --root "$root" --manifest "$root/slim-manifest.json" --uv "$uv" "$@"
fi
