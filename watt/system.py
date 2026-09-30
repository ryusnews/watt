"""환경 점검·설치 — 설정 마법사가 쓴다.

GPU·VRAM(레지스트리) · Windows OCR 언어 팩(DISM, 관리자 권한 창) · Ollama(공식 설치 파일) · 번역 모델(ollama pull)
· 게임 폴더 · 글꼴 애드온(ChatFontCJK) 복사.
관리자 권한이 필요한 일은 Windows 권한 확인 창(UAC)을 거쳐 사용자가 허락할 때만 한다.
"""
import ctypes
import json
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import winreg
from ctypes import wintypes as wt
from pathlib import Path

from . import ocr, paths, screen

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"

# 번역 모델 — VRAM 에 맞춰 추천. verified: 평가 세트(보내기 23·받기 22·사전 15)로 검증했는가
MODELS = [
    {"name": "gemma4:12b", "label": "Gemma 4 12B", "download_gb": 7.7, "vram_gb": 8.1, "min_vram": 11,
     "verified": True, "note": "가장 정확 · 문장당 약 0.3~1.3초(게임 중)"},
    {"name": "gemma4:e4b", "label": "Gemma 4 E4B", "download_gb": 6.6, "vram_gb": 7.0, "min_vram": 8,
     "verified": False, "note": "VRAM 8GB 급 · 검증 전"},
    {"name": "gemma4:e2b", "label": "Gemma 4 E2B", "download_gb": 4.6, "vram_gb": 5.0, "min_vram": 0,
     "verified": False, "note": "가벼움 · 정확도 낮을 수 있음 · 검증 전"},
]


# ---- 시스템
def gpus() -> list[dict]:
    """디스플레이 어댑터와 전용 VRAM(레지스트리 HardwareInformation.qwMemorySize) — 제조사 상관없이."""
    out = []
    key = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key)
    except OSError:
        return out
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            k = winreg.OpenKey(root, sub)
            name = winreg.QueryValueEx(k, "DriverDesc")[0]
        except OSError:
            continue
        mem = 0
        for val in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
            try:
                v = winreg.QueryValueEx(k, val)[0]
                mem = int.from_bytes(v, "little") if isinstance(v, bytes) else int(v)
                if mem:
                    break
            except OSError:
                pass
        if "Basic" in name or "Virtual" in name or "Remote" in name:
            continue
        vendor = "nvidia" if "NVIDIA" in name.upper() else "amd" if ("AMD" in name.upper() or "RADEON" in name.upper()) \
            else "intel" if "INTEL" in name.upper() else "other"
        out.append({"name": name, "vram_gb": round(mem / 1024 ** 3, 1), "vendor": vendor})
    uniq = {g["name"]: g for g in out}
    return sorted(uniq.values(), key=lambda g: -g["vram_gb"])


class _MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def system_info() -> dict:
    m = _MEMSTAT()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    gl = gpus()
    best = gl[0] if gl else None
    build = int(platform.version().split(".")[-1]) if platform.version().count(".") >= 2 else 0
    return {"windows": f"Windows {'11' if build >= 22000 else '10'} (빌드 {build})", "build": build,
            "ram_gb": round(m.ullTotalPhys / 1024 ** 3), "gpus": gl, "gpu": best,
            "cpu": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", ""),
            "free_disk_gb": round(shutil.disk_usage(os.environ.get("USERPROFILE", "C:\\")).free / 1024 ** 3)}


def recommend_model(vram_gb: float) -> str:
    for m in MODELS:
        if vram_gb >= m["min_vram"]:
            return m["name"]
    return MODELS[-1]["name"]


# ---- 관리자 권한으로 실행(UAC) 후 끝날 때까지 기다리기
class _SEI(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wt.HWND), ("lpVerb", wt.LPCWSTR),
                ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR), ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int),
                ("hInstApp", wt.HINSTANCE), ("lpIDList", ctypes.c_void_p), ("lpClass", wt.LPCWSTR),
                ("hkeyClass", wt.HKEY), ("dwHotKey", wt.DWORD), ("hIcon", wt.HANDLE), ("hProcess", wt.HANDLE)]


def run_elevated(exe: str, params: str, show: int = 1) -> int:
    """UAC 창을 거쳐 실행하고 끝날 때까지 기다린다. 사용자가 거절하면 -1."""
    sei = _SEI()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb, sei.lpFile, sei.lpParameters, sei.nShow = "runas", exe, params, show
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        return -1
    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
    code = wt.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return code.value


# ---- Windows OCR 언어 팩
def ocr_status() -> dict:
    inst = ocr.installed()
    return {"langs": [{"code": k, "label": ocr.LABELS[k], "installed": v} for k, v in inst.items()],
            "missing": [k for k, v in inst.items() if not v]}


