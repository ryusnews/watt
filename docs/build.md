# WATT 배포판 만들기

```
python tools/build.py               # 아이콘 → WATT.exe → 오픈소스 고지 → selftest → 포터블 zip(+ 풀어서 selftest)
python tools/build.py --installer   # + 설치 프로그램 — 릴리스할 때는 항상
python tools/prune_releases.py      # 릴리스 뒤: 최근 2개만 파일을 두고 옛 첨부 파일 지우기(태그 · 변경 내용은 남김)
```

| 결과 | 위치 |
|---|---|
| 실행 파일(폴더형, 약 96MB) | `dist/WATT/WATT.exe` |
| **포터블(기본 배포)** | `dist/portable/WATT-Portable-<버전>.zip` |
| 설치 프로그램 | `dist/installer/WATT-Setup-<버전>.exe` |

- **개발 · 시험은 포터블로, 릴리스는 두 파일 모두**: 0.1.2 의 옛 업데이트는 최신 릴리스의 설치 파일만 보므로(#15).
- **릴리스 파일은 최근 2개만**(초기 빠른 개발 단계): 업데이트는 최신만 쓰고, 하나 앞은 되돌리기용. 옛 릴리스는 첨부 파일만 지운다.
  나중에 정식 · 베타를 나누면(브랜치 · 사전 릴리스) 그때 다시 정한다.
- 포터블 판별: `WATT.exe` 옆 `portable.txt` → 설정·기록은 `<폴더>\data`(쓸 수 없는 폴더면 `%LOCALAPPDATA%\WATT`).
- 업데이트(0.1.8~, `watt/update.py`, 설치판 · 포터블 공통): 최신 릴리스의 포터블 zip 에서 바뀐 파일만 HTTP Range 로 받아
  `update\<버전>` 에 두고 다시 시작할지 묻는다. 바꿔 끼우기는 지금 프로그램을 `update\runner` 로 복사해 `--role apply-delta` 로
  (옛 파일은 backup, 실패하면 되돌림). 설치판은 '앱 및 기능' 버전도 고친다. 기록: `logs\update.log`.
  0.1.3–0.1.7 포터블의 옛 방식(`--role apply-update`)은 옮겨 가기용으로 남아 있다.

- 버전은 `watt/__init__.py` 의 `VERSION` 하나만 바꾼다(exe 버전 정보·설치 프로그램 이름이 따라간다).
- 버전 규칙(자세히는 [ROADMAP.md](ROADMAP.md)): 안건(추가·수정·변경·삭제) 하나 = GitHub 이슈 하나 = 수(패치) +1 = 릴리스 하나.
  마일스톤(0.2.0 · 0.3.0 · 1.0.0)의 통과 기준을 넘으면 부(마이너)를 올리고 수는 0 부터.
  이슈 열기 → 고치기 → 커밋 메시지에 `Closes #번호` → VERSION·CHANGELOG → `python tools/build.py --installer`
  → 릴리스 `v0.1.N` 에 포터블 zip · 설치 파일 → `python tools/prune_releases.py`.
- 필요한 것: Python 3.12 + `pip install pywebview numpy pyinstaller winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Globalization winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-Windows.Foundation.Collections`, Inno Setup 6(내 계정 설치: `%LOCALAPPDATA%\Programs\Inno Setup 6`).
- `selftest` 는 exe 안에 모듈·데이터·OCR·글꼴·WebView2·API 가 다 들어갔는지 창을 띄우지 않고 확인한다(`WATT.exe --role selftest` → `logs/selftest.json`).

## 설치 프로그램
- 내 계정에만 설치(관리자 권한 없음): `%LOCALAPPDATA%\Programs\WATT`, 시작 메뉴 바로가기, 바탕 화면 바로가기(선택).
- 사용자 데이터(설정·기록·채팅 영역)는 `%LOCALAPPDATA%\WATT` — 제거해도 남긴다.
- 조용히 설치: `WATT-Setup-0.1.0.exe /VERYSILENT /CURRENTUSER`
- WATT 가 켜져 있으면 설치·제거가 닫기를 요청한다(AppMutex).
- 0.1.7 이하의 앱 안 업데이트는 최신 릴리스의 `WATT-Setup-*.exe` 를 조용히 실행한다(`/VERYSILENT … /RELAUNCH=1`).
  그래서 릴리스의 설치 파일 이름은 꼭 `WATT-Setup-<버전>.exe` 로. 0.1.8 부터는 위의 바뀐 파일 받기.

## 알아 둘 것
- **코드 서명 없음** — 다른 PC 에서 처음 실행하면 "Windows의 PC 보호"(SmartScreen)가 뜬다. 배포하려면 서명 인증서(예: Azure Trusted Signing 월 구독, OV 인증서)가 필요하다.
- 개발용 `start-watt.vbs`(python -m watt)와 설치본은 같은 잠금을 쓰므로 동시에 켤 수 없다.
- 오류 기록: `%LOCALAPPDATA%\WATT\logs\app.log`(런처), `live_error.log`(통역 창).
- 화면 미리보기: `python -m http.server` 로 프로젝트를 띄우고 `watt/ui/index.html?mock` — `?mock` 이 있을 때만 가짜 데이터.
