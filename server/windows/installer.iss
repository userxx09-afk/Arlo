; Inno Setup script for CameraKeeper-Setup.exe. Built by .github/workflows/windows-installer.yml from
; the bundle it puts in build\CameraKeeper (embedded Python + packages, the app, ffmpeg).
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define PS "{sys}\WindowsPowerShell\v1.0\powershell.exe"

[Setup]
AppId={{8B7C2E5A-4F3D-4C1B-9A6E-2D5F7A8C9B10}
AppName=Camera Keeper
AppVersion={#AppVersion}
AppVerName=Camera Keeper {#AppVersion}
AppPublisher=BCN (Barb's Camera Notifications)
AppPublisherURL=https://github.com/userxx09-afk/Arlo
AppSupportURL=https://github.com/userxx09-afk/Arlo
DefaultDirName={autopf}\Camera Keeper
DefaultGroupName=Camera Keeper
DisableProgramGroupPage=yes
; Same AppId as when this was "Arlo Keeper", so it updates that install, but into the new folder
; (migrate-from-arlo.ps1 moves the data over and removes the old folder).
UsePreviousAppDir=no
UsePreviousGroup=no
DisableDirPage=auto
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\..\dist
OutputBaseFilename=CameraKeeper-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
UninstallDisplayName=Camera Keeper
UninstallDisplayIcon={sys}\shell32.dll,21

[Files]
Source: "migrate-from-arlo.ps1"; Flags: dontcopy
Source: "..\..\build\CameraKeeper\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "camera-keeper.env"; DestDir: "{commonappdata}\CameraKeeper"; Flags: onlyifdoesntexist uninsneveruninstall

[InstallDelete]
; Old code is replaced on every update (clips and settings live in ProgramData, not here).
Type: filesandordirs; Name: "{app}\app"

[INI]
Filename: "{group}\Open Camera Keeper.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://localhost:8090"

[Icons]
Name: "{group}\Restart Camera Keeper"; Filename: "{#PS}"; Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\restart.ps1"""; IconFilename: "{sys}\shell32.dll"; IconIndex: 238
Name: "{group}\Camera Keeper log"; Filename: "{win}\notepad.exe"; Parameters: """{commonappdata}\CameraKeeper\logs\camera-keeper.log"""
Name: "{group}\Camera Keeper settings"; Filename: "{win}\notepad.exe"; Parameters: """{commonappdata}\CameraKeeper\camera-keeper.env"""
Name: "{group}\Clips folder"; Filename: "{commonappdata}\CameraKeeper\clips"
Name: "{group}\Uninstall Camera Keeper"; Filename: "{uninstallexe}"

[Run]
Filename: "{#PS}"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install-task.ps1"" -AppDir ""{app}"""; StatusMsg: "Starting Camera Keeper..."; Flags: runhidden waituntilterminated
Filename: "http://localhost:8090"; Description: "Open Camera Keeper (sign in to your cameras there)"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{#PS}"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\uninstall-task.ps1"" -AppDir ""{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveCameraKeeperTask"

[UninstallDelete]
Type: files; Name: "{group}\Open Camera Keeper.url"
Type: filesandordirs; Name: "{app}"

[Code]
// Stop a running copy before its files are replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  StopScript: String;
begin
  // Once, on a PC that still has Arlo Keeper (the name before the rename): move it over first.
  ExtractTemporaryFile('migrate-from-arlo.ps1');
  Exec(ExpandConstant('{#PS}'), '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\migrate-from-arlo.ps1') +
       '" -OldAppDir "' + ExpandConstant('{autopf}\Arlo Keeper') + '"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  StopScript := ExpandConstant('{app}\stop.ps1');
  if FileExists(StopScript) then
    Exec(ExpandConstant('{#PS}'), '-NoProfile -ExecutionPolicy Bypass -File "' + StopScript + '" -AppDir "' + ExpandConstant('{app}') + '"',
         '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
