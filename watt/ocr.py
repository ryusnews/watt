"""Windows OCR(Windows.Media.Ocr) — PowerShell 없이 Python(winrt)에서 직접.

Reader.read(region) 는 예전 ocr_server.ps1 과 같은 모양을 돌려준다:
  {"same": True}                                   글자 모양이 안 바뀜
  {"same": False, "ms": N, "diff": D, "lines": {"en-US": [{"t","x","y","h"}], "ko": [...], "zh-Hans-CN": [...], "ru-RU": [...]}}
"""
import asyncio
import time
from pathlib import Path

import numpy as np
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import Buffer

from . import screen

# 채팅에 나오는 언어 → Windows OCR 언어 팩. uk(우크라이나어)는 Windows OCR 에 없어 러시아어 엔진으로 읽는다
ENGINES = ["en-US", "ko", "zh-Hans-CN", "ru-RU"]
PACKS = {  # 언어 팩 이름(설치: DISM /Add-Capability)
    "en-US": "Language.OCR~~~en-US~0.0.1.0",
    "ko": "Language.OCR~~~ko-KR~0.0.1.0",
    "zh-Hans-CN": "Language.OCR~~~zh-CN~0.0.1.0",
    "ru-RU": "Language.OCR~~~ru-RU~0.0.1.0",
}
LABELS = {"en-US": "영어", "ko": "한국어", "zh-Hans-CN": "중국어(간체)", "ru-RU": "러시아어"}


def installed() -> dict[str, bool]:
    """엔진별 설치 여부 — 관리자 권한 없이 확인."""
    tags = {l.language_tag.lower() for l in OcrEngine.available_recognizer_languages}
    out = {}
    for k in ENGINES:
        base = k.lower().split("-")[0]
        out[k] = any(t == k.lower() or t.split("-")[0] == base for t in tags)
    return out


def to_bitmap(bgra: np.ndarray) -> SoftwareBitmap:
    h, w = bgra.shape[:2]
    buf = Buffer(w * h * 4)
    buf.length = w * h * 4
    memoryview(buf)[:] = np.ascontiguousarray(bgra).tobytes()
    sb = SoftwareBitmap(BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    sb.copy_from_buffer(buf)
    return sb


class Ocr:
    def __init__(self, langs: list[str] | None = None):
        self.engines = {}
        for lg in langs or ENGINES:
            e = OcrEngine.try_create_from_language(Language(lg))
            if e:
                self.engines[lg] = e
        self.loop = asyncio.new_event_loop()

    async def _all(self, sb):
        keys = list(self.engines)
        results = await asyncio.gather(*(self.engines[k].recognize_async(sb) for k in keys))
        return dict(zip(keys, results))

    def recognize(self, bgra: np.ndarray) -> dict:
        """엔진마다 OcrResult(네 엔진을 동시에). 좌표는 bgra 기준."""
        return self.loop.run_until_complete(self._all(to_bitmap(bgra)))

    @staticmethod
    def lines_of(result, scale: float = 1.0) -> list[dict]:
        out = []
        for ln in result.lines:
            words = list(ln.words)
            if not words:
                continue
            ys = [w.bounding_rect.y for w in words]
            bs = [w.bounding_rect.y + w.bounding_rect.height for w in words]
            xs = [w.bounding_rect.x for w in words]
            rs = [w.bounding_rect.x + w.bounding_rect.width for w in words]
            out.append({"t": ln.text, "x": int(min(xs) / scale), "y": int(min(ys) / scale),
                        "h": int((max(bs) - min(ys)) / scale), "r": int(max(rs) / scale), "b": int(max(bs) / scale)})
        return out


class Reader:
    """채팅 영역을 읽는다 — 글자 모양이 바뀌었을 때만 OCR."""

    def __init__(self, scale: int = 2):
        screen.dpi_aware()
        self.ocr = Ocr()
        self.scale = scale
        self.last_sig = None
        self.last_img = None
        self.ready = {"ready": True, "engines": list(self.ocr.engines)}

    def read(self, region: dict, force: bool = False) -> dict:
        t0 = time.perf_counter()
        img = screen.capture(region["x"], region["y"], region["w"], region["h"])
        sig = screen.text_sig(img)
        diff = screen.sig_diff(sig, self.last_sig)
        min_diff = max(12, int(sig.size * 0.001))
        if 0 <= diff <= min_diff and not force:
            return {"same": True}
        self.last_sig, self.last_img = sig, img
        big = screen.upscale2(img) if self.scale == 2 else img
        res = self.ocr.recognize(big)
        lines = {k: [{kk: v for kk, v in l.items() if kk in ("t", "x", "y", "h")} for l in Ocr.lines_of(r, self.scale)]
                 for k, r in res.items()}
        return {"same": False, "ms": int((time.perf_counter() - t0) * 1000), "diff": diff, "lines": lines}

    def save_last(self, path: Path) -> bool:
        if self.last_img is None:
            return False
        screen.save_png(self.last_img, path)
        return True

    def close(self) -> None:
        try:
            self.ocr.loop.close()
        except RuntimeError:
            pass
