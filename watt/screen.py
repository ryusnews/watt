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


def capture_window(win: dict) -> np.ndarray | None:
    """게임 창 자체의 출력(클라이언트 영역) — 다른 창(WATT · 통역 창 · 브라우저)이 가려도 게임 화면을 받는다.
    PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT): DirectX 창도 DWM 이 그린 내용으로 준다. 실패하거나 까맣면 None."""
    img = _wgc_grab(win, 0, 0, win["w"], win["h"])
    if img is not None:
        return img
    hwnd, w, h = win["hwnd"], win["w"], win["h"]
    dc = user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(dc)
    bmp = gdi32.CreateCompatibleBitmap(dc, w, h)
    try:
        old = gdi32.SelectObject(mdc, bmp)
        ok = user32.PrintWindow(hwnd, mdc, 3)
        gdi32.SelectObject(mdc, old)
        bi = _BMIH(ctypes.sizeof(_BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = (ctypes.c_ubyte * (w * h * 4))()
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, dc)
    img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()
    return img if ok and img[::16, ::16, :3].max() > 8 else None


def covered(win: dict, x: int, y: int, w: int, h: int) -> bool:
    """(x,y,w,h) 안에 게임이 아닌 창이 보이는가 — 몇 점을 찍어 맨 위 창이 게임인지 본다."""
    for fy in (0.1, 0.5, 0.9):
        for fx in (0.05, 0.3, 0.6, 0.95):
            pt = wt.POINT(int(x + w * fx), int(y + h * fy))
            top = user32.WindowFromPoint(pt)
            if top and user32.GetAncestor(top, 2) != win["hwnd"]:  # GA_ROOT
                return True
    return False


_game = {"win": None, "t": 0.0}
_wgc = {"cap": None, "key": None, "off": False}


def _wgc_grab(win: dict, x: int, y: int, w: int, h: int) -> np.ndarray | None:
    """Windows 그래픽 캡처(#45) — 창 클라이언트 (x, y, w, h). 안 되면 None(이후 GDI 로)."""
    import logging
    import time
    if _wgc["off"]:
        return None
    key = (win["hwnd"], win["w"], win["h"])
    try:
        if _wgc["key"] != key:  # 게임을 다시 켰거나 창 크기가 바뀜
            if _wgc["cap"]:
                _wgc["cap"].close()
            from . import wgc
            _wgc["cap"], _wgc["key"] = wgc.WindowCapture(win["hwnd"]), key
        for _ in range(10):  # 시작 직후에는 첫 프레임을 잠깐 기다린다
            img = _wgc["cap"].grab(x, y, w, h)
            if img is not None:
                return img
            time.sleep(0.03)
    except Exception as e:  # 지원하지 않는 Windows · 드라이버 — 지금 방식으로
        logging.getLogger("watt").warning("Windows 그래픽 캡처를 쓸 수 없어 GDI 로: %s", e)
        _wgc["off"] = True
    return None


def capture_game(x: int, y: int, w: int, h: int) -> np.ndarray:
    """채팅 영역 캡처 — Windows 그래픽 캡처(GPU 에서, ~1ms, 가려져도 게임 화면). 안 되면 화면(GDI) · 게임 창 출력."""
    import time
    if time.monotonic() - _game["t"] > 2:
        _game["win"], _game["t"] = find_game_window(), time.monotonic()
    win = _game["win"]
    if win:
        img = _wgc_grab(win, x - win["x"], y - win["y"], w, h)
        if img is not None:
            return img
    if win and covered(win, x, y, w, h):
        full = capture_window(win)
        if full is not None:
            ox, oy = x - win["x"], y - win["y"]
            if 0 <= ox and 0 <= oy and ox + w <= win["w"] and oy + h <= win["h"]:
                return full[oy:oy + h, ox:ox + w].copy()
    return capture(x, y, w, h)


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


def _cubic_w(d: float) -> float:
    d = abs(d)
    return 1.5 * d ** 3 - 2.5 * d ** 2 + 1 if d <= 1 else -0.5 * d ** 3 + 2.5 * d ** 2 - 4 * d + 2 if d < 2 else 0.0


# 3배 — 출력 3n+r 의 원래 위치는 n + (r - 1)/3. 위상마다 이웃 4점의 고정 가중치(쌍삼차, a = -0.5)
_P3 = []
for _r in range(3):
    _s = (_r - 1) / 3
    _f = int(np.floor(_s))
    _P3.append((_f, [_cubic_w(_s - (_f + j - 1)) for j in range(4)]))


def _up_axis3(arr: np.ndarray, ax: int) -> np.ndarray:
    n = arr.shape[ax]
    p = np.pad(arr, [(2, 2) if i == ax else (0, 0) for i in range(arr.ndim)], mode="edge")
    outs = []
    for f, w in _P3:
        acc = 0
        for j in range(4):
            acc = acc + w[j] * np.take(p, np.arange(n) + 2 + f + j - 1, axis=ax)
        outs.append(acc)
    shape = list(arr.shape)
    shape[ax] *= 3
    return np.stack(outs, axis=ax + 1).reshape(shape)


def upscale3(bgra: np.ndarray) -> np.ndarray:
    """3배 확대(쌍삼차) — 작은 글꼴의 영어를 다시 읽을 때. 일반 upscale(…, 3) 은 화면 한 장에 ~320ms."""
    f = bgra[..., :3].astype(np.float32)
    out = np.clip(_up_axis3(_up_axis3(f, 0), 1), 0, 255).astype(np.uint8)
    return np.concatenate([out, np.full(out.shape[:2] + (1,), 255, np.uint8)], axis=2)


def upscale(bgra: np.ndarray, k: float) -> np.ndarray:
    """k 배 확대(쌍삼차) — 한 메시지를 배율을 바꿔 다시 읽을 때(live.refine)."""
    def axis(arr, ax):
        n = arr.shape[ax]
        m = int(round(n * k))
        src = (np.arange(m) + 0.5) / k - 0.5
        i0 = np.floor(src).astype(int)
        t = src - i0
        d = np.abs(np.stack([1 + t, t, 1 - t, 2 - t], -1))
        w = np.where(d <= 1, 1.5 * d ** 3 - 2.5 * d ** 2 + 1, -0.5 * d ** 3 + 2.5 * d ** 2 - 4 * d + 2)
        p = np.pad(arr, [(2, 2) if i == ax else (0, 0) for i in range(arr.ndim)], mode="edge")
        out = 0
        for j in range(4):
            shape = [1] * arr.ndim
            shape[ax] = m
            out = out + np.take(p, i0 + 1 + j, axis=ax) * w[:, j].reshape(shape)
        return out
    f = bgra[..., :3].astype(np.float32)
    out = np.clip(axis(axis(f, 0), 1), 0, 255).astype(np.uint8)
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