def install_ocr(codes: list[str]) -> dict:
    """빠진 OCR 언어 팩을 DISM 으로 설치 — 관리자 권한 창 하나에서 차례로(창에 진행 상황이 보인다)."""
    caps = [ocr.PACKS[c] for c in codes if c in ocr.PACKS]
    if not caps:
        return ocr_status()
    cmd = " & ".join(f"dism /Online /Add-Capability /CapabilityName:{c} /NoRestart" for c in caps)
    code = run_elevated("cmd.exe", f'/c "echo WATT: Windows OCR 언어 팩 설치 중 — 창이 닫힐 때까지 기다려 주세요 & {cmd}"')
    st = ocr_status()
    st["exit_code"] = code
    return st


# ---- Ollama
def ollama_exe() -> str | None:
    cands = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
             Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Ollama" / "ollama.exe"]
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which("ollama")


def _get(path: str, timeout: float = 2.0):
    with urllib.request.urlopen(OLLAMA_URL + path, timeout=timeout) as r:
        return json.load(r)


def ollama_status() -> dict:
    exe = ollama_exe()
    try:
        ver = _get("/api/version").get("version")
        running = True
    except (urllib.error.URLError, OSError, ValueError):
        ver, running = None, False
    models, loaded = [], []
    if running:
        try:
            models = [{"name": m["name"], "size_gb": round(m["size"] / 1024 ** 3, 1)} for m in _get("/api/tags")["models"]]
            loaded = [{"name": m["name"], "vram_gb": round(m.get("size_vram", 0) / 1024 ** 3, 1)}
                      for m in _get("/api/ps").get("models", [])]
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            pass
    return {"installed": bool(exe), "exe": exe, "running": running, "version": ver, "models": models, "loaded": loaded}


def ollama_start() -> dict:
    """설치돼 있으면 켠다(트레이 앱이 있으면 그것, 없으면 serve)."""
    exe = ollama_exe()
    if not exe:
        return ollama_status()
    app = Path(exe).with_name("ollama app.exe")
    if app.exists():
        subprocess.Popen([str(app)], creationflags=NO_WINDOW, close_fds=True)
    else:
        subprocess.Popen([exe, "serve"], creationflags=NO_WINDOW | 0x00000008, close_fds=True)  # DETACHED_PROCESS
    for _ in range(40):
        time.sleep(0.5)
        st = ollama_status()
        if st["running"]:
            return st
    return ollama_status()


def download(url: str, dest: Path, progress=None, cancel: threading.Event | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "WATT-setup"})
    with urllib.request.urlopen(req, timeout=30) as r, tmp.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel and cancel.is_set():
                raise RuntimeError("취소됨")
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    tmp.replace(dest)
    return dest


def ollama_install(progress=None, cancel=None) -> dict:
    """공식 설치 파일(ollama.com)을 받아 실행 — 설치 창은 Ollama 것이 뜬다."""
    setup = download(OLLAMA_SETUP_URL, paths.DOWNLOADS / "OllamaSetup.exe", progress, cancel)
    subprocess.run([str(setup)], check=False)  # 사용자 영역 설치(관리자 권한 불필요). 끝나면 Ollama 가 스스로 켜진다
    for _ in range(40):
        st = ollama_status()
        if st["running"]:
            return st
        time.sleep(0.5)
    return ollama_start()


