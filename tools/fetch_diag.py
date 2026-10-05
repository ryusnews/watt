"""진단 기록(#135) 받기 — 다른 PC(PC방 등)에서 '진단 기록 보내기'로 올린 것을 받아 풀고, 그대로 분석 도구에 넣는다.

python -m tools.fetch_diag                 # 목록
python -m tools.fetch_diag <번호>          # 받아서 eval/private/diag/<번호>/ 에 풀기
그다음 WATT_HOME 을 그 폴더로: 예) WATT_HOME=eval/private/diag/<번호> python -m tools.analyze_live
관리자 키는 ~/.watt-admin-token(저장소 밖)에서 읽는다. 받은 기록은 다른 플레이어 채팅이 있어 eval/private(gitignore)에만.
"""
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://watt-api.watt-api.workers.dev/v1/admin/diags"
TOKEN = (Path.home() / ".watt-admin-token").read_text(encoding="utf-8").strip()


def call(path: str = "", method: str = "GET", body: bytes | None = None) -> bytes:
    req = urllib.request.Request(API + path, data=body, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": "WATT/admin",
                                          **({"Content-Type": "application/json"} if body else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main() -> int:
    if len(sys.argv) < 2:
        for d in json.loads(call()):
            print(f"{d['id']}  {d['day']}  v{d['ver']:<8} {d['size'] / 1048576:6.1f}MB  {d['status']}")
        return 0
    did = sys.argv[1]
    out = ROOT / "eval" / "private" / "diag" / did
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(call(f"/{did}/zip"))) as z:
        for name in z.namelist():  # zip 안 경로가 폴더 밖으로 나가지 않게
            dest = (out / name).resolve()
            if not str(dest).startswith(str(out.resolve())):
                raise SystemExit(f"이상한 경로: {name}")
        z.extractall(out)
    call(f"/{did}", "POST", json.dumps({"status": "seen"}).encode())
    print(f"풀었습니다: {out}\nWATT_HOME={out} 로 분석 도구를 돌리세요")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
