#define AppName "Local AI Manager"
#define AppVersion "48.1.0"
#define AppPublisher "Local AI Manager"
#define AppExeName "LocalAIManager.exe"

[Setup]
AppId={{B3D3A0A8-7C5D-4F0C-9B44-7C2F9B4E4722}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\LocalAIManager
DisableDirPage=yes
UsePreviousAppDir=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename=LocalAIManager_Setup_v{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
UninstallDisplayIcon={app}\{#AppExeName}
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
Uninstallable=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "dist\LocalAIManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Local AI Manager"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; AppUserModelID: "LocalAIManager.Desktop"
Name: "{autodesktop}\Local AI Manager"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; AppUserModelID: "LocalAIManager.Desktop"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Запустить Local AI Manager"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Intentionally do NOT delete {userappdata}\LocalAIManager.
; User models, configuration and logs must survive uninstall.
