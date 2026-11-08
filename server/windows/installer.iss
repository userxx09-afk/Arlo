; Inno Setup script for ArloKeeper-Setup.exe. Built by .github/workflows/windows-installer.yml from
; the bundle it puts in build\ArloKeeper (embedded Python + packages, the app, ffmpeg).
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define PS "{sys}\WindowsPowerShell\v1.0\powershell.exe"

[Setup]
AppId={{8B7C2E5A-4F3D-4C1B-9A6E-2D5F7A8C9B10}
AppName=Arlo Keeper
AppVersion={#AppVersion}
AppVerName=Arlo Keeper {#AppVersion}
AppPublisher=BAN (Barb's Arlo Notifications)
AppPublisherURL=https://github.com/userxx09-afk/Arlo
AppSupportURL=https://github.com/userxx09-afk/Arlo
DefaultDirName={autopf}\Arlo Keeper
DefaultGroupName=Arlo Keeper
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\..\dist
OutputBaseFilename=ArloKeeper-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
UninstallDisplayName=Arlo Keeper
UninstallDisplayIcon={sys}\shell32.dll,21

[Files]
Source: "..\..\build\ArloKeeper\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "arlo-keeper.env"; DestDir: "{commonappdata}\ArloKeeper"; Flags: onlyifdoesntexist uninsneveruninstall

[InstallDelete]
; Old code is replaced on every update (clips and settings live in ProgramData, not here).
Type: filesandordirs; Name: "{app}\app"

[INI]
Filename: "{group}\Open Arlo Keeper.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://localhost:8090"

[Icons]
Name: "{group}\Restart Arlo Keeper"; Filename: "{#PS}"; Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\restart.ps1"""; IconFilename: "{sys}\shell32.dll"; IconIndex: 238
Name: "{group}\Arlo Keeper log"; Filename: "{win}\notepad.exe"; Parameters: """{commonappdata}\ArloKeeper\logs\arlo-keeper.log"""
Name: "{group}\Arlo Keeper settings"; Filename: "{win}\notepad.exe"; Parameters: """{commonappdata}\ArloKeeper\arlo-keeper.env"""
Name: "{group}\Clips folder"; Filename: "{commonappdata}\ArloKeeper\clips"
Name: "{group}\Uninstall Arlo Keeper"; Filename: "{uninstallexe}"

[Run]
Filename: "{#PS}"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install-task.ps1"" -AppDir ""{app}"""; StatusMsg: "Starting Arlo Keeper..."; Flags: runhidden waituntilterminated
Filename: "http://localhost:8090"; Description: "Open Arlo Keeper (sign in to Arlo there)"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{#PS}"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\uninstall-task.ps1"" -AppDir ""{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveArloKeeperTask"

[UninstallDelete]
Type: files; Name: "{group}\Open Arlo Keeper.url"
Type: filesandordirs; Name: "{app}"

[Code]
// Stop a running copy before its files are replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  StopScript: String;
begin
  StopScript := ExpandConstant('{app}\stop.ps1');
  if FileExists(StopScript) then
    Exec(ExpandConstant('{#PS}'), '-NoProfile -ExecutionPolicy Bypass -File "' + StopScript + '" -AppDir "' + ExpandConstant('{app}') + '"',
         '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
