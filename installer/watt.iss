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

[InstallDelete]
; 옛 버전의 내부 파일이 남지 않게(업데이트 때)
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\WATT\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\WATT"; Filename: "{app}\WATT.exe"
Name: "{autodesktop}\WATT"; Filename: "{app}\WATT.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\WATT.exe"; Description: "{cm:LaunchProgram,WATT}"; Flags: nowait postinstall skipifsilent
; 앱 안 업데이트(/RELAUNCH=1): 조용히 설치한 뒤 다시 켠다
Filename: "{app}\WATT.exe"; Flags: nowait; Check: IsRelaunch

[CustomMessages]
korean.AskModels=WATT 로 받은 AI 번역 모델도 지울까요?%n(수 GB · Ollama 프로그램은 남습니다)
korean.AskAddons=게임 폴더의 글꼴 애드온(ChatFontCJK)도 지울까요?
korean.AskData=WATT 설정과 기록(채팅 기록 포함)도 지울까요?
english.AskModels=Also remove the AI translation models WATT downloaded?%n(several GB; Ollama itself stays)
english.AskAddons=Also remove the ChatFontCJK font addon from your game folder?
english.AskData=Also remove WATT settings and logs (including chat logs)?

[Code]
{ 삭제할 때 WATT 가 넣은 것만 물어보고 지운다. 조용히 삭제(/SILENT)하면 기본값 '아니오' — 아무것도 지우지 않는다 }
function IsRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

function DataDir: String;
begin
  Result := ExpandConstant('{localappdata}\WATT');
end;

function ReadList(const Name: String; var Lines: TArrayOfString): Boolean;
begin
  Result := LoadStringsFromFile(DataDir + '\' + Name, Lines) and (GetArrayLength(Lines) > 0);
end;

function OllamaExe: String;
begin
  Result := ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe');
  if not FileExists(Result) then
    Result := ExpandConstant('{commonpf}\Ollama\ollama.exe');
end;

procedure RemoveModels(const Lines: TArrayOfString);
var
  I, Code: Integer;
begin
  if not FileExists(OllamaExe) then Exit;
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Trim(Lines[I]) <> '' then
      Exec(OllamaExe, 'rm ' + Trim(Lines[I]), '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure RemoveAddons(const Lines: TArrayOfString);
var
  I: Integer;
  D: String;
begin
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Trim(Lines[I]) <> '' then
    begin
      D := Trim(Lines[I]) + '\Interface\AddOns\ChatFontCJK';
      if DirExists(D) then DelTree(D, True, True, True);
    end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Lines: TArrayOfString;
begin
  if CurUninstallStep <> usUninstall then Exit;
  if ReadList('installed_models.txt', Lines) then
    if SuppressibleMsgBox(CustomMessage('AskModels'), mbConfirmation, MB_YESNO, IDNO) = IDYES then
      RemoveModels(Lines);
  if ReadList('installed_addons.txt', Lines) then
    if SuppressibleMsgBox(CustomMessage('AskAddons'), mbConfirmation, MB_YESNO, IDNO) = IDYES then
      RemoveAddons(Lines);
  if DirExists(DataDir) then
    if SuppressibleMsgBox(CustomMessage('AskData'), mbConfirmation, MB_YESNO, IDNO) = IDYES then
      DelTree(DataDir, True, True, True);
end;
