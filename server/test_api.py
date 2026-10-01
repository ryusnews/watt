"""받는 서버 시험 — python server/test_api.py [주소]  (기본: wrangler dev 의 http://127.0.0.1:8787)
관리 기능까지 보려면 WATT_ADMIN 환경 변수에 관리자 키."""
import json
import os
import struct
import sys
import uuid
import zlib
from urllib import error, request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
ADMIN = os.environ.get("WATT_ADMIN", "local-test-admin" if "127.0.0.1" in BASE else "")


def call(method, path, body=None, headers=None):
    # Cloudflare 는 Python 기본 User-Agent 를 막는다(error 1010) — 앱도 WATT/버전 으로 보낸다
    req = request.Request(BASE + path, data=body, method=method, headers={"user-agent": "WATT/test", **(headers or {})})
    try:
        with request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except error.HTTPError as e:
        return e.code, e.read()


def post_json(path, obj):
    return call("POST", path, json.dumps(obj).encode(), {"content-type": "application/json"})


def png(seed: int, w=64, h=32) -> bytes:
    raw = b"".join(b"\x00" + bytes(((x * seed + y) % 256) for x in range(w * 3)) for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def multipart(meta: dict, image: bytes, ctype="image/png"):
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"meta\"\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"s.png\"\r\nContent-Type: {ctype}\r\n\r\n"
            ).encode() + image + f"\r\n--{b}--\r\n".encode()
    return body, {"content-type": f"multipart/form-data; boundary={b}"}


def report(meta, image):
    body, h = multipart(meta, image)
    return call("POST", "/v1/report", body, h)


results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("ok  " if cond else "BAD ") + name + (f"  ({detail})" if detail and not cond else ""))


s, b = call("GET", "/v1/health")
check("health", s == 200, b)

a, c = str(uuid.uuid4()), str(uuid.uuid4())
items = [{"term": "NY", "lang": "zh", "ctx": ["任务队", "来T"], "n": 4}, {"term": "LFIM", "lang": "en", "ctx": ["tank", "BFD"], "n": 3},
         {"term": "QCW1392010", "lang": "zh", "n": 9}, {"term": "x" * 40, "lang": "en"}, {"term": "ok", "lang": "xx"}]
s, b = post_json("/v1/terms", {"install": a, "ver": "0.1.9", "items": items})
check("terms 받기 · 연락처 · 긴 말 · 모르는 언어 거름", s == 200 and json.loads(b)["accepted"] == 2, b)
s, b = post_json("/v1/terms", {"install": a, "items": items})
check("terms 같은 설치 하루 두 번 → 거절", s == 429, b)
s, b = post_json("/v1/terms", {"install": c, "items": items[:1]})
check("terms 다른 설치", s == 200, b)
s, b = post_json("/v1/terms", {"install": "short", "items": items})
check("terms 잘못된 설치 ID", s == 400, b)
s, b = call("POST", "/v1/terms", b"x" * 20000, {"content-type": "application/json"})
check("terms 너무 큼", s == 413, b)

meta = {"install": a, "ver": "0.1.9", "found": {"x": 1, "y": 2, "w": 3, "h": 4}, "picked": {"x": 10, "y": 20, "w": 300, "h": 200},
        "window": {"x": 0, "y": 0, "w": 2560, "h": 1440}, "name": "다른 사람 이름", "lines": 6}
img = png(7)
s, b = report(meta, img)
check("report 받기", s == 201, b)
s, b = report(meta, img)
check("report 같은 화면 → 이미 보냄", s == 409 and json.loads(b).get("already"), b)
s, b = report(meta, png(8))
check("report 같은 설치 24시간 → 이미 보냄 + 남은 시간", s == 429 and json.loads(b).get("retry_after", 0) > 3600, b)
s, b = report(dict(meta, install=c), b"GIF89a" + b"\x00" * 100)
check("report 이미지 아님", s == 415, b)
s, b = report(dict(meta, install=str(uuid.uuid4())), png(9, 900, 900))
check("report 큰 이미지(압축 안 된 PNG ≤1MB면 받음)", s in (201, 413), b)

s, b = call("GET", "/v1/admin/stats")
check("admin 키 없이 → 401", s == 401, b)
if ADMIN:
    auth = {"authorization": "Bearer " + ADMIN}
    s, b = call("GET", "/v1/admin/reports", headers=auth)
    rows = json.loads(b) if s == 200 else []
    check("admin 신고 목록", s == 200 and len(rows) >= 1, b)
    check("admin 이름 같은 모르는 칸은 저장 안 됨", rows and "name" not in rows[-1]["meta"], rows[-1]["meta"] if rows else "")
    if rows:
        s, b = call("GET", f"/v1/admin/reports/{rows[-1]['id']}/image", headers=auth)
        check("admin 이미지 받기", s == 200 and b[:4] == b"\x89PNG", s)
    s, b = call("GET", "/v1/admin/terms?min=1", headers=auth)
    t = {(r["term"], r["lang"]): r for r in json.loads(b)} if s == 200 else {}
    check("admin 사전 후보 — NY 설치 2 · 횟수 8", t.get(("NY", "zh"), {}).get("installs") == 2 and t[("NY", "zh")]["hits"] == 8, t.get(("NY", "zh")))
    s, b = call("GET", "/v1/admin/stats", headers=auth)
    check("admin 통계", s == 200, b)

print(f"{sum(results)}/{len(results)}")
sys.exit(0 if all(results) else 1)
