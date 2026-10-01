"""업데이트 — 설치 없이 바뀐 파일만 받아 두고, 다음에 켤 때 바꿔 끼운다(설치판 · 포터블 공통, #15).

1. 받아 두기(stage): 가장 새 릴리스의 WATT-Portable-<버전>.zip 목록(CRC)과 이 PC 파일을 비교해 바뀐 것만
   HTTP Range 로 받는다(0.1.6 → 0.1.7: 43MB 중 6.8MB). zip 이 CRC 를 확인한다. 부분 받기가 안 되면 zip 전체를
   받아 GitHub 가 알려 주는 SHA-256 과 맞춰 본 뒤 같은 방식으로. 결과: update\\<버전>\\files + ready.json
2. 바꿔 끼우기(launch_apply → apply_delta): 켤 때(또는 '지금 다시 시작') 지금 프로그램을 update\\runner 로 복사해
   거기서 --role apply-delta 로 돈다 → 옛 WATT 가 꺼지길 기다림 → 옛 파일은 update\\<버전>\\backup 으로 옮기고
   새 파일을 넣는다(실패하면 되돌림) → 설치판이면 '앱 및 기능' 버전 고침 → 다시 켜기.
옛 방식(--role apply-update): 0.1.3–0.1.7 포터블이 새 zip 을 통째로 풀어 새 WATT.exe 로 부른다 — 옮겨 가기용으로 남긴다.
밖으로 나가는 HTTPS 요청만 쓴다(Windows 방화벽 창이 뜨지 않는다).
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urlparse

from . import VERSION, paths, system

REPO = "ryusnews/watt"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
PAGE = f"https://github.com/{REPO}/releases/latest"
ZIP = re.compile(r"^WATT-Portable-(\d+\.\d+\.\d+)\.zip$")
TRUSTED_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — 부른 쪽이 꺼져도 계속
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{8C2E7A51-5B7D-4E3A-9C4F-2D6B1A9E7C30}_is1"
UA = {"User-Agent": f"WATT/{VERSION}"}
log = logging.getLogger("watt")


def kind() -> str:
    return "portable" if paths.PORTABLE else "setup"


def parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def check(timeout: float = 6.0) -> dict:
    """포터블 zip 이 있는 가장 새 릴리스 — {version, current, newer, kind, url, size, sha256, page, notes, ready}."""
    req = urllib.request.Request(API, headers={**UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rels = json.load(r)
    best = None
    for rel in rels:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        asset = next((a for a in rel.get("assets", []) if ZIP.match(a["name"])), None)
        if asset and (best is None or parse(rel["tag_name"]) > parse(best[0]["tag_name"])):
            best = (rel, asset)
    ready = pending()
    if not best:
        return {"version": VERSION, "current": VERSION, "newer": False, "kind": kind(), "page": PAGE, "ready": ready}
    rel, asset = best
    ver = rel["tag_name"].lstrip("v")
    sha = asset["digest"].split(":", 1)[1] if str(asset.get("digest", "")).startswith("sha256:") else None
    if not sha:  # 예전 릴리스 — 노트에 적은 해시
        m = re.search(re.escape(asset["name"]) + r"`?\s*SHA-256:\s*`?([0-9a-f]{64})", rel.get("body") or "")
        sha = m.group(1) if m else None
    return {"version": ver, "current": VERSION, "newer": parse(ver) > parse(VERSION), "kind": kind(),
            "url": asset["browser_download_url"], "size": asset["size"], "sha256": sha,
            "page": rel.get("html_url") or PAGE, "notes": (rel.get("body") or "")[:3000], "ready": ready}


# ---- 1. 받아 두기
class RangeFile(io.RawIOBase):
    """HTTP Range 로 읽는 파일 — zipfile 이 목록(끝부분)과 필요한 파일만 읽는다."""

    def __init__(self, url: str):
        self.src = url
        self.pos = 0
        self.fetched = 0
        self._resolve()

    def _resolve(self) -> None:  # GitHub 는 서명된 주소로 넘긴다(몇 분 뒤 만료 → 다시)
        with urllib.request.urlopen(urllib.request.Request(self.src, headers={**UA, "Range": "bytes=0-0"}), timeout=20) as r:
            if r.status != 206 or urlparse(r.url).hostname not in TRUSTED_HOSTS:
                raise OSError("부분 받기를 쓸 수 없습니다")
            self.url, self.size = r.url, int(r.headers["Content-Range"].rsplit("/", 1)[1])

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        for attempt in range(3):
            try:
                req = urllib.request.Request(self.url, headers={**UA, "Range": f"bytes={self.pos}-{self.pos + n - 1}"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
                break
            except OSError:
                if attempt == 2:
                    raise
                self._resolve()
        b[:len(data)] = data
        self.pos += len(data)
        self.fetched += len(data)
        return len(data)


def _crc(p: Path) -> int:
    c = 0
    with p.open("rb") as f:
        while chunk := f.read(1 << 20):
            c = zlib.crc32(chunk, c)
    return c


def plan_of(z: zipfile.ZipFile, app: Path) -> tuple[list[zipfile.ZipInfo], list[str]]:
    """(받을 것, 지울 것). zip 은 WATT\\... — portable.txt · data 는 건드리지 않는다. 지우기는 _internal 안만."""
    want, names = [], set()
    for zi in z.infolist():
        if zi.is_dir() or not zi.filename.startswith("WATT/"):
            continue
        rel = zi.filename[5:]
        if rel == "portable.txt" or rel.startswith("data/"):
            continue
        names.add(rel)
        local = app / rel
        if not local.is_file() or local.stat().st_size != zi.file_size or _crc(local) != zi.CRC:
            want.append(zi)
    if "WATT.exe" not in names:
        raise RuntimeError("받은 목록에 WATT.exe 가 없습니다")
    gone = sorted(str(p.relative_to(app)).replace("\\", "/") for p in (app / "_internal").rglob("*")
                  if p.is_file() and str(p.relative_to(app)).replace("\\", "/") not in names) if (app / "_internal").exists() else []
    return want, gone


def stage(info: dict, progress=None, cancel: threading.Event | None = None, app: Path | None = None) -> dict:
    """바뀐 파일만 받아 update\\<버전> 에 둔다. 이미 받아 뒀으면 그대로."""
    app = app or paths.APP_DIR
    ver = info["version"]
    d = paths.UPDATE_STAGE / ver
    if (d / "ready.json").exists():
        return json.loads((d / "ready.json").read_text(encoding="utf-8"))
    if not info.get("url") or urlparse(info["url"]).hostname not in TRUSTED_HOSTS:
        raise RuntimeError("받을 주소가 없습니다")
    shutil.rmtree(d, ignore_errors=True)
    t0 = time.time()
    try:
        rf = RangeFile(info["url"])
        z, how = zipfile.ZipFile(io.BufferedReader(rf, 1 << 20)), "range"
    except (OSError, zipfile.BadZipFile) as e:  # 부분 받기가 안 되면 통째로(SHA-256 대조)
        log.info("range failed (%s) — whole zip", e)
        rf, z, how = None, zipfile.ZipFile(download(info, progress, cancel)), "whole"
    with z:
        want, gone = plan_of(z, app)
        total, done = sum(zi.compress_size for zi in want), 0
        for zi in want:
            if cancel and cancel.is_set():
                raise RuntimeError("취소했습니다")
            dst = d / "files" / zi.filename[5:]
            dst.parent.mkdir(parents=True, exist_ok=True)
            with z.open(zi) as src, dst.open("wb") as out:  # 다 읽으면 zipfile 이 CRC 를 맞춰 본다
                shutil.copyfileobj(src, out, 1 << 20)
            done += zi.compress_size
            if progress:
                progress(done, total)
    plan = {"version": ver, "from": VERSION, "files": [zi.filename[5:] for zi in want], "delete": gone, "how": how,
            "bytes": rf.fetched if rf else info.get("size", 0), "sec": round(time.time() - t0, 1)}
    (d / "ready.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")  # 마지막에 — 반쯤 받은 것은 안 씀
    log.info("staged %s: %d files, %d removed, %.1f MB via %s", ver, len(want), len(gone), plan["bytes"] / 1e6, how)
    return plan


def download(info: dict, progress=None, cancel: threading.Event | None = None) -> Path:
    """zip 전체를 받아 해시를 맞춰 본다. 다르면 지우고 오류."""
    if not info.get("url") or not info.get("sha256"):
        raise RuntimeError("파일이나 확인값이 없는 릴리스입니다")
    if urlparse(info["url"]).hostname not in TRUSTED_HOSTS:
        raise RuntimeError("믿을 수 없는 주소입니다")
    dest = paths.DOWNLOADS / Path(urlparse(info["url"]).path).name
    system.download(info["url"], dest, progress, cancel)
    h = hashlib.sha256(dest.read_bytes()).hexdigest()
    if h != info["sha256"]:
        dest.unlink(missing_ok=True)
        raise RuntimeError("받은 파일의 확인값이 맞지 않아 쓰지 않았습니다")
    return dest


def pending() -> str | None:
    """받아 두고 아직 안 바꾼 새 버전."""
    best = None
    if paths.UPDATE_STAGE.exists():
        for f in paths.UPDATE_STAGE.glob("*/ready.json"):
            ver = f.parent.name
            if parse(ver) > parse(VERSION) and (best is None or parse(ver) > parse(best)):
                best = ver
    return best


# ---- 2. 바꿔 끼우기
def launch_apply(ver: str) -> None:
    """지금 프로그램을 update\\runner 로 복사해 거기서 바꿔 끼우기를 돌린다 — 부르는 쪽은 잠금을 풀고 바로 꺼져야 한다."""
    runner = paths.UPDATE_STAGE / "runner"
    shutil.rmtree(runner, ignore_errors=True)
    runner.mkdir(parents=True)
    shutil.copy2(paths.APP_DIR / "WATT.exe", runner / "WATT.exe")
    shutil.copytree(paths.APP_DIR / "_internal", runner / "_internal")
    subprocess.Popen([str(runner / "WATT.exe"), "--role", "apply-delta", "--target", str(paths.APP_DIR),
                      "--stage", str(paths.UPDATE_STAGE / ver), "--pid", str(os.getpid()),
                      "--log", str(paths.LOGS / "update.log")],
                     creationflags=DETACHED, close_fds=True, cwd=str(runner))


def _wait_pid(pid: int, timeout: float = 60) -> None:
    h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        ctypes.windll.kernel32.WaitForSingleObject(h, int(timeout * 1000))
        ctypes.windll.kernel32.CloseHandle(h)


def _procs_of(exe: Path) -> list[int]:
    """이 exe 로 돌고 있는 프로세스(통역 창 · 입력창이 늦게 꺼질 때)."""
    arr = (wt.DWORD * 4096)()
    got = wt.DWORD()
    ctypes.windll.psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(got))
    want, out = str(exe).lower(), []
    for pid in arr[:got.value // 4]:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        buf, n = ctypes.create_unicode_buffer(1024), wt.DWORD(1024)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) and buf.value.lower() == want:
            out.append(pid)
        ctypes.windll.kernel32.CloseHandle(h)
    return out


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    for i in range(40):  # 막 꺼진 프로세스가 파일을 잠깐 쥐고 있을 수 있다
        try:
            os.replace(src, dst)
            return
        except OSError:
            if i == 39:
                raise
            time.sleep(0.5)


def apply_delta(target: str, stage_dir: str, pid: int, logfile: str | None = None) -> int:
    """update\\runner\\WATT.exe --role apply-delta 로 돈다: 기다림 → 옛 파일 옮겨 두기 → 새 파일 넣기 → 다시 켜기."""
    app, d = Path(target), Path(stage_dir)
    if logfile:
        logging.basicConfig(filename=logfile, level=logging.INFO, encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
    done: list[tuple[Path, Path | None]] = []  # (넣은 자리, 옮겨 둔 옛 파일)
    try:
        plan = json.loads((d / "ready.json").read_text(encoding="utf-8"))
        if not (app / "WATT.exe").exists():  # 엉뚱한 폴더를 건드리지 않게
            raise RuntimeError(f"WATT 폴더가 아닙니다: {app}")
        _wait_pid(pid)
        exe = app / "WATT.exe"
        for _ in range(30):  # 통역 창 · 입력창
            if not _procs_of(exe):
                break
            time.sleep(0.5)
        else:
            for p in _procs_of(exe):
                subprocess.run(["taskkill", "/PID", str(p), "/F"], capture_output=True)
        backup = d / "backup"
        for rel in plan["files"]:
            dst, old = app / rel, backup / rel
            if dst.exists():
                _move(dst, old)
                done.append((dst, old))
            else:
                done.append((dst, None))
            _move(d / "files" / rel, dst)
        for rel in plan["delete"]:
            dst = app / rel
            if dst.exists():
                _move(dst, backup / rel)
                done.append((dst, backup / rel))
        if not (app / "portable.txt").exists():  # 설치판 — '앱 및 기능'에 보이는 버전
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, plan["version"])
            except OSError:
                pass
        (d / "applied").write_text(plan["version"], encoding="utf-8")
        logging.info("updated %s → %s: %d files, %d removed", plan.get("from"), plan["version"], len(plan["files"]),
                     len(plan["delete"]))
    except Exception:
        logging.exception("update failed — rolling back")
        for dst, old in reversed(done):
            try:
                if old is None:
                    dst.unlink(missing_ok=True)
                else:
                    _move(old, dst)
            except OSError:
                logging.exception("rollback %s", dst)
        shutil.rmtree(d, ignore_errors=True)  # 같은 것을 또 시도하지 않게
    subprocess.Popen([str(app / "WATT.exe")], creationflags=DETACHED, close_fds=True, cwd=str(app))
    return 0


def apply_portable(target: str, pid: int) -> int:
    """옛 방식(0.1.3–0.1.7 포터블이 부른다): data\\update\\<버전>\\WATT\\WATT.exe 로 실행 → 옛 WATT 가 꺼지길 기다림 →
    옛 프로그램 파일(data 빼고) 지우고 새 것 복사 → 다시 켜기."""
    dst = Path(target)
    src = Path(sys.executable).resolve().parent
    (dst / "data" / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=dst / "data" / "logs" / "update.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        if not ((dst / "WATT.exe").exists() and (dst / "portable.txt").exists()):  # 엉뚱한 폴더를 지우지 않게
            raise RuntimeError(f"포터블 WATT 폴더가 아닙니다: {dst}")
        _wait_pid(pid)
        time.sleep(0.5)
        for _ in range(20):  # 통역 창·입력창이 늦게 꺼지면 파일이 잠겨 있다
            try:
                shutil.rmtree(dst / "_internal")
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.5)
        for item in src.iterdir():
            if item.name in ("data",):
                continue
            target_item = dst / item.name
            if item.is_dir():
                shutil.copytree(item, target_item, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target_item)
        logging.info("updated %s from %s", dst, src)
    except Exception:
        logging.exception("update failed")
    subprocess.Popen([str(dst / "WATT.exe")], creationflags=DETACHED, close_fds=True, cwd=str(dst))
    return 0


def cleanup_stage() -> None:
    """다시 켜진 뒤 정리 — 바꿔 끼우기에 쓴 runner, 끝난 버전(지금 버전 이하). 받아 두고 아직 안 쓴 새 버전은 남긴다."""
    def work():
        for _ in range(30):
            left = [p for p in paths.UPDATE_STAGE.iterdir()
                    if p.name == "runner" or not re.fullmatch(r"\d+\.\d+\.\d+", p.name) or parse(p.name) <= parse(VERSION)] \
                if paths.UPDATE_STAGE.exists() else []
            if not left:
                return
            for p in left:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
            time.sleep(2)
    threading.Thread(target=work, daemon=True).start()
