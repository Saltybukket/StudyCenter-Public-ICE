@echo off
setlocal
chcp 65001 >nul
set "project_root=%~dp0"
set "bootstrap_root=%~dp0"
set "setup_prepare="
if "%~1"=="--prepare-target" (
    set "project_root=%~f2\"
    set "setup_prepare=1"
)
set "setup_stopping="
set "setup_help="
for %%A in (%*) do (
    if "%%~A"=="--stop" set "setup_stopping=1"
    if "%%~A"=="--quit" set "setup_stopping=1"
    if "%%~A"=="--help" set "setup_help=1"
    if "%%~A"=="-h" set "setup_help=1"
)
if defined setup_help (
    echo StudyCenter Public ICE: start.cmd [--setup-only] [--no-browser] [--stop] [--update] [--data-dir folder]
    echo Base setup uses its own environment. Browser, OCR and speech downloads are optional.
    exit /b 0
)
set "project_python="
if exist "%project_root%.local\python-path.txt" set /p "project_python="<"%project_root%.local\python-path.txt"
if defined project_python if exist "%project_python%" (
    "%project_python%" -I -c "import sys,struct; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8" >nul 2>nul
    if not errorlevel 1 goto python_ready
)
if exist "%project_root%.venv\Scripts\python.exe" (
    set "project_python=%project_root%.venv\Scripts\python.exe"
    "%project_root%.venv\Scripts\python.exe" -I -c "import sys,struct; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8" >nul 2>nul
    if not errorlevel 1 goto python_ready
)
if defined setup_stopping (
    echo No prepared Public environment. Nothing started or downloaded.
    exit /b 0
)
if /i "%PROCESSOR_ARCHITECTURE%"=="AMD64" goto architecture_ready
if /i "%PROCESSOR_ARCHITEW6432%"=="AMD64" goto architecture_ready
echo This starter supports Windows x64 and CPython 3.12.
exit /b 1
:architecture_ready
set "local_dir=%project_root%.local"
set "tools=%local_dir%\tools"
set "uv_dir=%tools%\uv-0.8.22"
set "STUDYCENTER_ICE_SETUP_ROOT=%project_root%"
powershell.exe -NoLogo -NoProfile -Command "try { $p=[IO.Path]::GetFullPath($env:STUDYCENTER_ICE_SETUP_ROOT); while ($p) { if ([IO.Directory]::Exists($p) -and ([IO.File]::GetAttributes($p) -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked setup path refused' }; $p=[IO.Path]::GetDirectoryName($p.TrimEnd('\')) }; foreach ($n in @('.local','.local\tools','.local\tools\uv-0.8.22','.local\python','.local\uv-cache','.local\bin')) { $p=[IO.Path]::Combine($env:STUDYCENTER_ICE_SETUP_ROOT,$n); if ([IO.File]::Exists($p) -or ([IO.Directory]::Exists($p) -and ([IO.File]::GetAttributes($p) -band [IO.FileAttributes]::ReparsePoint))) { throw 'Unsafe setup path' }; [void][IO.Directory]::CreateDirectory($p) } } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
if errorlevel 1 goto setup_error
set "uv=%uv_dir%\uv.exe"
if exist "%uv%" goto uv_ready
where curl.exe >nul 2>nul
if errorlevel 1 goto tools_error
where tar.exe >nul 2>nul
if errorlevel 1 goto tools_error
set "STUDYCENTER_ICE_SETUP_ARCHIVE=%tools%\uv-0.8.22-windows.zip"
if exist "%STUDYCENTER_ICE_SETUP_ARCHIVE%" goto check_uv
set "download_partial=%STUDYCENTER_ICE_SETUP_ARCHIVE%.%RANDOM%.partial"
echo Preparing the local setup tool. First setup needs internet.
curl.exe --fail --location --proto "=https" --proto-redir "=https" --tlsv1.2 "https://github.com/astral-sh/uv/releases/download/0.8.22/uv-x86_64-pc-windows-msvc.zip" --output "%download_partial%"
if errorlevel 1 goto setup_error
move /y "%download_partial%" "%STUDYCENTER_ICE_SETUP_ARCHIVE%" >nul
:check_uv
powershell.exe -NoLogo -NoProfile -Command "$s=$null; $h=$null; try { $s=[IO.File]::OpenRead($env:STUDYCENTER_ICE_SETUP_ARCHIVE); $h=[Security.Cryptography.SHA256]::Create(); $v=[BitConverter]::ToString($h.ComputeHash($s)).Replace('-','').ToLowerInvariant(); if ($v -ne '5049375aa2a5162f132b2c1cb992e25d42d47d934cab8c174dbe6f60973dcc12') { throw 'Official uv archive checksum mismatch.' } } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 } finally { if ($s) { $s.Dispose() }; if ($h) { $h.Dispose() } }"
if errorlevel 1 goto damaged_archive
tar.exe -xf "%STUDYCENTER_ICE_SETUP_ARCHIVE%" -C "%uv_dir%" uv.exe
if errorlevel 1 goto setup_error
:uv_ready
set "UV_CACHE_DIR=%local_dir%\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%local_dir%\python"
set "UV_PYTHON_BIN_DIR=%local_dir%\bin"
set "UV_PYTHON_INSTALL_BIN=0"
set "UV_PYTHON_CPYTHON_BUILD=20250918"
set "UV_PYTHON_INSTALL_MIRROR="
set "UV_PYTHON_DOWNLOADS_JSON_URL="
set "UV_INSECURE_HOST="
set "UV_CONFIG_FILE="
set "UV_PYTHON_PREFERENCE="
set "VIRTUAL_ENV="
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
set "python_selection=%local_dir%\bootstrap-python-%RANDOM%.txt"
"%uv%" --no-config python find --system --no-python-downloads "cpython@3.12" > "%python_selection%" 2>nul
if not errorlevel 1 goto selected_python
"%uv%" --no-config python install --no-bin --no-registry 3.12.11
if errorlevel 1 goto setup_error
"%uv%" --no-config python find --managed-python 3.12.11 > "%python_selection%"
if errorlevel 1 goto setup_error
:selected_python
set /p "project_python="<"%python_selection%"
del "%python_selection%" >nul 2>nul
:python_ready
set "uv=%project_root%.local\tools\uv-0.8.22\uv.exe"
set "PYTHONHOME="
set "PYTHONPATH="
set "VIRTUAL_ENV="
cd /d "%project_root%"
if defined setup_prepare goto prepare_target
if exist "%project_root%studycenter_ice\platform\setup.py" (
    "%project_python%" -I "%project_root%studycenter_ice\platform\setup.py" --root "%project_root%." --source "%project_root%." --uv "%uv%" %*
) else (
    "%project_python%" -I "%project_root%setup.py" --root "%project_root%." --manifest "%project_root%slim-manifest.json" --uv "%uv%" %*
)
exit /b %errorlevel%
:prepare_target
"%project_python%" -I "%bootstrap_root%setup.py" --root "%project_root%." --manifest "%bootstrap_root%slim-manifest.json" --uv "%uv%" %*
exit /b %errorlevel%
:damaged_archive
del "%STUDYCENTER_ICE_SETUP_ARCHIVE%" >nul 2>nul
echo Damaged own uv download removed. Retry start.cmd.
exit /b 1
:tools_error
echo Existing Windows curl.exe and tar.exe are required for first setup.
exit /b 1
:setup_error
echo Setup did not finish. Retry the same starter; existing Public data is retained.
exit /b 1
