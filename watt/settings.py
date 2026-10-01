"""사용자 설정(settings.json) — 런처가 쓰고 통역 창·입력창은 읽는다."""
import json

from . import paths

DEFAULTS = {
    "model": "gemma4:12b",
    "ollama_url": "http://127.0.0.1:11434",
    "out_lang": "en",            # 보내기 기본 언어
    "out_mode": "clipboard",     # clipboard | paste
    "overlay_font": 11,          # 통역 창 글자 크기
    "overlay_alpha": 0.88,
    "overlay_lines": 10,
    "show_original": False,      # 통역 창에 원문도 보이기
    "ad_filter": "fold",         # 광고: show 보이기 | fold 접기(번역 안 함) | hide 숨기기
    "hotkey_input": "Ctrl+Shift+K",
    "game_dir": "",              # 고른 게임 폴더(예: ...\\World of Warcraft\\_classic_) — 애드온을 넣을 곳
    "wow_roots": [],             # 사용자가 직접 지정한 WoW 설치 폴더(자동으로 못 찾을 때)
    "setup_done": False,
    "keep_logs": True,           # 분석용 추적 기록·화면 저장
    "preload": True,             # 켤 때 모델을 GPU 에 미리 올리기
    "input_on": True,            # 런처를 켜면 보내기 입력창도 켜기
    "welcomed": False,           # 첫 실행 화면을 지났나
    "onboarded": False,          # 홈 도움말(말풍선)을 봤나
    "update_check": True,        # 켤 때 GitHub 에서 새 버전 확인(6시간에 한 번)
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(paths.SETTINGS.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save(changes: dict) -> dict:
    cfg = load()
    cfg.update({k: v for k, v in changes.items() if k in DEFAULTS})
    paths.ensure()
    tmp = paths.SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(paths.SETTINGS)
    return cfg
