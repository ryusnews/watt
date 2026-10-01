"""업데이트 — GitHub 최신 릴리스 확인 → 설치 파일 받기 → SHA-256 대조 → 조용히 설치(끝나면 다시 켜짐).

밖으로 나가는 HTTPS 요청만 쓴다(Windows 방화벽 창이 뜨지 않는다). 앱이 직접 받은 파일에는 '인터넷에서 받음'
표시가 붙지 않아 SmartScreen 창도 뜨지 않으므로, 대신 GitHub 가 알려 주는 해시와 반드시 맞춰 본다.
"""
import hashlib
import json
import re
import subprocess
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import VERSION, paths, system

REPO = "ryusnews/watt"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE = f"https://github.com/{REPO}/releases/latest"
ASSET = re.compile(r"^WATT-Setup-(\d+\.\d+\.\d+)\.exe$")
TRUSTED_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}


def parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def check(timeout: float = 6.0) -> dict:
    """최신 릴리스 — {version, newer, url, size, sha256, page, notes}."""
    req = urllib.request.Request(API, headers={"User-Agent": f"WATT/{VERSION}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rel = json.load(r)
    ver = rel["tag_name"].lstrip("v")
    asset = next((a for a in rel.get("assets", []) if ASSET.match(a["name"])), None)
    sha = None
    if asset and str(asset.get("digest", "")).startswith("sha256:"):
        sha = asset["digest"].split(":", 1)[1]
    if not sha:  # 예전 릴리스 — 노트에 적은 해시
        m = re.search(r"SHA-256:\s*`?([0-9a-f]{64})", rel.get("body") or "")
        sha = m.group(1) if m else None
    return {"version": ver, "current": VERSION, "newer": parse(ver) > parse(VERSION),
            "url": asset and asset["browser_download_url"], "size": asset and asset["size"], "sha256": sha,
            "page": rel.get("html_url") or PAGE, "notes": (rel.get("body") or "")[:3000]}


def download(info: dict, progress=None, cancel: threading.Event | None = None) -> Path:
    """설치 파일을 받아 해시를 맞춰 본다. 다르면 지우고 오류."""
    if not info.get("url") or not info.get("sha256"):
        raise RuntimeError("설치 파일이나 확인값이 없는 릴리스입니다")
    if urlparse(info["url"]).hostname not in TRUSTED_HOSTS:
        raise RuntimeError("믿을 수 없는 주소입니다")
    dest = paths.DOWNLOADS / f"WATT-Setup-{info['version']}.exe"
    system.download(info["url"], dest, progress, cancel)
    h = hashlib.sha256(dest.read_bytes()).hexdigest()
    if h != info["sha256"]:
        dest.unlink(missing_ok=True)
        raise RuntimeError("받은 파일의 확인값이 맞지 않아 설치하지 않았습니다")
    return dest


def install(setup: Path) -> None:
    """조용히 설치하고 끝나면 다시 켠다(installer/watt.iss 의 RELAUNCH). 부르는 쪽은 바로 꺼져야 한다."""
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — 런처가 꺼져도 계속
    subprocess.Popen([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                      "/FORCECLOSEAPPLICATIONS", "/RELAUNCH=1"], creationflags=flags, close_fds=True)
