; TidySweep installer script (Inno Setup 6)
; Compiles to a single setup EXE that installs TidySweep on any Windows 10/11 PC.

#define MyAppName "TidySweep"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "TidySweep"
#define MyAppExeName "TidySweep.exe"

[Setup]
AppId={{8A63B902-7C44-4B9B-9F51-23D7A1001E0F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; PrivilegesRequired=lowest installs per-user without admin rights; admin gives Program Files install.
PrivilegesRequired=admin
OutputBaseFilename=TidySweep-Setup-1.0.0
OutputDir=Output
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\TidySweep.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
