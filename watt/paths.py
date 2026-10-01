"""파일 위치.

RES  — 프로그램과 함께 배포되는 읽기 전용 파일(ui, 용어 사전, 애드온). exe 로 묶이면 압축이 풀린 폴더.
DATA — 사용자별로 쓰는 파일(설정·기록·채팅 영역).
       포터블(WATT.exe 옆에 portable.txt) → 그 폴더 안 data\\ (폴더에 쓸 수 없으면 아래로)
       설치판 → %LOCALAPPDATA%\\WATT · 개발 중 → 프로젝트 폴더(예전 logs/·chat_region.json 을 그대로 쓰도록)
"""
import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else None  # WATT.exe 가 있는 폴더
PORTABLE = bool(FROZEN and (APP_DIR / "portable.txt").exists())


def _data_dir() -> Path:
    if os.environ.get("WATT_HOME"):
        return Path(os.environ["WATT_HOME"])
    if PORTABLE and os.access(APP_DIR, os.W_OK):
        return APP_DIR / "data"
    if FROZEN:
        return Path(os.environ["LOCALAPPDATA"]) / "WATT"
    return RES


DATA = _data_dir()
UPDATE_STAGE = DATA / "update"  # 포터블 업데이트: 새 버전을 여기에 풀었다가 덮어쓴다

UI = RES / "watt" / "ui"
ADDON = RES / "addon" / "ChatFontCJK"
TERMS = RES / "translator" / "wow_terms.json"
USER_TERMS = DATA / "user_terms.json"  # 사용자가 고친 용어(수정 · 추가 · 숨김) — 기본 사전 위에 덧씌운다

LOGS = DATA / "logs"
SETTINGS = DATA / "settings.json"
REGION = DATA / "chat_region.json"
REGION_GOOD = DATA / "chat_region_good.json"
LIVE_UI = DATA / "live_ui.json"     # 통역 창 위치·크기(통역 창 프로세스가 씀)
INPUT_UI = DATA / "input_ui.json"   # 입력창 위치(입력창 프로세스가 씀)
LAUNCHER_UI = DATA / "launcher_ui.json"  # 런처 창 위치 · 크기(물리 픽셀)
LIVE_STATUS = DATA / "live_status.json"  # 통역 창이 2초마다 쓰는 상태 — 런처 대시보드용
RESUME = DATA / "resume.json"            # 업데이트로 다시 켤 때 이어서 켤 것(통역 창이 돌고 있었나)
LIVE_CMD = DATA / "live_cmd.json"        # 런처 → 통역 창 명령(다시 찾기·위치 초기화)
DOWNLOADS = DATA / "downloads"
# WATT 가 직접 넣은 것 — 삭제 프로그램이 읽어 이것만 지운다(installer/watt.iss). 한 줄에 하나
INSTALLED_ADDONS = DATA / "installed_addons.txt"   # 애드온을 넣은 게임 폴더
INSTALLED_MODELS = DATA / "installed_models.txt"   # WATT 로 받은 Ollama 모델
INSTALLED_OCR = DATA / "installed_ocr.txt"         # WATT 가 설치한 OCR 언어 팩(원래 있던 팩은 적지 않는다)
INSTALLED_OLLAMA = DATA / "installed_ollama.txt"   # WATT 가 설치한 Ollama 폴더(원래 있었으면 비어 있음)
WEBVIEW = DATA / "webview"                         # 런처 창(WebView2) 캐시 — 데이터와 함께 지워지게


def ensure() -> None:
    for d in (DATA, LOGS):
        d.mkdir(parents=True, exist_ok=True)


def child_command(role: str) -> list[str]:
    """통역 창·입력창을 따로 띄울 명령 — exe 면 자기 자신, 개발 중이면 python -m watt."""
    if FROZEN:
        return [sys.executable, "--role", role]
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else exe), "-m", "watt", "--role", role]
