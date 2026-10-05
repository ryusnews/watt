import json
import os
import time
import urllib.error
import urllib.request

from watt import runner

class _Url(str):
    """llm.URL — 쓸 때마다 지금 방식의 주소(PC 의 Ollama 11434 · WATT 전용 11535)."""
    def __add__(self, other):
        return runner.url() + other


URL = _Url()


NUM_CTX = 2048
_dev = {"t": -1e9, "opts": {}}


def device_options() -> dict:
    """번역 모델을 CPU 로 돌릴지 — settings.llm_device: auto(VRAM 이 모자라면 CPU) · gpu · cpu.
    VRAM 이 모자라 모델 일부가 공유 메모리로 넘어가면 표시까지 12초였다(PC방 RTX 4060 + 와우 2개). CPU 만(스레드 1/4 — 와우 몫을
    남김)이면 E2B 문장당 약 3초 · 5개 묶음 7초(Ryzen 7 9850X3D, 2026-10-02). 10분 캐시 — 자주 바뀌면 모델을 다시 올린다."""
    if time.monotonic() - _dev["t"] < 600:
        return _dev["opts"]
    from watt import settings, system
    pick = settings.load().get("llm_device") or "auto"
    cpu = pick == "cpu"
    if pick == "auto":
        try:
            cpu = system.vram_plan().get("short", 0) > 0
        except Exception:
            cpu = False
    _dev.update(t=time.monotonic(), opts={"num_gpu": 0, "num_thread": cpu_threads(settings.load().get("llm_cpu_share"))} if cpu else {})
    return _dev["opts"]


def cpu_threads(share=None) -> int:
    """CPU 로 번역할 때 스레드 수 — 설정의 몫(%, 기본 25 = 1/4). PC방 CPU(20% 사용 중)는 남는 몫이 커서 늘릴 수 있게(#126)."""
    try:
        share = float(share)
    except (TypeError, ValueError):
        share = 25.0
    share = min(100.0, max(5.0, share))
    return max(2, round((os.cpu_count() or 8) * share / 100))


def reset_device() -> None:
    _dev["t"] = -1e9


def options(num_ctx: int = NUM_CTX) -> dict:
    return {"temperature": 0, "num_ctx": num_ctx, **device_options()}


def think_for(model: str):
    """추론을 끌 수 없는 모델(gpt-oss)은 가장 짧게."""
    return "low" if model.startswith("gpt-oss") else False


def chat_json(model: str, system: str, user: str, schema: dict, keep_alive: str = "30m", num_ctx: int = NUM_CTX) -> tuple[dict, float]:
    body = {"model": model, "stream": False, "think": think_for(model), "format": schema, "keep_alive": keep_alive,
            "options": options(num_ctx),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    t0 = time.monotonic()
    for attempt in (1, 2):  # 빈 본문·깨진 JSON 은 한 번 더 묻는다(gpt-oss 에서 가끔 생김)
        req = urllib.request.Request(URL + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
        content = resp.get("message", {}).get("content", "")
        try:
            return json.loads(content), time.monotonic() - t0
        except json.JSONDecodeError:
            if attempt == 2:
                raise ValueError(f"응답이 JSON 이 아님({resp.get('done_reason')}): {content[:80]!r}") from None


def loaded() -> list[str]:
    """지금 Ollama 가 올려 둔 모델."""
    try:
        with urllib.request.urlopen(URL + "/api/ps", timeout=5) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return []


def unload(model: str) -> bool:
    """올라가 있으면 내린다(keep_alive 0). 안 올라간 모델에 보내면 올렸다 내리므로 먼저 확인한다."""
    if model not in loaded():
        return False
    req = urllib.request.Request(URL + "/api/generate", data=json.dumps({"model": model, "keep_alive": 0}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
        return True
    except (urllib.error.URLError, OSError):
        return False


def preload(model: str, keep_alive: str = "30m") -> None:
    """첫 번역이 모델 올리기(수 초)를 기다리지 않게 미리 올린다."""
    # 번역과 같은 옵션으로 — 문맥 길이 · 장치가 다르면 첫 번역에서 다시 올려 미리 올린 보람이 없다
    req = urllib.request.Request(URL + "/api/generate", data=json.dumps({"model": model, "keep_alive": keep_alive,
                                                                         "options": options()}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except (urllib.error.URLError, OSError):
        pass
