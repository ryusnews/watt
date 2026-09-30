"""배포판 만들기 — WATT.exe(PyInstaller, 폴더형) + 설치 프로그램(Inno Setup).

python tools/build.py            전부
python tools/build.py --no-installer
결과: dist/WATT/WATT.exe, dist/installer/WATT-Setup-<버전>.exe
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from watt import VERSION  # noqa: E402

BUILD, DIST = ROOT / "build", ROOT / "dist"
APP = DIST / "WATT"
ISCC = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe"]
SITE = Path(sys.prefix) / "Lib" / "site-packages"


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


def version_file() -> Path:
    v = tuple(int(x) for x in VERSION.split(".")) + (0,)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041204B0', [
      StringStruct('CompanyName', 'WATT'),
      StringStruct('FileDescription', 'WATT'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'WATT'),
      StringStruct('LegalCopyright', 'WATT'),
      StringStruct('OriginalFilename', 'WATT.exe'),
      StringStruct('ProductName', 'WATT'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1042, 1200])])
  ]
)
"""
    BUILD.mkdir(exist_ok=True)
    p = BUILD / "version_info.txt"
    p.write_text(text, encoding="utf-8")
    return p


def pyinstaller() -> None:
    sep = ";"
    data = [(ROOT / "watt" / "ui", "watt/ui"), (ROOT / "translator" / "wow_terms.json", "translator"),
            (ROOT / "addon" / "ChatFontCJK", "addon/ChatFontCJK")]
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", "WATT", "--windowed",
           "--icon", str(ROOT / "assets" / "watt.ico"), "--version-file", str(version_file()),
           "--distpath", str(DIST), "--workpath", str(BUILD / "pyi"), "--specpath", str(BUILD),
           "--collect-all", "winrt", "--hidden-import", "webview.platforms.edgechromium",
           "--exclude-module", "eval", "--exclude-module", "tools"]
    for src, dst in data:
        cmd += ["--add-data", f"{src}{sep}{dst}"]
    cmd.append(str(ROOT / "watt_main.py"))
    subprocess.run(cmd, check=True, cwd=ROOT)


def _font_copyright(ttf: Path) -> str:
    """TTF name 표에서 저작권 문구(nameID 0) — Sarasa Gothic OFL 고지에 쓴다."""
    b = ttf.read_bytes()
    num = struct.unpack(">H", b[4:6])[0]
    for i in range(num):
        tag, _, off, _ = struct.unpack(">4sIII", b[12 + 16 * i: 28 + 16 * i])
        if tag == b"name":
            _, count, sto = struct.unpack(">HHH", b[off:off + 6])
            for j in range(count):
                pid, eid, lid, nid, ln, o = struct.unpack(">HHHHHH", b[off + 6 + 12 * j: off + 18 + 12 * j])
                if nid == 0 and pid == 3:
                    return b[off + sto + o: off + sto + o + ln].decode("utf-16-be")
    return "Copyright (c) Sarasa Gothic authors"


