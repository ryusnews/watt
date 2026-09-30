# WATT 배포판 만들기

```
python tools/build.py              # 아이콘 → WATT.exe → 오픈소스 고지 → selftest → 설치 프로그램
python tools/build.py --no-installer
```

| 결과 | 위치 |
|---|---|
| 실행 파일(폴더형, 약 96MB) | `dist/WATT/WATT.exe` |
| 설치 프로그램(약 31MB) | `dist/installer/WATT-Setup-<버전>.exe` |

- 버전은 `watt/__init__.py` 의 `VERSION` 하나만 바꾼다(exe 버전 정보·설치 프로그램 이름이 따라간다).
- 필요한 것: Python 3.12 + `pip install pywebview numpy pyinstaller winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Globalization winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-Windows.Foundation.Collections`, Inno Setup 6(내 계정 설치: `%LOCALAPPDATA%\Programs\Inno Setup 6`).
- `selftest` 는 exe 안에 모듈·데이터·OCR·글꼴·WebView2·API 가 다 들어갔는지 창을 띄우지 않고 확인한다(`WATT.exe --role selftest` → `logs/selftest.json`).

## 설치 프로그램
- 내 계정에만 설치(관리자 권한 없음): `%LOCALAPPDATA%\Programs\WATT`, 시작 메뉴 바로가기, 바탕 화면 바로가기(선택).
- 사용자 데이터(설정·기록·채팅 영역)는 `%LOCALAPPDATA%\WATT` — 제거해도 남긴다.
- 조용히 설치: `WATT-Setup-0.1.0.exe /VERYSILENT /CURRENTUSER`
- WATT 가 켜져 있으면 설치·제거가 닫기를 요청한다(AppMutex).

## 알아 둘 것
- **코드 서명 없음** — 다른 PC 에서 처음 실행하면 "Windows의 PC 보호"(SmartScreen)가 뜬다. 배포하려면 서명 인증서(예: Azure Trusted Signing 월 구독, OV 인증서)가 필요하다.
- 개발용 `start-watt.vbs`(python -m watt)와 설치본은 같은 잠금을 쓰므로 동시에 켤 수 없다.
- 오류 기록: `%LOCALAPPDATA%\WATT\logs\app.log`(런처), `live_error.log`(통역 창).
- 화면 미리보기: `python -m http.server` 로 프로젝트를 띄우고 `watt/ui/index.html?mock` — `?mock` 이 있을 때만 가짜 데이터.
