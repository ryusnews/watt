"""진단 기록 보내기(#135) — 다른 PC(PC방 등)에서 돈 기록을 개발자가 받아 분석하도록 WATT 서버로.

사용자가 정보 → '진단 기록 보내기'에서 무엇을 보내는지 보고 '보내기'를 눌렀을 때만. 한 번 30MB 까지, 서버는 14일 뒤 지운다.
보내는 것: 번역 기록 · 추적 기록(최근 2일) · 화면 캡처(최근 2일, 새것부터 최대 80장) · 실행 로그(끝부분) · 설정(설치 ID 뺌) ·
채팅 영역 · 시스템 정보. 다른 플레이어 이름과 채팅이 들어 있다 — 화면에 그렇게 알린다.
"""
import io
import json
import platform
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import VERSION, paths, report, settings, system

API = report.API + "/v1/diag"
MAX_BYTES = 30 * 1024 * 1024
FRAMES_MAX = 80
LOG_TAIL = 2 * 1024 * 1024  # 실행 로그는 끝부분만(ollama.log 는 수십 MB 가 되기도 한다)
DAYS = 2


def _days() -> set[str]:
    return {time.strftime("%Y%m%d", time.localtime(time.time() - 86400 * i)) for i in range(DAYS)}


def _tail(p: Path, n: int = LOG_TAIL) -> bytes:
    with p.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - n))
        return f.read()


def _plan() -> dict:
    """보낼 파일 — {arcname: Path | bytes}, 화면 캡처 수."""
    logs, days = paths.LOGS, _days()
    files: dict[str, Path | bytes] = {}
    for name in ("live.jsonl", "outgoing.jsonl", "app.log", "live.log", "live_error.log", "ollama.log"):
        p = logs / name
        if p.exists():
            files[f"logs/{name}"] = _tail(p) if name.endswith(".log") else p
    for p in sorted((logs / "trace").glob("trace_*.jsonl")):
        if p.stem.split("_")[-1] in days:
            files[f"logs/trace/{p.name}"] = p
    frames = sorted((p for d in days for p in (logs / "frames" / d).glob("*.png")), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in frames[:FRAMES_MAX]:
        files[f"logs/frames/{p.parent.name}/{p.name}"] = p
    for name in ("chat_region.json", "chat_region_good.json", "wow_vram.json", "runtime.json", "live_status.json"):
        p = paths.DATA / name
        if p.exists():
            files[name] = p
    cfg = {k: v for k, v in settings.load().items() if k not in ("install_id", "report_last_hash")}
    files["settings.json"] = json.dumps(cfg, ensure_ascii=False, indent=1).encode()
    info = {"version": VERSION, "portable": paths.PORTABLE, "os": platform.platform(), "sent": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        info["system"] = system.system_info()
        info["vram"] = system.vram_plan()
    except Exception as e:  # 시스템 정보가 안 돼도 기록은 보낸다
        info["system_error"] = str(e)
    files["info.json"] = json.dumps(info, ensure_ascii=False, indent=1, default=str).encode()
    return {"files": files, "frames": min(len(frames), FRAMES_MAX)}


def preview() -> dict:
    """보내기 전에 보일 요약 — {size, frames, trace_days, logs}."""
    plan = _plan()
    size = sum(len(v) if isinstance(v, bytes) else v.stat().st_size for v in plan["files"].values())
    return {"size": size, "frames": plan["frames"],
            "trace": sum(1 for k in plan["files"] if k.startswith("logs/trace/")),
            "has_live": "logs/live.jsonl" in plan["files"]}


def build() -> bytes:
    """zip — 30MB 를 넘으면 오래된 화면 캡처부터 뺀다."""
    files = _plan()["files"]
    frames = [k for k in files if k.startswith("logs/frames/")]  # 새것부터
    while True:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, v in files.items():
                if isinstance(v, bytes):
                    z.writestr(name, v)
                else:
                    z.write(v, name)
        data = buf.getvalue()
        if len(data) <= MAX_BYTES or not frames:
            return data
        for k in frames[-max(1, len(frames) // 4):]:
            files.pop(k, None)
        frames = frames[:-max(1, len(frames) // 4)]


def send() -> dict:
    """{ok, id, size} · {error}."""
    data = build()
    if len(data) > MAX_BYTES:
        return {"error": "기록이 너무 큽니다"}
    req = urllib.request.Request(API, data=data, method="POST", headers={
        **report.UA, "Content-Type": "application/zip", "X-WATT-Install": report.install_id(), "X-WATT-Ver": VERSION})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            res = json.load(r)
        return {"ok": True, "id": res.get("id"), "size": len(data)}
    except urllib.error.HTTPError as e:
        code = {413: "기록이 너무 큽니다", 429: "오늘은 더 보낼 수 없습니다 — 내일 다시", 503: "서버가 바쁩니다 — 잠시 뒤 다시"}
        return {"error": code.get(e.code, f"서버 오류({e.code})")}
    except (urllib.error.URLError, OSError) as e:
        return {"error": f"보내지 못했습니다 — 인터넷 연결을 확인해 주세요({e})"}
