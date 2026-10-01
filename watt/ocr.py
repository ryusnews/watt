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
        """엔진의 줄 — 한 줄이 화면 두 줄에 걸쳐 있으면(한국어 엔진이 가끔 묶는다) 낱말 세로 위치로 나눈다."""
        out = []
        for ln in result.lines:
            words = list(ln.words)
            if not words:
                continue
            groups: list[list] = []  # 세로로 겹치는 낱말 = 같은 줄(문장 부호처럼 낮게 붙은 낱말도)
            for w in sorted(words, key=lambda w: w.bounding_rect.y):
                top, bot = w.bounding_rect.y, w.bounding_rect.y + w.bounding_rect.height
                if groups:
                    g_bot = max(q.bounding_rect.y + q.bounding_rect.height for q in groups[-1])
                    if top < g_bot - min(w.bounding_rect.height, 8) * 0.3:
                        groups[-1].append(w)
                        continue
                groups.append([w])
            if len(groups) > 1:
                for g in groups:
                    g.sort(key=lambda w: w.bounding_rect.x)
                    out.append(Ocr._line(" ".join(w.text for w in g), g, scale))
                continue
            out.append(Ocr._line(ln.text, words, scale))
        return out

    @staticmethod
    def _line(text: str, words: list, scale: float) -> dict:
        ys = [w.bounding_rect.y for w in words]
        bs = [w.bounding_rect.y + w.bounding_rect.height for w in words]
        xs = [w.bounding_rect.x for w in words]
        rs = [w.bounding_rect.x + w.bounding_rect.width for w in words]
        return {"t": text, "x": int(min(xs) / scale), "y": int(min(ys) / scale),
                "h": int((max(bs) - min(ys)) / scale), "r": int(max(rs) / scale), "b": int(max(bs) / scale),
                # 낱말별 위치 — 한 줄에 여러 문자가 섞이면 부분마다 맞는 엔진 것을 쓰려고(live.pick_lines)
                "w": [[w.text, int(w.bounding_rect.x / scale), int((w.bounding_rect.x + w.bounding_rect.width) / scale)]
                      for w in words]}


def shift_of(old: np.ndarray, new: np.ndarray, min_iou: float = 0.5) -> int | None:
    """채팅이 위로 얼마나 밀렸나(픽셀) — 글자 픽셀(밝은 점)이 얼마나 겹치나로 맞춘다. 깔끔히 맞지 않으면 None(전체 다시 읽기).
    글자 크기 · 색과 상관없이 같은 화면 안에서만 비교한다. 흐려지는 위쪽 줄은 빼고 본다."""
    if old.shape != new.shape:
        return None
    a, b = screen.luminance(old) > 110, screen.luminance(new) > 110
    h = a.shape[0]
    top = h // 4  # 흐려지는 옛 줄

    def iou(dy):
        x, y = a[dy + top:], b[top:h - dy]
        u = np.logical_or(x, y).sum()
        return np.logical_and(x, y).sum() / u if u else 0.0
    base = iou(0)
    if base >= 0.98:
        return None  # 밀리지 않음(제자리에서 조금 바뀜)
    rows_a, rows_b = a.sum(1), b.sum(1)  # 줄 윤곽으로 후보를 먼저 좁힌다
    cands = sorted(range(1, h // 2), key=lambda dy: np.abs(rows_a[dy:] - rows_b[:h - dy]).mean())[:6]
    best, dy = max((iou(d), d) for d in cands)
    # 오래된 줄이 흐려지며 픽셀이 바뀌어 밀린 뒤에도 ~70% 만 겹친다(밀리지 않았다면 ~13%) — 상대적으로 본다
    return dy if best >= min_iou and best >= 3 * base else None


def gap_above(img: np.ndarray, y: int, line_h: int) -> int:
    """y 에서 위로 올라가며 글자가 없는 가로줄(줄 사이 빈 곳) — 띠를 거기서 잘라야 글자 줄이 반쪽으로 잘리지 않는다."""
    rows = (screen.luminance(img) > 110).sum(1)
    for yy in range(min(y, len(rows) - 1), max(0, y - line_h * 2), -1):
        if rows[yy] == 0:
            return yy
    return -1  # 빈 곳을 못 찾으면 전체 다시 읽기


class Reader:
    """채팅 영역을 읽는다 — 글자 모양이 바뀌었을 때만 OCR."""

    def __init__(self, scale: int = 2):
        screen.dpi_aware()
        self.ocr = Ocr()
        self.scale = scale
        self.last_sig = None
        self.last_img = None
        self.last_lines = None
        self.ready = {"ready": True, "engines": list(self.ocr.engines)}

    def read(self, region: dict, force: bool = False) -> dict:
        t0 = time.perf_counter()
        img = screen.capture_game(region["x"], region["y"], region["w"], region["h"])  # 가려지면 게임 창 출력에서
        sig = screen.text_sig(img)
        diff = screen.sig_diff(sig, self.last_sig)
        min_diff = max(12, int(sig.size * 0.001))
        if 0 <= diff <= min_diff and not force:
            return {"same": True}
        prev_img, prev_lines = self.last_img, self.last_lines
        self.last_sig, self.last_img = sig, img
        dy = shift_of(prev_img, img) if prev_img is not None and prev_lines and not force else None
        line_h = int(region.get("line_h") or 14)
        band = gap_above(img, img.shape[0] - dy - int(line_h * 1.5), line_h) if dy else -1
        if dy and band > line_h * 2:
            # 채팅이 dy 만큼 위로 밀렸을 뿐 — 새로 나타난 아래 띠만 읽고, 위는 지난 결과를 옮겨 쓴다(32줄 ~300ms → 몇 줄)
            lines = self._read(img[band:], band)
            for k, old in prev_lines.items():
                kept = [{**l, "y": l["y"] - dy} for l in old if l["y"] - dy >= 0 and l["y"] - dy + l.get("h", line_h) <= band]
                lines[k] = kept + lines.get(k, [])
            how = "band"
        else:
            lines, how = self._read(img, 0), "full"
        self.last_lines = lines
        return {"same": False, "ms": int((time.perf_counter() - t0) * 1000), "diff": diff, "lines": lines, "how": how,
                "shift": dy}

    def _read(self, img, top: int) -> dict:
        big = screen.upscale2(img) if self.scale == 2 else img
        res = self.ocr.recognize(big)
        return {k: [{**{kk: v for kk, v in l.items() if kk in ("t", "x", "y", "h", "w")}, "y": l["y"] + top}
                    for l in Ocr.lines_of(r, self.scale)] for k, r in res.items()}

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
