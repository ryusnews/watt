"""데이터 폴더 정리 — 기록·캐시가 한도 없이 쌓이지 않게.

켤 때(prune): 화면 캡처·추적 기록은 최근 KEEP_DAYS 일만, 화면 캡처는 전체 FRAMES_MAX 까지,
             덧붙이는 기록은 ROTATE_BYTES 를 넘으면 새 파일(옛 것 ROTATE_KEEP 개까지), 다 쓴 설치·업데이트 파일 지우기.
"""
import logging
import logging.handlers
import shutil
import time
from pathlib import Path

from . import paths

KEEP_DAYS = 14
FRAMES_MAX = 200 * 1024 ** 2
ROTATE_BYTES = 5 * 1024 ** 2
ROTATE_KEEP = 2
APPEND_LOGS = ("live.jsonl", "outgoing.jsonl", "update.log")  # 프로세스가 열어 두지 않는 것 — 켤 때 돌린다
log = logging.getLogger("watt")


def size_of(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def file_handler(name: str) -> logging.Handler:
    """app.log · live.log — 크기가 넘으면 스스로 새 파일로."""
    paths.ensure()
    h = logging.handlers.RotatingFileHandler(paths.LOGS / name, maxBytes=ROTATE_BYTES, backupCount=ROTATE_KEEP,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    return h


def _rotate(f: Path) -> bool:
    if not f.exists() or f.stat().st_size < ROTATE_BYTES:
        return False
    for i in range(ROTATE_KEEP, 0, -1):
        src = f.with_name(f"{f.name}.{i - 1}") if i > 1 else f
        dst = f.with_name(f"{f.name}.{i}")
        if src.exists():
            dst.unlink(missing_ok=True)
            src.rename(dst)
    return True


def prune() -> dict:
    """켤 때 한 번. 지운 양(바이트)을 돌려준다."""
    freed = 0
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - KEEP_DAYS * 86400))
    frames, trace = paths.LOGS / "frames", paths.LOGS / "trace"
    try:
        days = sorted(d for d in frames.iterdir() if d.is_dir()) if frames.exists() else []
        for d in days:  # 오래된 날짜
            if d.name < cutoff:
                freed += size_of(d)
                shutil.rmtree(d, ignore_errors=True)
        days = [d for d in days if d.exists()]
        total = sum(size_of(d) for d in days)
        for d in days:  # 그래도 크면 오래된 날부터
            if total <= FRAMES_MAX:
                break
            s = size_of(d)
            shutil.rmtree(d, ignore_errors=True)
            total -= s
            freed += s
        for f in trace.glob("trace_*.jsonl") if trace.exists() else []:
            if f.stem[6:] < cutoff:
                freed += f.stat().st_size
                f.unlink(missing_ok=True)
        for name in APPEND_LOGS:
            _rotate(paths.LOGS / name)
        if paths.DOWNLOADS.exists():  # 지난번에 받아 다 쓴 설치·업데이트 파일
            for f in paths.DOWNLOADS.iterdir():
                if time.time() - f.stat().st_mtime > 600:
                    freed += size_of(f)
                    shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
    except OSError as e:
        log.warning("prune: %s", e)
    if freed:
        log.info("prune freed %.1f MB", freed / 1024 ** 2)
    return {"freed": freed}


def usage() -> dict:
    """번역 설정 → 기록에 보이는 용량."""
    parts = {"frames": paths.LOGS / "frames", "trace": paths.LOGS / "trace", "downloads": paths.DOWNLOADS,
             "webview": paths.WEBVIEW}
    out = {k: size_of(v) for k, v in parts.items()}
    out["logs"] = sum(f.stat().st_size for f in paths.LOGS.glob("*") if f.is_file()) if paths.LOGS.exists() else 0
    out["total"] = size_of(paths.DATA) if paths.DATA != paths.RES else sum(out.values())
    return out


def clear() -> dict:
    """기록 지우기 — 화면 캡처·추적 기록·번역 기록·받은 파일. 설정·채팅 영역·지금 쓰는 앱 기록은 남긴다."""
    before = usage()
    for d in (paths.LOGS / "frames", paths.LOGS / "trace", paths.DOWNLOADS):
        shutil.rmtree(d, ignore_errors=True)
    for f in paths.LOGS.glob("*"):
        if f.is_file() and f.name not in ("app.log", "live.log"):  # 열려 있는 기록은 남긴다
            try:
                f.unlink()
            except OSError:
                pass
    after = usage()
    return {"freed": max(0, before["total"] - after["total"]), "usage": after}
