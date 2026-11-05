@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Local AI Manager - Windows build

echo ================================================
echo   Local AI Manager v48.1.4 - build and validation
echo ================================================
echo.

rem Python: prefer the py launcher, fall back to python from PATH.
where py >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python 3.10-3.13 x64 with Tcl/Tk not found.
    echo Install it from python.org with "Add Python to PATH".
    pause
    exit /b 1
  )
  set "PY=python"
)

%PY% -c "import sys, tkinter; print('Python:',sys.version); print('Tk:',tkinter.TkVersion)"
if errorlevel 1 (
  echo [ERROR] Python or Tkinter is unavailable.
  pause
  exit /b 1
)

echo [1/5] Creating isolated build environment...
if not exist .build_venv %PY% -m venv .build_venv
if errorlevel 1 goto :error
call .build_venv\Scripts\activate.bat

echo [2/5] Updating packaging tools...
python -m pip install --upgrade pip wheel setuptools
if errorlevel 1 goto :error
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :error

echo [3/5] Running regression tests...
python -m pip install pytest pillow pywin32 psutil
if errorlevel 1 goto :error
python -m pytest -q
if errorlevel 1 goto :error

echo [4/5] Building portable application folder...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
rem Build from LocalAIManager.spec: it holds hiddenimports ai_bridge and version_info.txt.
python -m PyInstaller --noconfirm --clean LocalAIManager.spec
if errorlevel 1 goto :error

if not exist "dist\LocalAIManager\LocalAIManager.exe" (
  echo [ERROR] EXE was not created.
  goto :error
)

echo [5/5] Checking output...
if not exist "dist\LocalAIManager\LocalAIManager.exe" goto :error

echo.
echo BUILD SUCCESSFUL
echo Output: %CD%\dist\LocalAIManager\
echo Next: compile installer.iss with Inno Setup 6.
echo.
pause
exit /b 0

:error
echo.
echo [ERROR] Build failed. See the first error above.
pause
exit /b 1
