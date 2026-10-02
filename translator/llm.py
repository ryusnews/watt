import json
import time
import urllib.error
import urllib.request

from watt import runner

class _Url(str):
    """llm.URL — 쓸 때마다 지금 방식의 주소(PC 의 Ollama 11434 · WATT 전용 11535)."""
    def __add__(self, other):
        return runner.url() + other


URL = _Url()


def think_for(model: str):
    """추론을 끌 수 없는 모델(gpt-oss)은 가장 짧게."""
    return "low" if model.startswith("gpt-oss") else False


def chat_json(model: str, system: str, user: str, schema: dict, keep_alive: str = "30m", num_ctx: int = 2048) -> tuple[dict, float]:
    body = {"model": model, "stream": False, "think": think_for(model), "format": schema, "keep_alive": keep_alive,
            "options": {"temperature": 0, "num_ctx": num_ctx},
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
    req = urllib.request.Request(URL + "/api/generate", data=json.dumps({"model": model, "keep_alive": keep_alive}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except (urllib.error.URLError, OSError):
        pass
