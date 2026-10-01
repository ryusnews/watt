<p align="center"><img src="assets/watt-256.png" width="96" alt="WATT"></p>
<h1 align="center">WATT</h1>
<p align="center">월드 오브 워크래프트 클래식 채팅 실시간 통역 · Real-time chat interpreter for WoW Classic</p>

<p align="center"><img src="docs/images/home.png" width="760" alt="WATT 홈 화면"></p>

외국어 채팅(영어·중국어·러시아어)을 읽어 게임 위에 한국어로 띄우고, 한국어로 쓴 말은 상대 언어로 바꿔 줍니다.
번역은 **내 PC 안의 AI**(Ollama + Gemma 4)가 합니다.

## 다운로드
[**Releases**](../../releases/latest)에서 `WATT-Setup-<버전>.exe` 를 받아 실행하세요.
관리자 권한 없이 내 계정에만 설치됩니다.

> 코드 서명이 아직 없어 처음 실행할 때 "Windows의 PC 보호" 창이 뜰 수 있습니다. **추가 정보 → 실행**을 누르세요.

## 필요한 것
| 항목 | 권장 |
|---|---|
| Windows | 10 (1809 이상) / 11, 64비트 |
| 그래픽카드 | VRAM 8GB 이상 (NVIDIA 권장) |
| 게임 | 창 모드(최대화) 또는 전체 창 모드 |

AI 실행기(Ollama), 번역 모델, 글자 인식 언어 팩은 앱의 **환경 설정**에서 버튼으로 설치합니다.
번역 모델 Gemma 4 는 Ollama 모델 저장소에서 받으며 Google [Gemma 이용 약관](https://ai.google.dev/gemma/terms)을 따릅니다.

## 쓰는 법
1. **환경 설정** 7단계를 차례로 끝냅니다.
2. 게임을 켜고 홈의 **전원 버튼**을 누르면 게임 위에 통역 창이 뜹니다.
3. 보낼 말은 **Ctrl+Shift+K**(게임 중에는 Ctrl+Enter) → 한국어 입력 → Enter → 게임 채팅창에 Ctrl+V.

채팅창을 잘 읽게 하는 게임 설정은 [docs/setup-guide.md](docs/setup-guide.md)에 있습니다.

## 약속
- **PC 안에서만 번역** — 채팅 내용을 인터넷으로 보내지 않습니다.
- **화면만 읽음** — 채팅창을 캡처해 글자를 읽을 뿐, 게임 파일·메모리를 건드리지 않습니다.
- **자동 입력 없음** — 번역문은 클립보드에 두거나 입력칸에 넣기까지만 합니다. 보내기는 직접.

## 삭제
앱의 **환경 설정**에서 하나씩 지울 수 있습니다 — 모델·글꼴 애드온은 휴지통, OCR 언어 팩(중국어·러시아어)은 ×, Ollama 는 "제거".

WATT 자체는 "설정 → 앱"에서 제거합니다. 제거할 때 하나씩 묻습니다.
- WATT 로 받은 AI 모델 · 게임 폴더의 글꼴 애드온 · WATT 설정과 기록(채팅 기록 포함)

Ollama 와 Windows OCR 언어 팩은 다른 프로그램도 쓰므로 남깁니다. 필요 없으면 직접 지우세요.
- Ollama: 설정 → 앱 → Ollama 제거 (받은 모델은 `ollama rm <이름>` 또는 `%USERPROFILE%\.ollama\models`)
- OCR 언어 팩: 설정 → 시간 및 언어 → 언어 및 지역

## 개발
```
pip install pywebview numpy pyinstaller winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging ^
  winrt-Windows.Globalization winrt-Windows.Storage.Streams winrt-Windows.Foundation winrt-Windows.Foundation.Collections
python -m watt                # 런처
python -m eval.quick_translate   # 번역 평가(60문장)
python tools/build.py         # WATT.exe + 설치 프로그램
```
| 폴더 | 내용 |
|---|---|
| `watt/` | 런처(pywebview), 화면 캡처·Windows OCR, 채팅 영역 찾기, 환경 점검·설치 |
| `watt/ui/` | 런처 화면(HTML/CSS/JS) |
| `translator/` | 통역 창(live), 보내기 입력창(outgoing), 번역 프롬프트, 용어 사전(`wow_terms.json`) |
| `addon/ChatFontCJK/` | 중국어·러시아어 채팅이 □로 깨지지 않게 하는 게임 글꼴 애드온 |
| `eval/` | 번역 평가 세트 |
| `tools/` | 빌드, 아이콘, 기록 분석 |

버전은 `0.1.N` — 안건(추가·수정·변경·삭제) 하나마다 N 을 올리고, [이슈](../../issues)와 [릴리스](../../releases)로 남깁니다.

자세한 빌드 방법: [docs/build.md](docs/build.md) · 용어 사전 고치기: [docs/setup-guide.md](docs/setup-guide.md#용어-사전)

## 라이선스
코드는 [MIT](LICENSE). 함께 배포하는 글꼴(IBM Plex, Sarasa Gothic)은 SIL OFL 1.1.

World of Warcraft는 Blizzard Entertainment의 상표입니다. WATT는 Blizzard와 관련 없는 비공식 도구입니다.
