"""인식 오류 신고(#14) — 채팅 영역을 잘못 찾았을 때 게임 화면 1장 + 영역 정보를 우리 서버(server/)로.

사용자가 미리 보기를 보고 '보내기'를 눌렀을 때만. 24시간에 1번, 같은 화면은 다시 보내지 않는다(서버도 같은 규칙).
보내는 것: 게임 창 화면(JPEG) · 찾은 영역 · 사용자가 지정한 영역 · 창 크기 · 인식 줄 수 · 앱 버전 · 무작위 설치 ID.
서버는 설치 ID · IP 를 해시로만 두고, 이미지는 30일 뒤 지운다. 토큰 같은 비밀 값은 앱에 없다.
"""
import asyncio
import hashlib
import json
import platform
import time
import urllib.error
import urllib.request
import uuid

import numpy as np

from . import VERSION, settings

API = "https://watt-api.watt-api.workers.dev"
EVERY = 24 * 3600
MAX_BYTES = 1024 * 1024
UA = {"User-Agent": f"WATT/{VERSION}"}  # Cloudflare 는 Python 기본 User-Agent 를 막는다(error 1010)


def install_id() -> str:
    """무작위 설치 ID — 사람 · PC 와 이어지지 않는다. 서버가 같은 설치의 되풀이를 막는 데만 쓴다."""
    cfg = settings.load()
    if not cfg.get("install_id"):
        settings.save({"install_id": str(uuid.uuid4())})
        cfg = settings.load()
    return cfg["install_id"]


def jpeg(bgra: np.ndarray) -> bytes:
    """JPEG — Windows 기본 인코더(추가 구성 요소 없이)."""
    from winrt.windows.graphics.imaging import BitmapEncoder
    from winrt.windows.storage.streams import DataReader, InMemoryRandomAccessStream

    from .ocr import to_bitmap

    async def enc():
        s = InMemoryRandomAccessStream()
        e = await BitmapEncoder.create_async(BitmapEncoder.jpeg_encoder_id, s)
        e.set_software_bitmap(to_bitmap(bgra))
        await e.flush_async()
        n = s.size
        r = DataReader(s.get_input_stream_at(0))
        await r.load_async(n)
        buf = bytearray(n)
        r.read_bytes(buf)
        return bytes(buf)

    return asyncio.run(enc())


def shrink(bgra: np.ndarray, long_side: int) -> np.ndarray:
    """긴 변을 long_side 로(최근접 — 미리 보기 · 크기 맞추기용)."""
    h, w = bgra.shape[:2]
    k = long_side / max(h, w)
    if k >= 1:
        return bgra
    ys = (np.arange(int(h * k)) / k).astype(int)
    xs = (np.arange(int(w * k)) / k).astype(int)
    return bgra[ys][:, xs].copy()


def status() -> dict:
    """{can, wait_h} — 24시간에 1번."""
    left = EVERY - (time.time() - float(settings.load().get("report_last_at") or 0))
    return {"can": left <= 0, "wait_h": max(0, int(left // 3600) + (1 if left % 3600 else 0))}


def send(img: np.ndarray, meta: dict) -> dict:
    """{ok} · {already, wait_h} · {error}."""
    st = status()
    if not st["can"]:
        return {"already": True, "wait_h": st["wait_h"]}
    data, side = jpeg(img), max(img.shape[:2])
    while len(data) > MAX_BYTES and side > 640:  # 1MB 를 넘으면 줄여서
        side = int(side * 0.8)
        data = jpeg(shrink(img, side))
    digest = hashlib.sha256(data).hexdigest()
    if digest == settings.load().get("report_last_hash"):
        return {"already": True, "wait_h": st["wait_h"]}
    meta = {**meta, "install": install_id(), "ver": VERSION, "os": platform.platform(terse=True)[:40]}
    b = uuid.uuid4().hex
    body = (f'--{b}\r\nContent-Disposition: form-data; name="meta"\r\n\r\n{json.dumps(meta)}\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="screen.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
            ).encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(API + "/v1/report", data=body, method="POST",
                                 headers={**UA, "Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = r.status in (200, 201)
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read())
        except ValueError:
            res = {}
        if res.get("already"):
            wait = int(res.get("retry_after", 0)) // 3600
            settings.save({"report_last_at": time.time() - EVERY + int(res.get("retry_after", 0)), "report_last_hash": digest})
            return {"already": True, "wait_h": wait}
        return {"error": "서버가 받지 않았습니다" if e.code < 500 else "서버가 바쁩니다. 나중에 다시 해 주세요"}
    except OSError:
        return {"error": "서버에 연결하지 못했습니다"}
    if ok:
        settings.save({"report_last_at": time.time(), "report_last_hash": digest})
    return {"ok": ok, "kb": len(data) // 1024}
