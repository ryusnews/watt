"""게임 위 창(tkinter) 공통 — 색, 글꼴(IBM Plex 를 설치 없이 이 프로세스에서만), 로고 그리기.

tkinter 는 둥근 모서리·그림자·그라데이션·부분 투명이 안 된다: 단색 면, 1px 테두리, 창 전체 투명도만 쓴다.
"""
import ctypes
import tkinter as tk
import tkinter.font as tkfont

from . import paths

BG, BAR, PANEL, CTRL, LINE = "#0F131A", "#161C26", "#131922", "#1E2632", "#252E3B"
FG, FG2, MUTED, FAINT, DIM = "#ECEFF4", "#B7BFCB", "#8C96A5", "#6A7483", "#4A5362"
TEAL, BRONZE, OK = "#3BB3D3", "#C9A571", "#7ED6AE"
LANG = {"en": "#86A8F0", "zh": "#E97A72", "ru": "#B89AF0", "ja": "#72D2AE", "ko": "#9AA4B2"}

_loaded = False
SANS, MONO = "Malgun Gothic", "Consolas"  # 글꼴을 못 불러오면 이것


def load_fonts(root: tk.Misc) -> None:
    """IBM Plex(OFL, 앱에 동봉)를 이 프로세스에서만 쓰게 등록 — 시스템에 설치하지 않는다(FR_PRIVATE)."""
    global _loaded, SANS, MONO
    if _loaded:
        return
    _loaded = True
    folder = paths.UI / "fonts"
    for f in folder.glob("*.ttf"):
        ctypes.windll.gdi32.AddFontResourceExW(str(f), 0x10, 0)
    fams = set(tkfont.families(root))
    if "IBM Plex Sans KR" in fams:
        SANS = "IBM Plex Sans KR"
    for name in ("IBM Plex Mono Medium", "IBM Plex Mono"):
        if name in fams:
            MONO = name
            break


def _round_rect(c: tk.Canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
           x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def logo(parent: tk.Misc, size: int = 13, bg: str = BAR) -> tk.Canvas:
    """말풍선(청동) + 번개 — 런처 로고와 같은 모양을 작게."""
    c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
    s = size / 40
    _round_rect(c, 3 * s, 4 * s, 37 * s, 31 * s, 6 * s, fill=BRONZE, outline="")
    c.create_polygon(11.5 * s, 30 * s, 19 * s, 30 * s, 11.5 * s, 37.5 * s, fill=BRONZE, outline="")
    c.create_polygon(22.5 * s, 8.5 * s, 13 * s, 20.5 * s, 19.5 * s, 20.5 * s, 17 * s, 28.5 * s, 27 * s, 16 * s,
                     20.5 * s, 16 * s, fill=bg, outline="")
    return c