def model_pull(name: str, progress=None, cancel: threading.Event | None = None) -> dict:
    """ollama pull — 진행률(status, completed, total)을 넘긴다."""
    req = urllib.request.Request(OLLAMA_URL + "/api/pull", data=json.dumps({"model": name, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        for line in r:
            if cancel and cancel.is_set():
                raise RuntimeError("취소됨")
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev.get("error"):
                raise RuntimeError(ev["error"])
            if progress:
                progress(ev.get("status", ""), ev.get("completed", 0), ev.get("total", 0))
    return ollama_status()


def model_warm(name: str) -> float:
    """모델을 GPU 에 올려 둔다(첫 번역이 기다리지 않게). 걸린 초."""
    t0 = time.monotonic()
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=json.dumps({"model": name, "keep_alive": "30m"}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=300).read()
    return round(time.monotonic() - t0, 1)


# ---- 게임 폴더 · 애드온
FLAVORS = {"_classic_era_": "클래식 오리지널", "_classic_": "클래식", "_classic_beta_": "클래식 베타",
           "_classic_ptr_": "클래식 PTR", "_anniversary_": "기념 서버", "_retail_": "리테일(지원 안 함)",
           "_ptr_": "리테일 PTR(지원 안 함)", "_beta_": "리테일 베타(지원 안 함)"}
SUPPORTED = {"_classic_era_", "_classic_", "_classic_beta_", "_classic_ptr_", "_anniversary_"}


def _is_game_dir(d: Path) -> bool:
    try:
        return d.is_dir() and any(d.glob("Wow*.exe"))
    except OSError:
        return False


def _is_flavor(d: Path) -> bool:
    return d.name.startswith("_") and d.name.endswith("_")


def _wow_roots(extra: list[str] | None = None) -> list[Path]:
    """WoW 설치 폴더 후보 — 사용자가 고른 폴더가 먼저, 그다음 레지스트리·실행 중인 게임·흔한 위치."""
    roots = [Path(x) for x in (extra or [])]
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Blizzard Entertainment\World of Warcraft")):
        try:
            p = Path(winreg.QueryValueEx(winreg.OpenKey(hive, key), "InstallPath")[0])
            roots.append(p.parent if _is_flavor(p) else p)
        except OSError:
            pass
    win = screen.find_game_window()
    if win and win.get("path"):
        d = Path(win["path"]).parent
        roots.append(d.parent if _is_flavor(d) else d)
    for drive in "CDEFG":
        for base in ("Program Files (x86)", "Program Files", "Games", ""):
            roots.append(Path(f"{drive}:\\") / base / "World of Warcraft")
    seen, out = set(), []
    for r in roots:
        k = str(r).lower().rstrip("\\")
        if k not in seen and r.exists():
            seen.add(k)
            out.append(r)
    return out


def resolve_folder(folder: str) -> dict:
    """사용자가 고른 폴더 → {root, dir}. 받는 것: World of Warcraft 폴더, 그 안의 _classic_ 같은 폴더,
    WoW 실행 파일이 바로 있는 폴더, 그 위 폴더(안에 World of Warcraft 가 있을 때). 아니면 ValueError."""
    p = Path(folder)
    if not p.is_dir():
        raise ValueError("폴더를 찾을 수 없습니다")
    if _is_game_dir(p):
        return {"root": str(p.parent if _is_flavor(p) else p), "dir": str(p)}
    try:
        subs = [d for d in sorted(p.iterdir()) if _is_flavor(d) and _is_game_dir(d)]
    except OSError:
        subs = []
    if subs:
        win = screen.find_game_window()
        run_dir = str(Path(win["path"]).parent).lower() if win else ""
        best = (next((d for d in subs if str(d).lower() == run_dir), None)            # 실행 중인 것
                or next((d for d in subs if d.name in SUPPORTED and addon_version(d)), None)  # 애드온이 있는 것
                or next((d for d in subs if d.name in SUPPORTED), subs[0]))
        return {"root": str(p), "dir": str(best)}
    if (p / "World of Warcraft").is_dir():
        return resolve_folder(str(p / "World of Warcraft"))
    raise ValueError("WoW 폴더가 아닙니다. World of Warcraft 폴더나 그 안의 _classic_ 폴더를 골라 주세요")


def addon_version(flavor_dir: Path) -> str | None:
    toc = flavor_dir / "Interface" / "AddOns" / "ChatFontCJK" / "ChatFontCJK.toc"
    try:
        for line in toc.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("## Version:"):
                return line.split(":", 1)[1].strip()
        return "?"
    except OSError:
        return None


def bundled_addon_version() -> str:
    for line in (paths.ADDON / "ChatFontCJK.toc").read_text(encoding="utf-8").splitlines():
        if line.startswith("## Version:"):
            return line.split(":", 1)[1].strip()
    return "?"


def game_installs(extra_roots: list[str] | None = None) -> list[dict]:
    running = screen.find_game_window()
    run_dir = str(Path(running["path"]).parent).lower() if running else ""
    out, seen = [], set()

    def add(d: Path, flavor: str, label: str, supported: bool, custom: bool):
        if str(d).lower() in seen:
            return
        seen.add(str(d).lower())
        out.append({"dir": str(d), "flavor": flavor, "label": label, "supported": supported,
                    "running": str(d).lower() == run_dir, "addon": addon_version(d), "custom": custom})

    extra = {str(Path(x)).lower() for x in (extra_roots or [])}
    for root in _wow_roots(extra_roots):
        custom = str(root).lower() in extra
        if _is_game_dir(root) and not _is_flavor(root):  # 실행 파일이 바로 있는 설치(버전 폴더 없음)
            add(root, root.name, root.name, True, custom)
        try:
            subs = sorted(root.iterdir())
        except OSError:
            continue
        for d in subs:
            if _is_flavor(d) and _is_game_dir(d):
                add(d, d.name, FLAVORS.get(d.name, d.name), d.name in SUPPORTED, custom)
    return out


def install_addon(flavor_dir: str) -> dict:
    """ChatFontCJK(중국어·러시아어가 □ 없이 보이게 하는 글꼴 애드온)를 AddOns 에 복사. 게임을 완전히 재시작해야 적용."""
    dst = Path(flavor_dir) / "Interface" / "AddOns" / "ChatFontCJK"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(paths.ADDON, dst, dirs_exist_ok=True)
    return {"dir": str(dst), "version": addon_version(Path(flavor_dir))}
