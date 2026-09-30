"""화면 — 게임 창 찾기, 영역 캡처(GDI), 2배 확대, 글자 모양 서명, PNG 저장. 외부 도구 없이 ctypes + numpy."""
import ctypes
import struct
import zlib
from ctypes import wintypes as wt
from pathlib import Path

import numpy as np

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32
GAME_EXES = {"wow.exe", "wowb.exe", "wowclassic.exe", "wowt.exe"}


def dpi_aware() -> None:
    """물리 픽셀 좌표로 — 캡처·OCR·창 위치가 같은 좌표를 쓰게."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        try:
            user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            pass


# ---- 창
def exe_of_pid(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        return buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


def find_game_window() -> dict | None:
    """보이는 WoW 창 중 가장 큰 것: {hwnd, exe, path, x, y, w, h}(클라이언트 영역, 화면 좌표)."""
    found = []
    enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        path = exe_of_pid(pid.value)
        if path.rsplit("\\", 1)[-1].lower() in GAME_EXES:
            r = wt.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(r))
            pt = wt.POINT(0, 0)
            user32.ClientToScreen(hwnd, ctypes.byref(pt))
            if r.right > 200 and r.bottom > 200:
                found.append({"hwnd": hwnd, "exe": path.rsplit("\\", 1)[-1], "path": path,
                              "x": pt.x, "y": pt.y, "w": r.right, "h": r.bottom})
        return True

    user32.EnumWindows(enum_proc(cb), 0)
    return max(found, key=lambda f: f["w"] * f["h"]) if found else None


# ---- 캡처
class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def capture(x: int, y: int, w: int, h: int) -> np.ndarray:
    """화면 (x,y,w,h) → BGRA (h, w, 4) uint8."""
    sdc = user32.GetDC(0)
    mdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    try:
        old = gdi32.SelectObject(mdc, bmp)
        gdi32.BitBlt(mdc, 0, 0, w, h, sdc, x, y, 0x00CC0020 | 0x40000000)  # SRCCOPY | CAPTUREBLT
        gdi32.SelectObject(mdc, old)
        bi = _BMIH(ctypes.sizeof(_BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = (ctypes.c_ubyte * (w * h * 4))()
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(0, sdc)
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()


def luminance(bgra: np.ndarray) -> np.ndarray:
    b, g, r = (bgra[..., i].astype(np.int32) for i in range(3))
    return (r * 299 + g * 587 + b * 114) // 1000


# ---- 확대(OCR 은 2배에서 작은 글자를 잘 읽는다) — 쌍삼차(Catmull-Rom 계열, a=-0.5)
def _cubic(t: float, a: float = -0.5) -> np.ndarray:
    d = np.abs(np.array([1 + t, t, 1 - t, 2 - t]))
    return np.where(d <= 1, (a + 2) * d ** 3 - (a + 3) * d ** 2 + 1, a * d ** 3 - 5 * a * d ** 2 + 8 * a * d - 4 * a)


_W_EVEN, _W_ODD = _cubic(0.75), _cubic(0.25)


def _up_axis(arr: np.ndarray, ax: int) -> np.ndarray:
    n = arr.shape[ax]
    pad = [(2, 2) if i == ax else (0, 0) for i in range(arr.ndim)]
    p = np.pad(arr, pad, mode="edge")

    def tk(o):
        return np.take(p, np.arange(n) + 2 + o, axis=ax)

    a, b, c, d, e = tk(-2), tk(-1), tk(0), tk(1), tk(2)
    even = _W_EVEN[0] * a + _W_EVEN[1] * b + _W_EVEN[2] * c + _W_EVEN[3] * d
    odd = _W_ODD[0] * b + _W_ODD[1] * c + _W_ODD[2] * d + _W_ODD[3] * e
    shape = list(arr.shape)
    shape[ax] *= 2
    return np.stack([even, odd], axis=ax + 1).reshape(shape)


def upscale2(bgra: np.ndarray) -> np.ndarray:
    f = bgra[..., :3].astype(np.float32)
    out = np.clip(_up_axis(_up_axis(f, 0), 1), 0, 255).astype(np.uint8)
    return np.concatenate([out, np.full(out.shape[:2] + (1,), 255, np.uint8)], axis=2)


# ---- 바뀌었나 — 밝은 점(글자) 이진 서명. 커서 깜빡임·배경 조금 바뀜은 무시(2026-09-30: 픽셀 해시는 매번 '바뀜')
def text_sig(bgra: np.ndarray, sx: int = 3, sy: int = 2, thr: int = 110) -> np.ndarray:
    return luminance(bgra[::sy, ::sx]) > thr


def sig_diff(a: np.ndarray | None, b: np.ndarray | None) -> int:
    if a is None or b is None or a.shape != b.shape:
        return -1
    return int(np.count_nonzero(a != b))


def save_png(bgra: np.ndarray, path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(png_bytes(bgra))


def png_bytes(bgra: np.ndarray) -> bytes:
    """PNG(RGB) — Pillow 없이."""
    h, w = bgra.shape[:2]
    rgb = bgra[..., [2, 1, 0]]
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))

