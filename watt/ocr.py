"""Windows OCR(Windows.Media.Ocr) — PowerShell 없이 Python(winrt)에서 직접.

Reader.read(region) 는 예전 ocr_server.ps1 과 같은 모양을 돌려준다:
  {"same": True}                                   글자 모양이 안 바뀜
  {"same": False, "ms": N, "diff": D, "lines": {"en-US": [{"t","x","y","h"}], "ko": [...], "zh-Hans-CN": [...], "ru-RU": [...]}}
"""
import asyncio
import time
from pathlib import Path

import numpy as np


def _prefer_system_msvcp() -> None:
    """winrt 꾸러미에 딸린 msvcp140.dll(14.29)이 먼저 올라가면 AI 실행 엔진(onnxruntime)이 'DLL 초기화 실패'로 안 뜬다.
    Windows 에 더 새 것(Visual C++ 재배포)이 있으면 그것을 먼저 올린다 — 같은 이름 DLL 은 먼저 올라간 것을 같이 쓴다."""
    import ctypes
    import os
    from ctypes import wintypes

    def version(path: str) -> tuple:
        ver = ctypes.windll.version
        n = ver.GetFileVersionInfoSizeW(path, None)
        if not n:
            return ()
        buf = ctypes.create_string_buffer(n)
        if not ver.GetFileVersionInfoW(path, 0, n, buf):
            return ()
        p, ln = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(ln)):
            return ()
        ms, ls = ctypes.cast(p, ctypes.POINTER(wintypes.DWORD * 4)).contents[2:4]
        return ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF
    try:
        import importlib.util
        spec = importlib.util.find_spec("winrt")
        dirs = list(spec.submodule_search_locations or []) if spec else []  # winrt 는 이름 공간 꾸러미(origin 없음)
        mine = next((os.path.join(d, "msvcp140.dll") for d in dirs if os.path.exists(os.path.join(d, "msvcp140.dll"))), "")
        system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "msvcp140.dll")
        if os.path.exists(mine) and os.path.exists(system) and version(system) > version(mine):
            ctypes.WinDLL(system)
    except Exception:
        pass


_prefer_system_msvcp()
from winrt.windows.globalization import Language  # noqa: E402
from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap  # noqa: E402
from winrt.windows.media.ocr import OcrEngine  # noqa: E402
from winrt.windows.storage.streams import Buffer  # noqa: E402

from . import screen  # noqa: E402

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
        self.ai = None  # AI 글자 인식(aiocr.AiOcr) — 켠 언어만 Windows 엔진 자리를 바꿔 낀다
        self.ai_error = ""

    def set_ai(self, langs: list[str], gpu: bool = True) -> None:
        """AI 보강 언어를 바꾼다. 실행 엔진 · 모델이 없거나 못 띄우면 Windows OCR 만(이유는 ai_error)."""
        langs = [lg for lg in langs if lg]
        if self.ai and sorted(self.ai.rec) == sorted(langs) and self.ai.want_gpu == gpu:
            return  # 언어만 보면 GPU 끄기가 적용되지 않았다(2026-10-01)
        self.ai, self.ai_error = None, ""
        if not langs:
            return
        try:
            from .aiocr import AiOcr
            self.ai = AiOcr(langs, gpu)
        except Exception as e:  # 없는 모델 · 실행 엔진 · GPU 문제
            self.ai_error = f"{type(e).__name__}: {e}"

    def read_lines(self, big: np.ndarray, scale: float) -> dict[str, list[dict]]:
        """확대한 화면 → 엔진 자리별 줄(원래 좌표). AI 를 켠 언어는 그 모델이 읽은 줄로."""
        lines = {k: self.lines_of(r, scale) for k, r in self.recognize(big).items()}
        if self.ai:
            try:
                for k, ai_ls in self.ai.lines(big, scale).items():
                    # AI 가 글자 상자를 못 찾은 줄은 Windows 엔진 것을 남긴다 — 통째로 바꾸면 그 줄이 사라졌다
                    # (러시아어 '[Катя Мизулина]: Ищешь…' 를 Windows 는 읽었는데 AI 가 놓쳐 메시지가 깨짐, 2026-10-01)
                    def covered(w):
                        return any(min(w["y"] + w["h"], a["y"] + a["h"]) - max(w["y"], a["y"]) > 0.5 * min(w["h"], a["h"])
                                   for a in ai_ls)
                    lines[k] = sorted(ai_ls + [w for w in lines.get(k, []) if not covered(w)], key=lambda l: (l["y"], l["x"]))
            except Exception as e:  # AI 가 실패해도 Windows OCR 결과로
                self.ai_error = f"{type(e).__name__}: {e}"
        return lines

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
        return {k: [{**{kk: v for kk, v in l.items() if kk in ("t", "x", "y", "h", "w")}, "y": l["y"] + top} for l in ls]
                for k, ls in self.ocr.read_lines(big, self.scale).items()}

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
