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
from . import runner
class _Url(str):
    """쓸 때마다 지금 방식의 주소 — runner.url()(PC 의 Ollama 11434 · WATT 전용 11535)."""
    def __add__(self, other):
        return runner.url() + other


OLLAMA_URL = _Url()

# 번역 모델 — VRAM 에 맞춰 추천. verified: 평가 세트(보내기 23·받기 22·사전 15)로 검증했는가
MODELS = [
    # vram_gb: 실제로 잰 값(nvidia-smi 차이, 문맥 2048, RTX 5080, 2026-10-02) — e2b 3.1 · e4b 4.7 · 12b 8.1GB.
    # Ollama 의 /api/ps 크기는 e2b · e4b 를 0.2GB 로 보여 믿을 수 없다
    {"name": "gemma4:12b", "label": "Gemma 4 12B", "download_gb": 7.7, "vram_gb": 8.1, "min_vram": 11,
     "verified": True, "note": "가장 정확 · 문장당 약 0.3~1.3초(게임 중)"},
    {"name": "gemma4:e4b", "label": "Gemma 4 E4B", "download_gb": 6.6, "vram_gb": 4.8, "min_vram": 7,
     "verified": False, "note": "VRAM 8GB 급 · 검증 전"},
    {"name": "gemma4:e2b", "label": "Gemma 4 E2B", "download_gb": 4.6, "vram_gb": 3.2, "min_vram": 0,
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
    """전체 VRAM 으로(남은 양을 못 잴 때)."""
    for m in MODELS:
        if vram_gb >= m["min_vram"]:
            return m["name"]
    return MODELS[-1]["name"]


WOW_VRAM_GB = 4.0   # 와우 몫 기본 어림(1920×1009 에서 4–5GB) — 이 PC 에서 잰 값이 있으면 그것(wow_vram)
_wow_cache: list = [0.0, None]


def _wow_measure(pids: list[int]) -> float | None:
    """와우 프로세스들이 쓰는 전용 VRAM(GB) — Windows 성능 카운터(GPU Process Memory), 5분 캐시.
    재는 데 PowerShell 로 1.7초 — 런처 get_state 가 5분마다 그만큼 멈췄다(#117). 그래서 뒤에서 재고, 그동안은 지난 값
    (처음엔 None → wow_vram 이 기억한 값 · 해상도 어림)을 돌려준다."""
    if time.monotonic() - _wow_cache[0] >= 300 and not _wow_busy.locked():
        threading.Thread(target=_wow_refresh, args=(list(pids),), daemon=True).start()
    return _wow_cache[1]


_wow_busy = threading.Lock()


def _wow_refresh(pids: list[int]) -> None:
    if not _wow_busy.acquire(blocking=False):
        return
    try:
        got = None
        try:
            q = ",".join(f"'\\GPU Process Memory(pid_{p}_*)\\Dedicated Usage'" for p in pids)
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-Counter {q} -ErrorAction SilentlyContinue).CounterSamples | ForEach-Object {{ $_.CookedValue }}"],
                                 capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
            vals = [float(x) for x in out.stdout.split() if x.replace(".", "").replace(",", "").isdigit()]
            got = sum(vals) / 1024 ** 3 if vals else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        _wow_cache[:] = [time.monotonic(), got]
    finally:
        _wow_busy.release()


def wow_vram(pids: list[int] | None = None) -> tuple[float, str]:
    """와우 몫(GB)과 근거 — 켜져 있으면 재서 기억(PC방 와우 포에버 2560×1440: 약 5GB, 기본 어림 4GB 보다 컸다),
    꺼져 있으면 기억한 값, 없으면 화면 해상도로 어림(1080p 3.5 · 1440p 5 · 4K 6GB)."""
    f = paths.DATA / "wow_vram.json"
    try:
        saved = json.loads(f.read_text(encoding="utf-8")).get("gb")
    except (OSError, ValueError):
        saved = None
    if pids:
        now = _wow_measure(pids)
        if now and now > 0.5:
            gb = now if not saved else max(now, 0.8 * saved + 0.2 * now)  # 큰 쪽으로 — 모자라는 것보다 낫다
            try:
                paths.ensure()
                f.write_text(json.dumps({"gb": round(gb, 2), "t": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
            except OSError:
                pass
            return gb, "measured"
    if saved:
        return saved, "remembered"
    px = ctypes.windll.user32.GetSystemMetrics(0) * ctypes.windll.user32.GetSystemMetrics(1)
    return (3.5 if px <= 2.2e6 else 5.0 if px <= 3.8e6 else 6.0), "resolution"
MARGIN_GB = 0.8     # 여유 — 창 · 브라우저 · 드라이버가 조금씩 더 쓴다
_vram_cache: list = [0.0, None]


def vram_used_gb() -> float | None:
    """지금 쓰고 있는 VRAM(GB) — NVIDIA 는 nvidia-smi(빠름), 아니면 Windows 성능 카운터(제조사 상관없이, ~2초). 30초 캐시."""
    if time.monotonic() - _vram_cache[0] < 30:
        return _vram_cache[1]
    used = None
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True,
                                 text=True, timeout=5, creationflags=NO_WINDOW)
            vals = [float(x) for x in out.stdout.split() if x.replace(".", "").isdigit()]
            used = max(vals) / 1024 if vals else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    if used is None:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-Counter '\\GPU Adapter Memory(*)\\Dedicated Usage').CounterSamples | "
                                  "ForEach-Object { $_.CookedValue }"], capture_output=True, text=True, timeout=15,
                                 creationflags=NO_WINDOW)
            vals = [float(x) for x in out.stdout.split() if x.replace(".", "").replace(",", "").isdigit()]
            used = max(vals) / 1024 ** 3 if vals else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    _vram_cache[:] = [time.monotonic(), used]
    return used


