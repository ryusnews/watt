"""파일 위치.

RES  — 프로그램과 함께 배포되는 읽기 전용 파일(ui, 용어 사전, 애드온). exe 로 묶이면 압축이 풀린 폴더.
DATA — 사용자별로 쓰는 파일(설정·기록·채팅 영역). 설치본은 %LOCALAPPDATA%\\WATT, 개발 중에는 프로젝트 폴더
       (예전 logs/·chat_region.json 을 그대로 쓰도록).
"""
import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
DATA = Path(os.environ.get("WATT_HOME") or (Path(os.environ["LOCALAPPDATA"]) / "WATT" if FROZEN else RES))

UI = RES / "watt" / "ui"
ADDON = RES / "addon" / "ChatFontCJK"
TERMS = RES / "translator" / "wow_terms.json"

LOGS = DATA / "logs"
SETTINGS = DATA / "settings.json"
REGION = DATA / "chat_region.json"
REGION_GOOD = DATA / "chat_region_good.json"
LIVE_UI = DATA / "live_ui.json"     # 통역 창 위치·크기(통역 창 프로세스가 씀)
INPUT_UI = DATA / "input_ui.json"   # 입력창 위치(입력창 프로세스가 씀)
LIVE_STATUS = DATA / "live_status.json"  # 통역 창이 2초마다 쓰는 상태 — 런처 대시보드용
LIVE_CMD = DATA / "live_cmd.json"        # 런처 → 통역 창 명령(다시 찾기·위치 초기화)
DOWNLOADS = DATA / "downloads"


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
