@echo off
rem build.bat: build the Windows executables WhiteboardServer.exe, WhiteboardClient.exe
rem and WhiteboardClient-console.exe into dist\windows\.
rem
rem     scripts\build.bat               (uses "py -3", else "python"; or: set PYTHON=C:\Python311\python.exe)
rem
rem Steps:
rem   1. create a FRESH virtual environment .build_venv (deleted first) from a plain
rem      python.org Python. Never from Anaconda: PyInstaller would bundle hundreds of MB
rem      of unrelated conda libraries into the executables.
rem   2. pip install -r requirements.txt pyinstaller
rem   3. pyinstaller --clean on each spec in packaging\
rem   4. copy README_RUN.txt and open_firewall.ps1 next to the executables, print sizes.
rem Executables are NOT committed (dist\ and build\ are in .gitignore); they go in the zip.
setlocal EnableExtensions

cd /d "%~dp0.."
set "ROOT=%CD%"
set "VENV=%ROOT%\.build_venv"
set "DIST=%ROOT%\dist\windows"
set "WORK=%ROOT%\build\windows"
set "PYINSTALLER_VERSION=6.11.1"
rem A PYTHONPATH from the system would come BEFORE the venv in sys.path: the build must see only the venv.
set "PYTHONPATH="
set "PYTHONHOME="

if not defined PYTHON (
    py -3 --version >nul 2>&1 && (set "PYTHON=py -3") || (set "PYTHON=python")
)

rem --- 1. refuse Anaconda / Miniconda -------------------------------------------
if defined CONDA_PREFIX (
    echo ERROR: an Anaconda/conda environment is active. Run "conda deactivate" and use python.org Python.
    exit /b 1
)
%PYTHON% -c "import os,sys; sys.exit(1 if os.path.isdir(os.path.join(sys.base_prefix,'conda-meta')) else 0)"
if errorlevel 1 (
    echo ERROR: %PYTHON% is an Anaconda Python. Install Python 3.10+ from python.org, or set PYTHON=...
    exit /b 1
)
%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.10 or newer is needed.
    exit /b 1
)

echo == Fresh virtual environment: %VENV%
if exist "%VENV%" rmdir /s /q "%VENV%"
%PYTHON% -m venv "%VENV%" || exit /b 1
set "VPY=%VENV%\Scripts\python.exe"

rem --- 2. dependencies ----------------------------------------------------------
echo == Installing requirements and PyInstaller %PYINSTALLER_VERSION%
"%VPY%" -m pip install --quiet --upgrade pip || exit /b 1
"%VPY%" -m pip install --quiet -r requirements.txt pyinstaller==%PYINSTALLER_VERSION% || exit /b 1

rem --- 3. build -----------------------------------------------------------------
if exist "%DIST%" rmdir /s /q "%DIST%"
mkdir "%DIST%"
if not exist "%WORK%" mkdir "%WORK%"
set "LOG=%WORK%\pyinstaller.log"
type nul > "%LOG%"
for %%S in (WhiteboardServer WhiteboardClient WhiteboardClient-console) do (
    echo == PyInstaller: %%S
    "%VPY%" -m PyInstaller --clean --noconfirm --log-level WARN --distpath "%DIST%" --workpath "%WORK%" "packaging\%%S.spec" >> "%LOG%" 2>&1
    if errorlevel 1 (
        type "%LOG%"
        echo ERROR: PyInstaller failed on %%S.spec
        exit /b 1
    )
)

rem --- 4. extras next to the executables ----------------------------------------
copy /y "packaging\README_RUN.txt" "%DIST%\" >nul
copy /y "scripts\open_firewall.ps1" "%DIST%\" >nul

echo.
echo == Missing-module warnings (full log: %LOG%; details: %WORK%\*\warn-*.txt)
findstr /c:"WARNING" "%LOG%" || echo    (none)
echo.
echo == Result in %DIST%
dir "%DIST%"
echo Smoke test:  "%VPY%" scripts\exe_smoke_test.py --dist "%DIST%"
endlocal