def vram_plan(ai_gpu: bool = False) -> dict:
    """번역 모델에 쓸 수 있는 VRAM — 지금 남은 양에서, 와우가 꺼져 있으면 와우 몫(예상)을 빼고, WATT 모델이 이미 올라가 있으면
    그만큼은 돌려받는다(바꾸면 내리니까). 전체 VRAM 만 보면 와우를 켰을 때 모자라 공유 메모리로 넘어가 느려졌다
    (16GB 에 12b + 와우 + AI 글자 인식 GPU → 공유 메모리 8GB, 2026-10-02)."""
    gl = gpus()
    best = max(gl, key=lambda g: g.get("vram_gb", 0)) if gl else None
    total = best["vram_gb"] if best else 0
    used = vram_used_gb()
    from . import screen
    wins = screen.list_windows(True)
    wows = len(wins)
    wow = wows > 0
    wow_gb, wow_how = wow_vram([w["pid"] for w in wins] if wow else None)
    watt_loaded = 0.0
    try:
        mine = {m["name"]: m["vram_gb"] for m in MODELS}
        # GPU 에 올라간 것만 돌려받는다(CPU 로 돌리는 중이면 VRAM 을 쓰지 않는다 — size_vram 0)
        watt_loaded = sum(mine.get(m["name"], 0) for m in runner.status().get("loaded", []) if m.get("vram_gb", 0) > 0)
    except Exception:
        pass
    if used is None:
        return {"total": total, "used": None, "wow": wow, "wow_est": 0, "available": None,
                "pick": recommend_model(total)}
    avail = total - used + watt_loaded - (0 if wow else wow_gb) - (1.5 if ai_gpu else 0) - MARGIN_GB
    fit = next((m for m in MODELS if m["vram_gb"] <= avail), None)
    pick = (fit or MODELS[-1])["name"]
    # 다 안 들어가면 Ollama 가 일부를 CPU 로 돌린다 — 느려진다. 와우 창을 줄이거나 그래픽 설정을 낮추면 남는다
    short = 0 if fit else round(MODELS[-1]["vram_gb"] - avail, 1)
    from . import settings
    dev = settings.load().get("llm_device") or "auto"
    return {"total": round(total, 1), "used": round(used, 1), "wow": wow, "wows": wows, "wow_est": 0 if wow else round(wow_gb, 1),
            "wow_gb": round(wow_gb, 1), "wow_how": wow_how, "watt_loaded": round(watt_loaded, 1), "available": round(avail, 1),
            "pick": pick, "short": short, "device": dev if dev != "auto" else ("cpu" if short > 0 else "gpu"), "device_pick": dev}


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
    before = ocr.installed()
    codes = [c for c in codes if c in ocr.PACKS and not before.get(c)]
    caps = [ocr.PACKS[c] for c in codes]
    if not caps:
        return ocr_status()
    cmd = " & ".join(f"dism /Online /Add-Capability /CapabilityName:{c} /NoRestart" for c in caps)
    code = run_elevated("cmd.exe", f'/c "echo WATT: Windows OCR 언어 팩 설치 중 — 창이 닫힐 때까지 기다려 주세요 & {cmd}"')
    after = ocr.installed()
    for c in codes:
        if after.get(c):
            remember(paths.INSTALLED_OCR, c)
    st = ocr_status()
    st["exit_code"] = code
    return st


