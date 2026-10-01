# WATT 배포판 만들기

```
python tools/build.py               # 아이콘 → WATT.exe → 오픈소스 고지 → selftest → 포터블 zip(+ 풀어서 selftest)
python tools/build.py --installer   # + 설치 프로그램 — 필요할 때만
```

| 결과 | 위치 |
|---|---|
| 실행 파일(폴더형, 약 96MB) | `dist/WATT/WATT.exe` |
| **포터블(기본 배포)** | `dist/portable/WATT-Portable-<버전>.zip` |
| 설치 프로그램(필요할 때만) | `dist/installer/WATT-Setup-<버전>.exe` |

- **포터블이 기본**: 개발·릴리스는 포터블에 먼저 적용한다. 설치판은 필요할 때(예: 설치판의 업데이트 방식이 바뀔 때) 맞춰 낸다.
  업데이트 확인은 자기 종류 파일이 있는 가장 새 릴리스를 고르므로, 포터블만 낸 릴리스는 설치판 사용자에게 안 보인다.
- 포터블 판별: `WATT.exe` 옆 `portable.txt` → 설정·기록은 `<폴더>\data`(쓸 수 없는 폴더면 `%LOCALAPPDATA%\WATT`).
- 포터블 업데이트: zip 을 `data\update\<버전>` 에 풀고 그 안의 새 `WATT.exe --role apply-update --target <폴더> --pid <n>` 이
  옛 WATT 가 꺼지길 기다렸다 `_internal` 을 지우고 새 파일을 복사(data 는 그대로)한 뒤 다시 켠다. 기록: `data\logs\update.log`.

- 버전은 `watt/__init__.py` 의 `VERSION` 하나만 바꾼다(exe 버전 정보·설치 프로그램 이름이 따라간다).
- 버전 규칙 `0.1.N`: 안건(추가·수정·변경·삭제) 하나 = GitHub 이슈 하나 = N+1 = 릴리스 하나.
  이슈 열기 → 고치기 → 커밋 메시지에 `Closes #번호` → VERSION·CHANGELOG → `python tools/build.py` → 릴리스 `v0.1.N` 에 설치 파일.
- 필요한 것: Python 3.12 + `pip install pywebview numpy pyinstaller winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Globalization winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-Windows.Foundation.Collections`, Inno Setup 6(내 계정 설치: `%LOCALAPPDATA%\Programs\Inno Setup 6`).
- `selftest` 는 exe 안에 모듈·데이터·OCR·글꼴·WebView2·API 가 다 들어갔는지 창을 띄우지 않고 확인한다(`WATT.exe --role selftest` → `logs/selftest.json`).

## 설치 프로그램
- 내 계정에만 설치(관리자 권한 없음): `%LOCALAPPDATA%\Programs\WATT`, 시작 메뉴 바로가기, 바탕 화면 바로가기(선택).
- 사용자 데이터(설정·기록·채팅 영역)는 `%LOCALAPPDATA%\WATT` — 제거해도 남긴다.
- 조용히 설치: `WATT-Setup-0.1.0.exe /VERYSILENT /CURRENTUSER`
- WATT 가 켜져 있으면 설치·제거가 닫기를 요청한다(AppMutex).
- 앱 안 업데이트(`watt/update.py`): GitHub 최신 릴리스의 `WATT-Setup-*.exe` 를 받아 GitHub 가 주는 SHA-256 과 맞춘 뒤
  `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /FORCECLOSEAPPLICATIONS /RELAUNCH=1` 로 실행하고 앱은 꺼진다.
  설치 프로그램은 옛 `_internal` 을 지우고 설치한 뒤 `RELAUNCH=1` 이면 다시 켠다. 릴리스에는 설치 파일 이름을 꼭 `WATT-Setup-<버전>.exe` 로.

## 알아 둘 것
- **코드 서명 없음** — 다른 PC 에서 처음 실행하면 "Windows의 PC 보호"(SmartScreen)가 뜬다. 배포하려면 서명 인증서(예: Azure Trusted Signing 월 구독, OV 인증서)가 필요하다.
- 개발용 `start-watt.vbs`(python -m watt)와 설치본은 같은 잠금을 쓰므로 동시에 켤 수 없다.
- 오류 기록: `%LOCALAPPDATA%\WATT\logs\app.log`(런처), `live_error.log`(통역 창).
- 화면 미리보기: `python -m http.server` 로 프로젝트를 띄우고 `watt/ui/index.html?mock` — `?mock` 이 있을 때만 가짜 데이터.
