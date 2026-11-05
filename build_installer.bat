@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Local AI Manager - installer build

echo ================================================
echo   Local AI Manager - Inno Setup build
echo ================================================
echo.

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo [ERROR] Inno Setup 6 not found.
  echo Install it from https://jrsoftware.org/isdl.php and run this again.
  pause
  exit /b 1
)

if not exist "dist\LocalAIManager\LocalAIManager.exe" (
  echo [ERROR] dist\LocalAIManager\LocalAIManager.exe not found.
  echo Run build_windows.bat first.
  pause
  exit /b 1
)

echo [1/1] Compiling installer.iss...
"%ISCC%" installer.iss
if errorlevel 1 goto :error

echo.
echo BUILD SUCCESSFUL
echo Output: %CD%\installer_output\
dir /b installer_output\*.exe
echo.
pause
exit /b 0

:error
echo.
echo [ERROR] Installer build failed. See the output above.
pause
exit /b 1