def notices() -> None:
    """오픈소스 고지 — 함께 배포하는 것들의 라이선스 원문."""
    lic = APP / "licenses"
    lic.mkdir(exist_ok=True)
    fonts = ROOT / "watt" / "ui" / "fonts"
    shutil.copy(fonts / "OFL-IBMPlexSansKR.txt", lic / "IBM-Plex-Sans-KR-OFL.txt")
    shutil.copy(fonts / "OFL-IBMPlexMono.txt", lic / "IBM-Plex-Mono-OFL.txt")
    # Sarasa Gothic(애드온 글꼴) — OFL 본문은 IBM 파일과 같고 저작권 줄만 다르다
    ofl = (fonts / "OFL-IBMPlexSansKR.txt").read_text(encoding="utf-8")
    body = ofl[ofl.index("This Font Software is licensed"):]
    sarasa = _font_copyright(ROOT / "addon" / "ChatFontCJK" / "Fonts" / "SarasaGothicK-Regular.ttf")
    text = sarasa.strip() + "\n\n" + body
    (lic / "Sarasa-Gothic-OFL.txt").write_text(text, encoding="utf-8")
    (ROOT / "addon" / "ChatFontCJK" / "Fonts" / "OFL.txt").write_text(text, encoding="utf-8")  # 게임 폴더로 복사될 때도 함께
    pkgs = {"pywebview": "BSD-3-Clause", "pythonnet": "MIT", "clr_loader": "MIT", "numpy": "BSD-3-Clause",
            "winrt_runtime": "MIT", "bottle": "MIT", "proxy_tools": "MIT", "cffi": "MIT", "pycparser": "BSD-3-Clause",
            "typing_extensions": "PSF-2.0"}
    lines = [f"WATT {VERSION} — 함께 배포하는 오픈소스와 라이선스", ""]
    for pkg, name in pkgs.items():
        for d in SITE.glob(f"{pkg}-*.dist-info"):
            files = [f for f in d.rglob("*") if f.is_file() and f.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))]
            for f in files:
                shutil.copy(f, lic / f"{pkg}-{f.name}")
            lines.append(f"- {pkg} ({name}) {'· ' + ', '.join(f'{pkg}-{f.name}' for f in files) if files else ''}")
    py_lic = Path(sys.prefix) / "LICENSE.txt"
    if py_lic.exists():
        shutil.copy(py_lic, lic / "Python-LICENSE.txt")
    lines += ["- Python (PSF License) · Python-LICENSE.txt", "- Tcl/Tk (BSD 계열) · _internal/_tcl_data/license.terms",
              "- IBM Plex Sans KR / Mono (SIL OFL 1.1) · IBM-Plex-*.txt",
              "- Sarasa Gothic (SIL OFL 1.1, ChatFontCJK 애드온) · Sarasa-Gothic-OFL.txt",
              "- PyInstaller 부트로더 (GPL 2.0 + 배포 예외)", "",
              "WATT 가 설치를 도와주지만 함께 배포하지 않는 것:",
              "- Ollama (MIT) — ollama.com 공식 설치 파일", "- Gemma 4 모델 (Gemma Terms of Use) — Ollama 로 받음",
              "- Windows OCR 언어 팩 — Windows 기능", "",
              "World of Warcraft는 Blizzard Entertainment의 상표입니다. WATT는 Blizzard와 관련 없는 비공식 도구입니다."]
    (APP / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(lines), encoding="utf-8")


def selftest() -> None:
    home = BUILD / "selftest_home"
    shutil.rmtree(home, ignore_errors=True)
    env = {**os.environ, "WATT_HOME": str(home)}
    t0 = time.time()
    code = subprocess.run([str(APP / "WATT.exe"), "--role", "selftest"], env=env, timeout=120).returncode
    report = (home / "logs" / "selftest.json").read_text(encoding="utf-8")
    print(report)
    print(f"selftest exit={code} ({time.time() - t0:.1f}s)")
    if code != 0:
        raise SystemExit("selftest 실패")


def installer() -> Path:
    iscc = next((p for p in ISCC if p.exists()), None)
    if not iscc:
        raise SystemExit("Inno Setup(ISCC.exe)이 없습니다")
    subprocess.run([str(iscc), f"/DAppVersion={VERSION}", str(ROOT / "installer" / "watt.iss")], check=True)
    return DIST / "installer" / f"WATT-Setup-{VERSION}.exe"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-installer", action="store_true")
    args = ap.parse_args()
    step("아이콘·마법사 그림")
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_assets.py")], check=True)
    step("WATT.exe (PyInstaller)")
    pyinstaller()
    step("오픈소스 고지")
    notices()
    step("묶음 점검 (selftest)")
    selftest()
    size = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file())
    print(f"dist/WATT: {size / 1024 ** 2:.0f} MB")
    if not args.no_installer:
        step("설치 프로그램 (Inno Setup)")
        out = installer()
        print(f"{out} {out.stat().st_size / 1024 ** 2:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
