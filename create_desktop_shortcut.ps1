$ErrorActionPreference = 'Stop'
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Exe = Join-Path $AppDir 'LocalAIManager.exe'
$Icon = Join-Path $AppDir 'assets\local_ai_manager_icon.ico'
if (-not (Test-Path $Exe)) { throw "Не найден LocalAIManager.exe рядом со скриптом: $Exe" }
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop 'Local AI Manager.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Exe
$Shortcut.WorkingDirectory = $AppDir
if (Test-Path $Icon) { $Shortcut.IconLocation = "$Icon,0" }
$Shortcut.Description = 'Local AI Manager'
$Shortcut.Save()
Write-Host "Ярлык создан: $ShortcutPath" -ForegroundColor Green
