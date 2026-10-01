"""오래된 릴리스의 첨부 파일 지우기 — 최근 KEEP 개만 파일을 둔다(태그 · 변경 내용은 남긴다).

업데이트는 최신 릴리스만 쓴다(바뀐 파일 받기 · 0.1.2 의 옛 업데이트 모두). 하나 앞 것은 되돌리기용.
python tools/prune_releases.py [--keep 2] [--dry-run]   (gh CLI 로그인 필요)
"""
import argparse
import json
import os
import re
import shutil
import subprocess

REPO = "ryusnews/watt"


def gh() -> str:
    found = shutil.which("gh")
    if found:
        return found
    base = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    for d in os.listdir(base) if os.path.isdir(base) else []:
        p = os.path.join(base, d, "bin", "gh.exe")
        if d.startswith("GitHub.cli") and os.path.exists(p):
            return p
    raise SystemExit("gh CLI 가 없습니다")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    g = gh()
    out = subprocess.run([g, "api", f"repos/{REPO}/releases", "--paginate"], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    rels = json.loads("[" + re.sub(r"\]\s*\[", ",", out.strip())[1:-1] + "]") if out.strip() else []
    rels = [r for r in rels if not r.get("draft") and not r.get("prerelease")]
    rels.sort(key=lambda r: tuple(int(x) for x in re.findall(r"\d+", r["tag_name"])[:3]), reverse=True)
    freed = 0
    for r in rels[a.keep:]:
        for asset in r.get("assets", []):
            freed += asset["size"]
            print(("would delete " if a.dry_run else "delete ") + f"{r['tag_name']} {asset['name']}")
            if not a.dry_run:
                subprocess.run([g, "api", "-X", "DELETE", f"repos/{REPO}/releases/assets/{asset['id']}"], check=True,
                               capture_output=True)
    print(f"kept {min(a.keep, len(rels))} releases with files, freed {freed / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
