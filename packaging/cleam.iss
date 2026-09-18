; Inno Setup script for the Windows installer. Built in CI:
;   ISCC /DAppVersion=0.1.0 packaging\cleam.iss
; Expects dist\cleam.exe (CLI) and dist\cleam-gui.exe (window) to exist.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8F3C6C2E-5F4B-4C1B-9E4A-0D3C6A1B2F77}
AppName=Cleam
AppVersion={#AppVersion}
AppPublisher=Cleam
DefaultDirName={autopf}\Cleam
DefaultGroupName=Cleam
UninstallDisplayIcon={app}\cleam-gui.exe
; Relative to this .iss file, not to the working directory.
OutputDir=..\dist-installer
OutputBaseFilename=cleam-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; lowest, not admin: a standard account (no rights to elevate) must still be
; able to install Cleam into its own profile. Cleam asks for elevation itself,
; from the window, only for the targets that need it.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\cleam-gui.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\cleam.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\Cleam"; Filename: "{app}\cleam-gui.exe"
Name: "{group}\Uninstall Cleam"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Cleam"; Filename: "{app}\cleam-gui.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "addtopath"; Description: "Add the cleam command to PATH"; GroupDescription: "Command line:"; Flags: unchecked

[Registry]
; Only when asked: appending to PATH is the kind of thing a cleaner should not
; do behind someone's back.
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}"; Check: NeedsPath; Tasks: addtopath

[Run]
Filename: "{app}\cleam-gui.exe"; Description: "Start Cleam"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsPath: Boolean;
var
  Existing: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Existing) then
    Existing := '';
  Result := Pos(Lowercase(ExpandConstant('{app}')), Lowercase(Existing)) = 0;
end;
