"""배포용 그림 — 앱 아이콘(watt.ico)과 설치 마법사 그림(BMP). 로고 모양(40 격자)을 numpy 로 직접 그린다.

python tools/make_assets.py   → assets/watt.ico, assets/wizard*.bmp, assets/wizard-small*.bmp
"""
import struct
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"

BUBBLE_RECT = (3, 4, 37, 31, 6)                     # x1, y1, x2, y2, 둥글기
TAIL = [(19, 31), (11.5, 37.5), (11.5, 30)]
BOLT = [(22.5, 8.5), (13, 20.5), (19.5, 20.5), (17, 28.5), (27, 16), (20.5, 16)]
NAVY = np.array([11, 14, 19], float)                # #0B0E13
BRONZE = np.array([201, 165, 113], float)           # #C9A571
G1, G2 = np.array([234, 211, 162], float), np.array([154, 116, 70], float)  # #EAD3A2 → #9A7446


def _in_poly(x, y, poly):
    inside = np.zeros(x.shape, bool)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        cond = (y1 > y) != (y2 > y)
        xin = (x2 - x1) * (y - y1) / ((y2 - y1) if y2 != y1 else 1e-9) + x1
        inside ^= cond & (x < xin)
    return inside


def _in_round_rect(x, y, x1, y1, x2, y2, r):
    cx = np.clip(x, x1 + r, x2 - r)
    cy = np.clip(y, y1 + r, y2 - r)
    return ((x - cx) ** 2 + (y - cy) ** 2 <= r * r) & (x >= x1) & (x <= x2) & (y >= y1) & (y <= y2)


def logo_rgba(size: int, gradient: bool = True, ss: int = 4) -> np.ndarray:
    """size×size RGBA(0~255). 말풍선은 청동(큰 크기는 그라데이션), 번개는 남색."""
    n = size * ss
    idx = (np.arange(n) + 0.5) / n * 40
    x, y = np.meshgrid(idx, idx)
    bubble = _in_round_rect(x, y, *BUBBLE_RECT) | _in_poly(x, y, TAIL)
    bolt = _in_poly(x, y, BOLT) & bubble
    t = np.clip((x + y) / 80, 0, 1)[..., None]
    fill = G1 * (1 - t) + G2 * t if gradient else np.broadcast_to(BRONZE, x.shape + (3,))
    rgb = np.where(bolt[..., None], NAVY, fill)
    a = bubble.astype(float)
    # 슈퍼샘플 평균(알파 곱해서 섞고 다시 나눔)
    rgb = (rgb * a[..., None]).reshape(size, ss, size, ss, 3).mean((1, 3))
    a = a.reshape(size, ss, size, ss).mean((1, 3))
    rgb = np.where(a[..., None] > 0, rgb / np.maximum(a[..., None], 1e-6), 0)
    return np.dstack([np.clip(rgb, 0, 255), a * 255]).round().astype(np.uint8)


def png_rgba(img: np.ndarray) -> bytes:
    h, w = img.shape[:2]
    raw = b"".join(b"\x00" + img[y].tobytes() for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_ico(path: Path, sizes=(16, 20, 24, 32, 40, 48, 64, 128, 256)) -> None:
    """PNG 를 담은 ICO(Windows Vista 이후 표준). 32px 이하는 단색(작게 보면 그라데이션이 뭉개진다)."""
    images = [png_rgba(logo_rgba(s, gradient=s >= 48)) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(head + entries + b"".join(images))


def write_bmp(path: Path, rgb: np.ndarray) -> None:
    """24비트 BMP(아래에서 위로, 줄마다 4바이트 맞춤) — Inno Setup 마법사 그림."""
    h, w = rgb.shape[:2]
    row = w * 3
    pad = (4 - row % 4) % 4
    body = b"".join(rgb[y, :, ::-1].astype(np.uint8).tobytes() + b"\x00" * pad for y in range(h - 1, -1, -1))
    header = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(body), 2835, 2835, 0, 0)
    path.write_bytes(header + info + body)


def compose(w: int, h: int, logo_px: int, logo_cy: float, glow: bool = True) -> np.ndarray:
    """남색 바탕(+청록 빛) 위에 로고."""
    yy, xx = np.mgrid[0:h, 0:w]
    bg = np.broadcast_to(NAVY, (h, w, 3)).copy()
    if glow:
        d = np.sqrt((xx - w / 2) ** 2 + (yy - logo_cy) ** 2) / (0.75 * w)
        teal = np.array([59, 179, 211], float)
        bg += (teal - NAVY) * (0.10 * np.clip(1 - d, 0, 1) ** 2)[..., None]
    lg = logo_rgba(logo_px, gradient=logo_px >= 48).astype(float)
    x0, y0 = int(round(w / 2 - logo_px / 2)), int(round(logo_cy - logo_px / 2))
    a = lg[..., 3:4] / 255
    area = bg[y0:y0 + logo_px, x0:x0 + logo_px]
    bg[y0:y0 + logo_px, x0:x0 + logo_px] = area * (1 - a) + lg[..., :3] * a
    return np.clip(bg, 0, 255)


def main() -> int:
    OUT.mkdir(exist_ok=True)
    write_ico(OUT / "watt.ico")
    (OUT / "watt-256.png").write_bytes(png_rgba(logo_rgba(256)))
    for scale, suffix in ((1, ""), (1.5, "-150"), (2, "-200")):
        w, h = int(164 * scale), int(314 * scale)
        write_bmp(OUT / f"wizard{suffix}.bmp", compose(w, h, int(84 * scale), h * 0.36))
        s = int(55 * scale)
        write_bmp(OUT / f"wizard-small{suffix}.bmp", compose(s, s, int(40 * scale), s / 2, glow=False))
    print("assets:", sorted(p.name for p in OUT.iterdir()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
