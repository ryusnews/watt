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
    "ai_langs": [],              # AI 글자 인식으로 보강할 언어(ko · zh · ru) — 켤 때 그 모델만 받는다
    "ai_gpu": False,             # AI 글자 인식을 GPU(DirectML)로 — VRAM 을 1–2GB 더 쓴다(크기마다 작업 공간을 잡아 둔다). 기본 CPU
    "hidden_names": [],          # 통역 창에서 숨긴 사람(#13) — 이 PC에서만
    "ad_allow": [],              # 광고 아님으로 고친 사람(자동 판정이 틀렸을 때)
    "ad_block": [],              # 광고로 고친 사람
    "show_korean": True,         # 한국어 메시지도 번역 없이 그대로 보이기(채팅창과 순서 맞춰 보기)
    "ad_filter": "fold",         # 광고: show 보이기 | fold 접기(번역 안 함) | hide 숨기기
    "chat_newest": "bottom",     # 채팅창에서 새 메시지가 나타나는 쪽: bottom(기본) | top(역순 정렬 애드온)
    "install_id": "",            # 무작위 설치 ID(신고 되풀이 막기용, 사람 · PC 와 무관)
    "report_last_at": 0,         # 마지막 인식 오류 신고(24시간에 1번)
    "report_last_hash": "",
    "hotkey_input": "Ctrl+Shift+K",
    "game_dir": "",              # 고른 게임 폴더(예: ...\\World of Warcraft\\_classic_) — 애드온을 넣을 곳
    "llm_device": "auto",        # 번역 모델을 돌릴 곳: auto(VRAM 이 모자라면 CPU) · gpu · cpu
    "llm_cpu_share": 25,         # CPU 로 번역할 때 쓸 스레드 몫(%) — 기본 1/4 은 와우 몫을 남긴다
    "runner_mode": "",           # AI 실행기: system(PC 의 Ollama) · watt(WATT 전용) · 비면 PC 에 있으면 system
    "models_dir": "",            # 번역 모델을 둘 폴더(비면 WATT 데이터 폴더 models) — 고르면 그 아래 WATT-models
    "game_exe": "",              # 고른 게임 창의 실행 파일(비면 WoW 창 중 가장 큰 것) — PC방 등 이름이 다를 때
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
