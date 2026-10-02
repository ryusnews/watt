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

[UninstallDelete]
; 설치 없이 바꿔 끼운 파일(#15)은 삭제 프로그램 목록에 없다 — 프로그램 파일은 통째로
Type: filesandordirs; Name: "{app}\_internal"

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
korean.AskModels=예전 버전의 WATT 가 PC 의 Ollama 로 받은 번역 모델도 지울까요?%n(수 GB · Ollama 프로그램은 남습니다)
korean.AskRunner=WATT 전용 AI 실행기와 번역 모델(수 GB)도 지울까요?%n(PC 에 따로 설치한 Ollama 는 그대로)
korean.AskAddons=게임 폴더의 글꼴 애드온(ChatFontCJK)도 지울까요?
korean.AskOcr=WATT 가 설치한 글자 인식 언어 팩도 지울까요?%n(원래 있던 언어 팩은 그대로 · Windows 권한 확인 창이 뜹니다)
korean.AskOllama=WATT 가 설치한 Ollama(AI 실행기)도 제거할까요?%n(Ollama 제거 프로그램이 열립니다)
korean.AskAi=WATT 로 받은 AI 글자 인식 파일(모델 · 실행 엔진)도 지울까요?
korean.AskData=WATT 설정과 기록(채팅 기록 포함)도 지울까요?
english.AskModels=Also remove the translation models an older WATT downloaded into your Ollama?%n(several GB; Ollama itself stays)
english.AskRunner=Also remove WATT's own AI runner and translation models (several GB)?%n(a separately installed Ollama stays)
english.AskAddons=Also remove the ChatFontCJK font addon from your game folder?
english.AskOcr=Also remove the text recognition language packs WATT installed?%n(packs you had before stay; Windows will ask for permission)
english.AskOllama=Also uninstall Ollama, which WATT installed?%n(the Ollama uninstaller will open)
english.AskAi=Also remove the AI text recognition files WATT downloaded (models and runtime)?
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

{ 설정의 models_dir("D:\\AI" → D:\AI) — 고른 모델 폴더. 없으면 '' }
function CustomModelsDir: String;
var
  S: AnsiString;
  P: Integer;
begin
  Result := '';
  if not LoadStringFromFile(DataDir + '\settings.json', S) then Exit;
  P := Pos('"models_dir": "', S);
  if P = 0 then Exit;
  S := Copy(S, P + Length('"models_dir": "'), 1024);
  P := Pos('"', S);
  if P <= 1 then Exit;
  Result := Copy(S, 1, P - 1);
  StringChangeEx(Result, '\\', '\', True);
end;

{ WATT 전용 실행기(데이터 폴더 ollama) 프로세스만 끈다 — PC 의 다른 Ollama 는 그대로 }
procedure StopRunner;
var
  Code: Integer;
begin
  Exec('powershell.exe', '-NoProfile -WindowStyle Hidden -Command "Get-Process ollama -ErrorAction SilentlyContinue | ' +
    'Where-Object { $_.Path -like ''' + DataDir + '\ollama\*'' } | Stop-Process -Force"', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure RemoveRunner;
var
  D: String;
begin
  StopRunner;
  DelTree(DataDir + '\ollama', True, True, True);
  DelTree(DataDir + '\ollama.new', True, True, True);
  DelTree(DataDir + '\models', True, True, True);
  D := CustomModelsDir;
  if (D <> '') and FileExists(D + '\WATT-models\.watt') then
    DelTree(D + '\WATT-models', True, True, True);
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

function CapName(const Code: String): String;
begin
  if Code = 'zh-Hans-CN' then Result := 'Language.OCR~~~zh-CN~0.0.1.0'
  else if Code = 'ru-RU' then Result := 'Language.OCR~~~ru-RU~0.0.1.0'
  else if Code = 'ko' then Result := 'Language.OCR~~~ko-KR~0.0.1.0'
  else if Code = 'en-US' then Result := 'Language.OCR~~~en-US~0.0.1.0'
  else Result := '';
end;

procedure RemoveOcr(const Lines: TArrayOfString);
var
  I, Err: Integer;
  Cmd: String;
begin
  Cmd := '';
  for I := 0 to GetArrayLength(Lines) - 1 do
    if CapName(Trim(Lines[I])) <> '' then
    begin
      if Cmd <> '' then Cmd := Cmd + ' & ';
      Cmd := Cmd + 'dism /Online /Remove-Capability /CapabilityName:' + CapName(Trim(Lines[I])) + ' /NoRestart';
    end;
  if Cmd <> '' then
    ShellExec('runas', 'cmd.exe', '/c ' + Cmd, '', SW_SHOW, ewWaitUntilTerminated, Err);
end;

procedure RemoveOllama(const Lines: TArrayOfString);
var
  Code: Integer;
  Unins: String;
begin
  if GetArrayLength(Lines) = 0 then Exit;
  Unins := Trim(Lines[0]) + '\unins000.exe';
  if FileExists(Unins) then
    Exec(Unins, '', '', SW_SHOW, ewWaitUntilTerminated, Code);
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
  if DirExists(DataDir + '\ollama') or DirExists(DataDir + '\models') or (CustomModelsDir <> '') then
    if SuppressibleMsgBox(CustomMessage('AskRunner'), mbConfirmation, MB_YESNO, IDNO) = IDYES then
      RemoveRunner;
  if ReadList('installed_models.txt', Lines) then
    if SuppressibleMsgBox(CustomMessage('AskModels'), mbConfirmation, MB_YESNO, IDNO) = IDYES then
      RemoveModels(Lines);
  if ReadList('installed_ollama.txt', Lines) then
    if SuppressibleMsgBox(CustomMessage('AskOllama'), mbConfirmation, MB_YESNO, IDNO) = IDYES then
      RemoveOllama(Lines);
  if ReadList('installed_ocr.txt', Lines) then
    if SuppressibleMsgBox(CustomMessage('AskOcr'), mbConfirmation, MB_YESNO, IDNO) = IDYES then
      RemoveOcr(Lines);
  if ReadList('installed_addons.txt', Lines) then
    if SuppressibleMsgBox(CustomMessage('AskAddons'), mbConfirmation, MB_YESNO, IDNO) = IDYES then
      RemoveAddons(Lines);
  if DirExists(DataDir + '\ai') then
    if SuppressibleMsgBox(CustomMessage('AskAi'), mbConfirmation, MB_YESNO, IDNO) = IDYES then
      DelTree(DataDir + '\ai', True, True, True);
  if DirExists(DataDir) then
    if SuppressibleMsgBox(CustomMessage('AskData'), mbConfirmation, MB_YESNO, IDNO) = IDYES then
      DelTree(DataDir, True, True, True);
end;
