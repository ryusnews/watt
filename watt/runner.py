"""AI 실행기(Ollama) — 두 방식(사용자 결정, 2026-10-02):
- **system**: PC 에 설치된 Ollama(`ollama --version` 으로 확인) — 더 받지 않는다. 포트 11434, 모델 폴더는 Ollama 설정을 따른다.
  WATT 는 끄지 않고, WATT 가 올린 모델만 내린다
- **watt**: WATT 전용(아래). 설정(runner_mode)이 비면 PC 에 Ollama 가 있으면 system, 없으면 watt

WATT 전용 실행기(Ollama 공식 포터블):

- 실행 파일: 공식 zip(github.com/ollama/ollama 릴리스, 확인값 고정)을 WATT 데이터 폴더 `ollama\\` 에 푼다. 설치 · 관리자 권한 ·
  트레이 앱 · 채팅 화면 없이 `ollama.exe serve` 만 띄운다
- 포트: 11535 — PC 의 Ollama(11434)와 부딪히지 않는다
- 모델: 기본은 데이터 폴더 `models\\`, 사용자가 고르면 그 폴더 아래 `WATT-models\\`(고른 폴더의 다른 파일은 건드리지 않는다)
- 런처가 켜고 끈다(런처를 닫으면 통역 창 · 입력창과 함께). 지우기는 폴더 둘(실행기 · 모델)만 지우면 끝
"""
import ctypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from ctypes import wintypes as wt
from pathlib import Path
from urllib.parse import urlparse

from . import paths

VERSION = "0.35.0"
_REL = f"https://github.com/ollama/ollama/releases/download/v{VERSION}/"
# 이름: (주소, 바이트, SHA-256) — GitHub 릴리스의 digest
PACKS = {
    "base": (_REL + "ollama-windows-amd64.zip", 1461196158,
             "d6f7d3dd4f5d013553a78c1e78b2521fcf41d43dd2863e4596cdc046fe6036db"),  # CPU · NVIDIA(CUDA)
    "rocm": (_REL + "ollama-windows-amd64-rocm.zip", 256101888,
             "5b4e3fe4d67cf68829644042d9cff0cf8c1cc7f31c6c1578c01992cc1b35f997"),  # AMD 그래픽 카드면 함께
}
PORT = 11535
URL = f"http://127.0.0.1:{PORT}"
SYSTEM_URL = "http://127.0.0.1:11434"
DIR = paths.DATA / "ollama"
EXE = DIR / "ollama.exe"
PID = paths.DATA / "ollama.pid"
STORE_NAME = "WATT-models"
MARK = ".watt"  # WATT 가 만든 모델 폴더 표시 — 지울 때 이것이 있는 폴더만
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_lock = threading.Lock()
last_error = ""  # 켜기에 실패한 까닭(ollama.log 끝) — 화면에 그대로


# ---- 방식
def system_exe() -> str | None:
    """PC 에 설치된 Ollama — 명령으로 찾고(PATH), 없으면 기본 설치 위치."""
    found = shutil.which("ollama")
    if found and not found.lower().startswith(str(DIR).lower()):
        return found
    for c in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
              Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Ollama" / "ollama.exe"):
        if c.exists():
            return str(c)
    return None


_sysver: dict = {}


def system_version() -> str | None:
    """`ollama --version` — 실행되는지까지 본다. 한 번만(1–2초)."""
    exe = system_exe()
    if not exe:
        return None
    if exe not in _sysver:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
            words = (out.stdout + out.stderr).split()
            _sysver[exe] = next((w for w in reversed(words) if w[:1].isdigit()), None) if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            _sysver[exe] = None
    return _sysver[exe]


def mode() -> str:
    """'system' · 'watt' — 고른 것, 안 골랐으면 PC 에 Ollama 가 있으면 system."""
    from . import settings
    m = settings.load().get("runner_mode") or ""
    if m in ("system", "watt"):
        return m
    return "system" if system_exe() else "watt"


def url() -> str:
    return SYSTEM_URL if mode() == "system" else URL


# ---- 경로
def models_dir(custom: str | None = None) -> Path:
    """모델 폴더 — 설정의 models_dir(고른 폴더) 아래 WATT-models, 없으면 데이터 폴더 models."""
    if custom is None:
        from . import settings
        custom = settings.load().get("models_dir") or ""
    return Path(custom) / STORE_NAME if custom else paths.DATA / "models"


def _mark(store: Path) -> None:
    store.mkdir(parents=True, exist_ok=True)
    (store / MARK).write_text("WATT 가 받은 번역 모델 — WATT 를 지우면 함께 지워집니다\n", encoding="utf-8")


