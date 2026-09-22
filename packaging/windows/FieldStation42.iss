; Inno Setup script for FieldStation42 on Windows.
;
;   iscc /DAppVersion=1.2.3 packaging\windows\FieldStation42.iss
;
; Packages the PyInstaller *folder* build (dist\FieldStation42) - the one
; that starts instantly - into a per-user installer.  No administrator rights
; are needed to install; the only elevated step is the optional firewall rule
; for the phone remote, and Windows asks for that separately.
;
; Everything the application needs is inside the folder build: mpv, ffmpeg,
; ffprobe, Qt and the Python runtime.  There are no prerequisites to install.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\FieldStation42"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\artifacts"
#endif

#define AppName "FieldStation42"
#define AppExe "FieldStation42.exe"
#define AppPublisher "FieldStation42 contributors"
#define AppURL "https://github.com/ryanmah/f242_Arcade"
#define WebConsole "http://localhost:4242"
; Inno's version resource wants numeric-only x.y.z.w; strip any -dev suffix.
#define NumericVersion Copy(AppVersion, 1, Pos("-", AppVersion + "-") - 1)

[Setup]
AppId={{9C2B1E5A-6E3D-4F0B-9A8E-F1E5D42A4242}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#NumericVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
InfoAfterFile=after-install.txt
OutputDir={#OutputDir}
OutputBaseFilename={#AppName}-{#AppVersion}-windows-x64-setup
SetupIconFile=..\..\fs42\fs42_server\static\favicon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Per-user by default (lands in %LOCALAPPDATA%\Programs); the dialog lets an
; administrator choose an all-users install instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
RestartApplications=no
MinVersion=10.0
ShowLanguageDialog=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start FieldStation42 when I &log in"; GroupDescription: "Startup:"; Flags: unchecked
Name: "firewall"; Description: "Allow the &phone remote and web console through Windows Firewall (port 4242, private networks only - asks for administrator approval)"; GroupDescription: "Network:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\THIRD_PARTY_LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Watch TV"
Name: "{group}\{#AppName} web console"; Filename: "{#WebConsole}"; Comment: "Manage channels in your browser (FieldStation42 must be running)"
Name: "{group}\{#AppName} data folder"; Filename: "{localappdata}\{#AppName}"; Comment: "Your channel configs, catalog and schedules"
; `doctor` has no console in the windowed build; it writes doctor-report.txt
; into the data folder and opens it in Notepad.
Name: "{group}\Check this computer (doctor)"; Filename: "{app}\{#AppExe}"; Parameters: "doctor"; Comment: "Diagnose problems - opens a report when done"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
; The firewall rule needs elevation even for a per-user install.  "runas"
; triggers the UAC prompt; the install carries on if the user declines.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes profile=private protocol=TCP localport=4242"; Verb: "runas"; Flags: shellexec runhidden waituntilterminated skipifdoesntexist; Tasks: firewall; StatusMsg: "Adding the Windows Firewall rule..."
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Verb: "runas"; Flags: shellexec runhidden waituntilterminated; RunOnceId: "RemoveFirewallRule"

[UninstallDelete]
; Files the application writes next to itself (none by design, but be tidy).
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
// Stop a running FieldStation42 (and the mpv it launched) before copying
// files over it.  CloseApplications only handles files Setup itself needs
// to replace; mpv.exe holding the audio device is not on that list.
procedure StopRunningApp;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM FieldStation42.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM FieldStation42-debug.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp;
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopRunningApp;
  Result := True;
end;

// The data folder (configs, catalog, schedules, and often the user's own
// video files pointed at by content_dir) is precious; never delete it
// silently.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\{#AppName}');
    if DirExists(DataDir) then
    begin
      if MsgBox('Also delete your FieldStation42 data folder?' + #13#10 + #13#10 +
                DataDir + #13#10 + #13#10 +
                'It holds your channel configs, catalog and schedules. ' +
                'Video files stored elsewhere are never touched.' + #13#10 + #13#10 +
                'Choose No to keep it for a future install.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
