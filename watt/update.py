"""업데이트 — GitHub 릴리스 확인 → 파일 받기 → SHA-256 대조 → 바꿔 끼우고 다시 켜기.

설치판: WATT-Setup-<버전>.exe 를 조용히 실행(설치 프로그램이 끝나면 다시 켠다, installer/watt.iss RELAUNCH).
포터블: WATT-Portable-<버전>.zip 을 data\\update 에 풀고, 그 안의 새 WATT.exe 가 옛 WATT 가 꺼지길 기다렸다가
        파일을 덮어쓰고 다시 켠다(--role apply-update). 스크립트 없이 WATT.exe 하나로.
밖으로 나가는 HTTPS 요청만 쓴다(Windows 방화벽 창이 뜨지 않는다). 앱이 직접 받은 파일에는 '인터넷에서 받음'
표시가 붙지 않아 SmartScreen 창도 뜨지 않으므로, 대신 GitHub 가 알려 주는 해시와 반드시 맞춰 본다.
"""
import ctypes
import hashlib
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
from pathlib import Path
from urllib.parse import urlparse

from . import VERSION, paths, system

REPO = "ryusnews/watt"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
PAGE = f"https://github.com/{REPO}/releases/latest"
ASSETS = {"setup": re.compile(r"^WATT-Setup-(\d+\.\d+\.\d+)\.exe$"),
          "portable": re.compile(r"^WATT-Portable-(\d+\.\d+\.\d+)\.zip$")}
TRUSTED_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — 부른 쪽이 꺼져도 계속
log = logging.getLogger("watt")


def kind() -> str:
    return "portable" if paths.PORTABLE else "setup"


def parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def check(timeout: float = 6.0) -> dict:
    """내 종류(포터블/설치판) 파일이 있는 가장 새 릴리스 — {version, newer, kind, url, size, sha256, page, notes}.
    설치판 파일이 없는 릴리스가 있어도(포터블만 낸 버전) 설치판 사용자는 설치판이 있는 버전만 본다."""
    req = urllib.request.Request(API, headers={"User-Agent": f"WATT/{VERSION}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rels = json.load(r)
    k = kind()
    best = None
    for rel in rels:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        asset = next((a for a in rel.get("assets", []) if ASSETS[k].match(a["name"])), None)
        if asset and (best is None or parse(rel["tag_name"]) > parse(best[0]["tag_name"])):
            best = (rel, asset)
    if not best:
        return {"version": VERSION, "current": VERSION, "newer": False, "kind": k, "page": PAGE}
    rel, asset = best
    ver = rel["tag_name"].lstrip("v")
    sha = asset["digest"].split(":", 1)[1] if str(asset.get("digest", "")).startswith("sha256:") else None
    if not sha:  # 예전 릴리스 — 노트에 적은 해시
        m = re.search(re.escape(asset["name"]) + r"`?\s*SHA-256:\s*`?([0-9a-f]{64})", rel.get("body") or "")
        sha = m.group(1) if m else None
    return {"version": ver, "current": VERSION, "newer": parse(ver) > parse(VERSION), "kind": k,
            "url": asset["browser_download_url"], "size": asset["size"], "sha256": sha,
            "page": rel.get("html_url") or PAGE, "notes": (rel.get("body") or "")[:3000]}


def download(info: dict, progress=None, cancel: threading.Event | None = None) -> Path:
    """파일을 받아 해시를 맞춰 본다. 다르면 지우고 오류."""
    if not info.get("url") or not info.get("sha256"):
        raise RuntimeError("파일이나 확인값이 없는 릴리스입니다")
    if urlparse(info["url"]).hostname not in TRUSTED_HOSTS:
        raise RuntimeError("믿을 수 없는 주소입니다")
    dest = paths.DOWNLOADS / Path(urlparse(info["url"]).path).name
    system.download(info["url"], dest, progress, cancel)
    h = hashlib.sha256(dest.read_bytes()).hexdigest()
    if h != info["sha256"]:
        dest.unlink(missing_ok=True)
        raise RuntimeError("받은 파일의 확인값이 맞지 않아 설치하지 않았습니다")
    return dest


def start(info: dict, path: Path) -> None:
    """받은 파일로 바꿔 끼우기를 시작한다 — 부르는 쪽은 잠금을 풀고 바로 꺼져야 한다."""
    if info["kind"] == "setup":
        subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                          "/FORCECLOSEAPPLICATIONS", "/RELAUNCH=1"], creationflags=DETACHED, close_fds=True)
        return
    stage = paths.UPDATE_STAGE / info["version"]
    shutil.rmtree(stage, ignore_errors=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(stage)
    new_exe = next(stage.rglob("WATT.exe"), None)
    if not new_exe:
        raise RuntimeError("받은 파일에 WATT.exe 가 없습니다")
    subprocess.Popen([str(new_exe), "--role", "apply-update", "--target", str(paths.APP_DIR), "--pid", str(os.getpid())],
                     creationflags=DETACHED, close_fds=True, cwd=str(new_exe.parent))


# ---- 포터블: 새 WATT.exe 가 옛 폴더를 덮어쓴다(새 버전 쪽 코드로 돈다)
def _wait_pid(pid: int, timeout: float = 60) -> None:
    h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        ctypes.windll.kernel32.WaitForSingleObject(h, int(timeout * 1000))
        ctypes.windll.kernel32.CloseHandle(h)


def apply_portable(target: str, pid: int) -> int:
    """data\\update\\<버전>\\WATT\\WATT.exe 로 실행된다: 옛 WATT 가 꺼지길 기다림 → 옛 프로그램 파일(data 빼고) 지우고 새 것 복사 → 다시 켜기."""
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
    """다시 켜진 뒤 data\\update 정리 — 덮어쓰기 프로세스가 끝날 때까지 몇 번 다시 해 본다."""
    def work():
        for _ in range(30):
            if not paths.UPDATE_STAGE.exists():
                return
            shutil.rmtree(paths.UPDATE_STAGE, ignore_errors=True)
            time.sleep(2)
    threading.Thread(target=work, daemon=True).start()