# ---- Ollama
def remove_ocr(codes: list[str]) -> dict:
    """OCR 언어 팩 지우기 — WATT 가 설치한 팩만(원래 있던 팩은 손대지 않는다). 관리자 권한 창."""
    mine = set(read_list(paths.INSTALLED_OCR))
    codes = [c for c in codes if c in mine and c in ocr.PACKS]
    caps = [ocr.PACKS[c] for c in codes]
    if caps:
        cmd = " & ".join(f"dism /Online /Remove-Capability /CapabilityName:{c} /NoRestart" for c in caps)
        code = run_elevated("cmd.exe", f'/c "echo WATT: Windows OCR 언어 팩 지우는 중 & {cmd}"')
    else:
        code = 0
    after = ocr.installed()
    for c in codes:
        if not after.get(c):
            forget(paths.INSTALLED_OCR, c)
    st = ocr_status()
    st["exit_code"] = code
    return st


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
    """WATT 전용 실행기 상태(installed · running · models · loaded · models_dir · download)."""
    return runner.status()


def legacy_ollama_status() -> dict:
    """옛 방식(PC 에 설치한 Ollama) — WATT 가 설치했던 것을 정리할 때만."""
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
    """실행기를 켠다 — 못 켜면 까닭을."""
    if not runner.start():
        raise RuntimeError(f"AI 실행기를 켜지 못했습니다 — {runner.last_error or '까닭 모름'}")
    return runner.status()


def ollama_uninstall() -> dict:
    """WATT 전용 실행기와 모델을 지운다(폴더 둘). 옛 방식으로 WATT 가 설치한 Ollama 가 있으면 그 제거 프로그램도."""
    runner.remove(models=True)
    forget_all = read_list(paths.INSTALLED_MODELS)
    for m in forget_all:
        forget(paths.INSTALLED_MODELS, m)
    if read_list(paths.INSTALLED_OLLAMA) and ollama_exe():
        legacy_ollama_uninstall()
    return runner.status()


def legacy_ollama_uninstall() -> dict:
    """Ollama 자체 제거 프로그램을 연다(받은 모델 폴더는 Ollama 가 남긴다)."""
    exe = ollama_exe()
    if not exe:
        return legacy_ollama_status()
    unins = next(iter(sorted(Path(exe).parent.glob("unins*.exe"))), None)
    if not unins:
        raise RuntimeError("Ollama 제거 프로그램을 찾지 못했습니다. 설정 → 앱에서 지워 주세요")
    subprocess.run([str(unins)], check=False)
    if not ollama_exe():
        write_list(paths.INSTALLED_OLLAMA, [])
    return legacy_ollama_status()


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


def amd_gpu() -> bool:
    """번역에 쓸 그래픽 카드(VRAM 이 가장 큰 것)가 AMD 인가 — Ryzen 내장 그래픽(Radeon)이 함께 잡혀도 RTX 가 있으면 아니다."""
    gl = gpus()
    if not gl:
        return False
    best = max(gl, key=lambda g: g.get("vram_gb", 0))
    return "amd" in best["name"].lower() or "radeon" in best["name"].lower()