def store_size(store: Path | None = None) -> int:
    store = store or models_dir()
    total = 0
    for root, _, files in os.walk(store):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ---- 받기
def need(amd: bool = False) -> list[str]:
    """받아야 할 묶음."""
    if EXE.exists() and (not amd or (DIR / "lib" / "ollama" / "rocm").exists()):
        return []
    return ["base", "rocm"] if amd else ["base"]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install(amd: bool = False, progress=None, cancel: threading.Event | None = None) -> None:
    """공식 zip 을 받아 확인값을 보고 푼다 — 풀기는 새 폴더에 한 뒤 바꿔 끼운다(중간에 끊겨도 옛 것이 남는다)."""
    from .system import download
    packs = need(amd)
    if not packs:
        return
    total = sum(PACKS[p][1] for p in packs)
    done_before = 0
    zips = []
    for p in packs:
        url, size, sha = PACKS[p]
        dest = paths.DOWNLOADS / url.rsplit("/", 1)[-1]
        if not (dest.exists() and dest.stat().st_size == size and _sha256(dest) == sha):
            download(url, dest, (lambda d, t, b=done_before: progress(b + d, total)) if progress else None, cancel)
            if _sha256(dest) != sha:
                dest.unlink(missing_ok=True)
                raise RuntimeError(f"받은 파일의 확인값이 맞지 않습니다({dest.name}) — 다시 받아 주세요")
        done_before += size
        zips.append(dest)
    stop_watt()
    tmp = DIR.with_name("ollama.new")
    shutil.rmtree(tmp, ignore_errors=True)
    if DIR.exists() and "base" not in packs:  # rocm 만 더할 때는 지금 것 위에
        shutil.copytree(DIR, tmp)
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            zf.extractall(tmp)
    if not (tmp / "ollama.exe").exists():
        raise RuntimeError("받은 묶음에 ollama.exe 가 없습니다")
    shutil.rmtree(DIR, ignore_errors=True)
    tmp.rename(DIR)
    for z in zips:
        z.unlink(missing_ok=True)


# ---- 켜기 · 끄기
def _get(path: str, timeout: float = 1.5):
    with urllib.request.urlopen(url() + path, timeout=timeout) as r:
        return json.load(r)


# 실행기(와 그것이 띄우는 모델 프로세스 — 낮은 우선순위는 물려받는다)를 '보통 아래' 로. 모델을 올리거나 CPU 로 번역할 때
# CPU 가 100% 가 되어도 Windows 가 와우에 먼저 준다 — 번역이 조금 늦을 뿐 게임은 끊기지 않게(PC방 i5-14400F, #130)
BELOW_NORMAL = 0x00004000


def _port_open(timeout: float = 0.25) -> bool:
    """실행기 포트가 열려 있나 — Windows 는 아무도 안 듣는 포트에 연결하면 거절을 바로 주지 않고 시간 초과까지 기다려
    (1.5초) 실행기가 꺼져 있으면 런처 get_state 가 3초씩 걸렸다(PC방 2026-10-05, #125). 켜져 있으면 연결은 바로 된다."""
    u = urlparse(url())
    try:
        with socket.create_connection((u.hostname, u.port), timeout=timeout):
            return True
    except OSError:
        return False


def running() -> bool:
    if not _port_open():
        return False
    try:
        _get("/api/version")
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _start_system(wait: float) -> bool:
    """PC 의 Ollama 를 켠다(트레이 앱이 있으면 그것, 없으면 serve) — 꺼져 있을 때만."""
    exe = system_exe()
    if not exe:
        return False
    app = Path(exe).with_name("ollama app.exe")
    if app.exists():
        subprocess.Popen([str(app)], creationflags=NO_WINDOW, close_fds=True)
    else:
        subprocess.Popen([exe, "serve"], creationflags=NO_WINDOW | 0x00000008, close_fds=True)  # DETACHED_PROCESS
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if running():
            return True
        time.sleep(0.5)
    return running()


