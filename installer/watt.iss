; WATT 설치 프로그램 — tools/build.py 가 ISCC /DAppVersion=x.y.z 로 만든다.
; 내 계정에만 설치(관리자 권한 없이 %LOCALAPPDATA%\Programs\WATT). 사용자 데이터(%LOCALAPPDATA%\WATT)는 지우지 않는다.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{8C2E7A51-5B7D-4E3A-9C4F-2D6B1A9E7C30}
AppName=WATT
AppVersion={#AppVersion}
AppVerName=WATT {#AppVersion}
AppPublisher=WATT
DefaultDirName={autopf}\WATT
DefaultGroupName=WATT
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=..\dist\installer
OutputBaseFilename=WATT-Setup-{#AppVersion}
SetupIconFile=..\assets\watt.ico
UninstallDisplayIcon={app}\WATT.exe
UninstallDisplayName=WATT
WizardStyle=modern
WizardImageFile=..\assets\wizard.bmp,..\assets\wizard-150.bmp,..\assets\wizard-200.bmp
WizardSmallImageFile=..\assets\wizard-small.bmp,..\assets\wizard-small-150.bmp,..\assets\wizard-small-200.bmp
Compression=lzma2/max
SolidCompression=yes
AppMutex=WATT_launcher_single_instance
CloseApplications=yes
ShowLanguageDialog=no
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName=WATT
VersionInfoDescription=WATT Setup

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\WATT\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\WATT"; Filename: "{app}\WATT.exe"
Name: "{autodesktop}\WATT"; Filename: "{app}\WATT.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\WATT.exe"; Description: "{cm:LaunchProgram,WATT}"; Flags: nowait postinstall skipifsilent