def ollama_install(progress=None, cancel=None) -> dict:
    """WATT 전용 실행기(공식 포터블 zip)를 받아 풀고 켠다 — 설치 창 · 관리자 권한 없이. AMD 그래픽 카드면 ROCm 묶음도."""
    runner.install(amd_gpu(), progress, cancel)
    runner.set_mode("watt")
    if not runner.running():
        raise RuntimeError(f"받았지만 AI 실행기를 켜지 못했습니다 — {runner.last_error or '까닭 모름'}")
    return runner.status()


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


def installed_by_watt() -> dict:
    """WATT 가 설치한 것(원래 있던 것은 빠진다) — 앱의 지우기 버튼·'설치한 것 정리'·삭제 프로그램이 이것만 다룬다."""
    return {"models": read_list(paths.INSTALLED_MODELS), "ocr": read_list(paths.INSTALLED_OCR),
            "addons": [d for d in read_list(paths.INSTALLED_ADDONS)
                       if (Path(d) / "Interface" / "AddOns" / "ChatFontCJK").exists()],
            "ollama": runner.EXE.exists() or (bool(read_list(paths.INSTALLED_OLLAMA)) and bool(ollama_exe()))}


def model_delete(name: str) -> dict:
    """Ollama 모델 지우기."""
    req = urllib.request.Request(OLLAMA_URL + "/api/delete", data=json.dumps({"model": name}).encode(),
                                 headers={"Content-Type": "application/json"}, method="DELETE")
    urllib.request.urlopen(req, timeout=60).read()
    forget(paths.INSTALLED_MODELS, name)
    return ollama_status()


def model_warm(name: str) -> float:
    """모델을 GPU 에 올려 둔다(첫 번역이 기다리지 않게). 걸린 초."""
    t0 = time.monotonic()
    from translator import llm
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=json.dumps({"model": name, "keep_alive": "30m",
                                                                                "options": llm.options()}).encode(),
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


def remember(listfile: Path, item: str) -> None:
    """삭제할 때 지울 수 있게 WATT 가 넣은 것을 적어 둔다(한 줄에 하나). 삭제 프로그램(Inno)은 BOM 이 없으면
    ANSI 로 읽어 한글 경로가 깨지므로 UTF-8 BOM 으로 쓴다."""
    lines = read_list(listfile)
    if item.lower() not in (x.lower() for x in lines):
        write_list(listfile, lines + [item])


def forget(listfile: Path, item: str) -> None:
    write_list(listfile, [x for x in read_list(listfile) if x.lower() != item.lower()])


def read_list(listfile: Path) -> list[str]:
    try:
        return [x.strip() for x in listfile.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    except OSError:
        return []


def write_list(listfile: Path, lines: list[str]) -> None:
    paths.ensure()
    listfile.write_text("".join(x + "\r\n" for x in lines), encoding="utf-8-sig", newline="")


def remove_addon(flavor_dir: str) -> dict:
    """게임 폴더에서 ChatFontCJK 만 지운다."""
    dst = Path(flavor_dir) / "Interface" / "AddOns" / "ChatFontCJK"
    if dst.is_dir():
        shutil.rmtree(dst)
    forget(paths.INSTALLED_ADDONS, str(Path(flavor_dir)))
    return {"dir": str(dst), "removed": not dst.exists()}


def install_addon(flavor_dir: str) -> dict:
    """ChatFontCJK(중국어·러시아어가 □ 없이 보이게 하는 글꼴 애드온)를 AddOns 에 복사. 게임을 완전히 재시작해야 적용."""
    dst = Path(flavor_dir) / "Interface" / "AddOns" / "ChatFontCJK"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(paths.ADDON, dst, dirs_exist_ok=True)
    remember(paths.INSTALLED_ADDONS, str(Path(flavor_dir)))
    return {"dir": str(dst), "version": addon_version(Path(flavor_dir))}