def _log_tail(n: int = 6) -> str:
    try:
        lines = (paths.LOGS / "ollama.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return " / ".join(l.strip()[-160:] for l in lines[-n:] if l.strip())


def start(wait: float = 45) -> bool:
    """실행기를 띄운다(이미 떠 있으면 그대로). 전용이면 모델 폴더는 OLLAMA_MODELS 로. 실패하면 last_error 에 까닭."""
    global last_error
    if running():
        last_error = ""
        return True
    if mode() == "system":
        return _start_system(wait)
    with _lock:
        if running():
            return True
        if not EXE.exists():
            last_error = "WATT 전용 실행기를 아직 받지 않았습니다"
            return False
        store = models_dir()
        _mark(store)
        env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{PORT}", "OLLAMA_MODELS": str(store)}
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        log = (paths.LOGS / "ollama.log").open("ab")
        try:
            p = subprocess.Popen([str(EXE), "serve"], env=env, cwd=str(DIR), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                 creationflags=NO_WINDOW | 0x00000200 | BELOW_NORMAL, close_fds=True)  # CREATE_NEW_PROCESS_GROUP
        except OSError as e:  # 실행이 막힘(PC방 보안 프로그램 · 실행 제한 정책 등)
            last_error = f"실행할 수 없습니다: {e}"
            return False
        PID.write_text(str(p.pid), encoding="utf-8")
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if running():
            last_error = ""
            return True
        if p.poll() is not None:
            last_error = f"바로 꺼짐(코드 {p.returncode}) — {_log_tail()}"
            return False
        time.sleep(0.3)
    if running():
        last_error = ""
        return True
    last_error = f"{int(wait)}초 동안 응답 없음 — {_log_tail()}"
    return False


def _our_pids() -> list[int]:
    """WATT 실행기 폴더의 실행 파일로 돈 프로세스(serve 와 모델 runner) — PC 의 다른 Ollama 는 빼고."""
    from .screen import exe_of_pid
    arr = (wt.DWORD * 4096)()
    got = wt.DWORD()
    if not ctypes.windll.psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(got)):
        return []
    base = str(DIR).lower()
    return [pid for pid in arr[:got.value // ctypes.sizeof(wt.DWORD)]
            if pid and exe_of_pid(pid).lower().startswith(base)]


def stop() -> None:
    """WATT 실행기와 그 모델 프로세스를 끈다(VRAM 비우기). PC 의 Ollama 를 쓰면 끄지 않고 WATT 모델만 내린다."""
    if mode() == "system":
        _unload_watt_models()
        return
    stop_watt()


def _unload_watt_models() -> None:
    from .system import MODELS
    mine = {m["name"] for m in MODELS}
    try:
        for m in _get("/api/ps").get("models", []):
            if m["name"] in mine:
                req = urllib.request.Request(url() + "/api/generate", data=json.dumps({"model": m["name"], "keep_alive": 0}).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass


def unload_all() -> list[str]:
    """올라간 모델 내리기(VRAM 비우기) — WATT 전용이면 모두, PC 의 Ollama 면 WATT 모델만. 내린 이름."""
    from .system import MODELS
    mine = {m["name"] for m in MODELS}
    gone = []
    try:
        for m in _get("/api/ps").get("models", []):
            if mode() == "watt" or m["name"] in mine:
                req = urllib.request.Request(url() + "/api/generate", data=json.dumps({"model": m["name"], "keep_alive": 0}).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=15).read()
                gone.append(m["name"])
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    return gone


def stop_watt() -> None:
    """WATT 전용 실행기 프로세스를 끈다."""
    # 실행 파일 경로로만 고른다 — 남은 PID 파일의 번호는 다른 프로그램이 다시 쓸 수 있다
    for pid in _our_pids():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PID.unlink(missing_ok=True)
    for _ in range(20):
        if not _our_pids():
            break
        time.sleep(0.2)


def status() -> dict:
    up = running()
    models, loaded, ver = [], [], None
    if up:
        try:
            ver = _get("/api/version").get("version")
            models = [{"name": m["name"], "size_gb": round(m["size"] / 1024 ** 3, 1)} for m in _get("/api/tags")["models"]]
            loaded = [{"name": m["name"], "vram_gb": round(m.get("size_vram", 0) / 1024 ** 3, 1)}
                      for m in _get("/api/ps").get("models", [])]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            pass
    m = mode()
    exe = system_exe() if m == "system" else (str(EXE) if EXE.exists() else None)
    return {"mode": m, "chosen": _chosen(), "installed": bool(exe), "exe": exe, "running": up, "version": ver,
            "models": models, "loaded": loaded, "watt": m == "watt", "models_dir": str(models_dir()) if m == "watt" else None,
            "download": sum(PACKS[p][1] for p in need()), "watt_ready": EXE.exists(), "error": "" if up else last_error,
            "system": {"exe": system_exe(), "version": system_version()} if system_exe() else None}


def _chosen() -> str:
    from . import settings
    return settings.load().get("runner_mode") or ""


def set_mode(m: str) -> None:
    """방식 바꾸기 — 지금 실행기의 WATT 모델을 내리고(전용이면 끄고) 고른 쪽을 켠다."""
    from . import settings
    if m not in ("system", "watt"):
        raise ValueError(m)
    if mode() != m:
        stop()
    settings.save({"runner_mode": m})
    start()


# ---- 지우기 · 옮기기
def remove(models: bool = True) -> None:
    """WATT 전용 실행기(와 모델)를 지운다 — WATT 가 만든 폴더만. PC 의 Ollama 는 그대로."""
    stop_watt()
    shutil.rmtree(DIR, ignore_errors=True)
    shutil.rmtree(DIR.with_name("ollama.new"), ignore_errors=True)
    if models:
        store = models_dir()
        if store.exists() and ((store / MARK).exists() or store == paths.DATA / "models"):
            shutil.rmtree(store, ignore_errors=True)


def move_models(new_parent: str, progress=None, cancel: threading.Event | None = None) -> str:
    """모델 폴더를 옮긴다 — 실행기를 끄고, 파일을 옮기고(같은 드라이브면 이름만 바꿈), 설정을 바꾸고, 다시 켠다."""
    from . import settings
    src, dst = models_dir(), models_dir(new_parent)
    if src.resolve() == dst.resolve():
        return str(dst)
    if dst.exists() and any(p.name != MARK for p in dst.iterdir()):
        raise RuntimeError(f"{dst} 에 이미 파일이 있습니다 — 다른 폴더를 골라 주세요")
    was = running() and mode() == "watt"
    stop_watt()
    if src.exists():
        files = [Path(r) / f for r, _, fs in os.walk(src) for f in fs]
        total = sum(f.stat().st_size for f in files) or 1
        free = shutil.disk_usage(Path(new_parent).anchor or new_parent).free
        if os.path.splitdrive(str(src))[0].lower() != os.path.splitdrive(str(dst))[0].lower() and free < total:
            raise RuntimeError(f"옮길 곳의 빈 공간이 부족합니다(필요 {total / 1024 ** 3:.1f}GB)")
        done = 0
        for f in files:
            if cancel and cancel.is_set():
                raise RuntimeError("취소됨")
            to = dst / f.relative_to(src)
            to.parent.mkdir(parents=True, exist_ok=True)
            size = f.stat().st_size
            shutil.move(str(f), str(to))
            done += size
            if progress:
                progress(done, total)
        shutil.rmtree(src, ignore_errors=True)
    _mark(dst)
    settings.save({"models_dir": new_parent})
    if was:
        start()
    return str(dst)


def system_store() -> Path | None:
    """PC 의 Ollama 모델 폴더(있으면) — 같은 모델을 다시 받지 않고 가져오기."""
    p = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")
    return p if (p / "manifests").exists() else None


def system_models() -> list[str]:
    root = system_store()
    if not root:
        return []
    lib = root / "manifests" / "registry.ollama.ai" / "library"
    return sorted(f"{m.name}:{t.name}" for m in lib.iterdir() if m.is_dir() for t in m.iterdir() if t.is_file()) \
        if lib.exists() else []


def import_model(name: str, progress=None, cancel: threading.Event | None = None) -> None:
    """PC 의 Ollama 에 이미 받은 모델을 WATT 모델 폴더로 — 같은 드라이브면 하드 링크(공간을 더 쓰지 않음), 아니면 복사."""
    src = system_store()
    if not src:
        raise RuntimeError("PC 에 받은 Ollama 모델이 없습니다")
    model, _, tag = name.partition(":")
    rel = Path("manifests") / "registry.ollama.ai" / "library" / model / (tag or "latest")
    man = json.loads((src / rel).read_text(encoding="utf-8"))
    digests = [man["config"]["digest"]] + [l["digest"] for l in man["layers"]]
    dst = models_dir()
    _mark(dst)
    blobs = [(src / "blobs" / d.replace(":", "-"), dst / "blobs" / d.replace(":", "-")) for d in digests]
    total = sum(a.stat().st_size for a, _ in blobs) or 1
    done = 0
    for a, b in blobs:
        if cancel and cancel.is_set():
            raise RuntimeError("취소됨")
        b.parent.mkdir(parents=True, exist_ok=True)
        if not b.exists():
            try:
                os.link(a, b)
            except OSError:
                shutil.copyfile(a, b)
        done += a.stat().st_size
        if progress:
            progress(done, total)
    (dst / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src / rel, dst / rel)
