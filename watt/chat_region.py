"""채팅 영역 자동 찾기(예전 tools/find_chat.ps1).

1) WoW 창(클라이언트 영역)을 캡처
2) 화면 전체 OCR(중국어 엔진: 한자+영어, 한국어 엔진: 한글)에서 채팅 줄 머리([6] · [6. 파티찾기] · [이름]: …)를 모은다
3) 왼쪽 끝이 비슷한 줄이 가장 많이 모인 무리 = 채팅창
4) 채팅 배경(단색)이 끝나는 테두리까지 넓힌다 — 채팅 내용과 상관없이 같은 사각형이 나오게
결과: 창 기준 좌표 + 비율(해상도가 바뀌어도 다시 쓸 수 있게) + 화면 좌표.
"""
import json
import re
import time
from collections import Counter

import numpy as np

from . import paths, screen
from .ocr import Ocr

# 채팅 줄 머리: [6] [이름] · [6. 파티찾기] [이름] · [1. 공개 - 오그리마] · [이름]: · [이름]님의 외침: · [12:30] (시간 표시)
# 채팅 줄 머리: [6] [이름] · [6. 파티찾기] · [1. 공개 - 오그리마] · [이름]: · [이름]님의 외침: · [12:30] (시간 표시).
# 앞에 OCR 부스러기가 몇 글자 붙어도 받는다. 퀘스트 목록 [18] 퀘스트 이름 은 아님(둘째 괄호 · 점 · 콜론이 없다)
CHAT_RX = re.compile(r"^.{0,4}?[\[〔(]\s*\d{1,2}\s*(?:[.．,]|[\]〕)]\s*[\[〔(])"
                     r"|^.{0,4}?\[[^\]]{1,32}\]\s*(?:님의\s*\S{1,4}\s*)?[:：]"
                     r"|^.{0,4}?\[\s*\d{1,2}\s*[:：]\s*\d{2}")
TOL, RUN = 2, 4


class NotFound(RuntimeError):
    pass


def find(img: np.ndarray | None = None, window: dict | None = None) -> dict:
    if img is None:
        screen.dpi_aware()
        window = screen.find_game_window()
        if not window:
            raise NotFound("WoW 창을 찾지 못했습니다 — 게임을 창 모드(최대화)로 켜 주세요")
        # 게임 창 자체의 출력 — WATT · 다른 창이 채팅창을 가려도 찾는다. 안 되면 화면에서
        img = screen.capture_window(window)
        if screen.black(img):
            img = screen.capture(window["x"], window["y"], window["w"], window["h"])
        if screen.black(img):
            raise NotFound(f"게임 화면이 까맣게 잡힙니다({window['exe']}) — 게임을 '창 모드(최대화)'로 바꾸거나, 다른 게임 창이면 위에서 고르세요")
    window = window or {"x": 0, "y": 0, "w": img.shape[1], "h": img.shape[0], "exe": "image"}
    H, W = img.shape[:2]

    t0 = time.perf_counter()
    ocr = Ocr(["zh-Hans-CN", "ko"])
    lines = []
    for res in ocr.recognize(img).values():
        lines += Ocr.lines_of(res)
    ocr_ms = int((time.perf_counter() - t0) * 1000)

    hits = [l for l in lines if CHAT_RX.search(l["t"])]
    hits.sort(key=lambda l: l["y"])
    dedup = {}
    for l in hits:  # 두 엔진이 같은 줄을 읽은 것 — 세로 위치로 하나만
        dedup.setdefault(round(l["y"] / 8), l)
    hits = list(dedup.values())
    if not hits:
        raise NotFound("채팅 줄([채널] [이름]: …)이 보이지 않습니다 — 채팅창에 대화가 몇 줄 보일 때 다시 시도하세요")
    # 왼쪽 끝이 비슷한(±2줄 높이) 줄이 가장 많은 무리 — 앞 부스러기로 x 가 조금씩 달라도 한 무리로
    near = lambda a, b: abs(a["x"] - b["x"]) <= 2 * max(12, a["b"] - a["y"])
    best = max(hits, key=lambda a: sum(near(a, b) for b in hits))
    chat = [l for l in hits if near(best, l)]
    L, T = min(l["x"] for l in chat), min(l["y"] for l in chat)
    R, B = max(l["r"] for l in chat), max(l["b"] for l in chat)
    line_h = int(round(sum(l["b"] - l["y"] for l in chat) / len(chat)))

    lum = screen.luminance(img)
    vals = [lum[min(H - 1, l["b"] + 2), x] for l in chat for x in range(l["x"], min(W, l["r"] + 1), 7)]
    bg = int(sorted(vals)[len(vals) // 2])
    is_bg = np.abs(lum - bg) <= TOL

    def edge_x(y, x0, dx):
        x, miss = x0, 0
        while 0 <= x + dx < W:
            x += dx
            if is_bg[y, x]:
                miss = 0
            else:
                miss += 1
                if miss >= RUN:
                    return x - dx * RUN
        return x

    def edge_y(x, y0, dy):
        y, miss = y0, 0
        while 0 <= y + dy < H:
            y += dy
            if is_bg[y, x]:
                miss = 0
            else:
                miss += 1
                if miss >= RUN:
                    return y - dy * RUN
        return y

    # 가로: 채팅 줄 사이 빈 행(배경이 가장 많은 행)을 따라 좌우로
    ys = range(T, min(H - 1, B + line_h) + 1)
    fracs = [is_bg[y, L:R + 1:5].mean() for y in ys]
    gap_y = ys[int(np.argmax(fracs))]
    box_l, box_r = edge_x(gap_y, L, -1), edge_x(gap_y, R, 1)
    # 세로: 안쪽 여백 열(글자가 거의 없는 곳)을 따라 위아래로 — 가장 멀리 간 값
    tops, bottoms = [], []
    for cx in (box_r - 3, box_r - 8, box_l + 3):
        if 0 <= cx < W and is_bg[gap_y, cx]:
            tops.append(edge_y(cx, gap_y, -1))
            bottoms.append(edge_y(cx, gap_y, 1))
    if tops:
        T, B = min(tops), max(bottoms)
    L, R = box_l, box_r
    # 배경이 반투명이면 테두리 찾기가 일찍 멈춘다 — 찾은 채팅 줄은 반드시 다 들어가게(첫 줄 윗부분이 잘리던 문제)
    pad = max(3, line_h // 4)
    T = max(0, min(T, min(l["y"] for l in chat) - pad))
    B = min(H - 1, max(B, max(l["b"] for l in chat) + pad))
    L = max(0, min(L, min(l["x"] for l in chat) - pad))
    R = min(W - 1, max(R, max(l["r"] for l in chat) + pad))

    return {
        "source": window.get("exe"),
        "window": {"w": W, "h": H, "screen_x": window["x"], "screen_y": window["y"]},
        "chat": {"x": L, "y": T, "w": R - L, "h": B - T},
        "chat_ratio": {"x": round(L / W, 4), "y": round(T / H, 4), "w": round((R - L) / W, 4), "h": round((B - T) / H, 4)},
        "line_height": line_h, "background_lum": bg, "chat_lines_found": len(chat), "ocr_full_ms": ocr_ms,
        "sample": [l["t"] for l in chat[:3]],
    }


def from_rect(window: dict, rect: dict, img: np.ndarray) -> dict:
    """사용자가 게임 화면에서 직접 지정한 채팅 영역(창 기준 좌표) → 저장 형식. 줄 높이는 그 안을 읽어서."""
    H, W = img.shape[:2]
    x, y = max(0, int(rect["x"])), max(0, int(rect["y"]))
    w, h = min(W - x, int(rect["w"])), min(H - y, int(rect["h"]))
    hs = []
    for res in Ocr(["zh-Hans-CN", "ko"]).recognize(screen.upscale2(img[y:y + h, x:x + w])).values():
        hs += [l["b"] - l["y"] for l in Ocr.lines_of(res, 2)]
    line_h = int(round(float(np.median(hs)))) if hs else 14
    return {"source": window.get("exe"), "manual": True,
            "window": {"w": W, "h": H, "screen_x": window["x"], "screen_y": window["y"]},
            "chat": {"x": x, "y": y, "w": w, "h": h},
            "chat_ratio": {"x": round(x / W, 4), "y": round(y / H, 4), "w": round(w / W, 4), "h": round(h / H, 4)},
            "line_height": line_h, "chat_lines_found": len(hs) // 2}


def save(r: dict) -> None:
    paths.ensure()
    for p in (paths.REGION, paths.REGION_GOOD):
        p.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")


def usable(r: dict) -> bool:
    """채팅 줄이 한두 개뿐인 순간에는 영역이 두 줄 크기로 잡힌다(2026-09-30: 594×39)."""
    return r["chat"]["h"] >= 5 * r["line_height"] and r["chat"]["w"] >= 250


def locate(r: dict, win: dict | None = None) -> dict | None:
    """저장된 채팅 영역을 지금 게임 창 위치에 맞춘 화면 좌표. 게임 창 크기가 바뀌었으면 None(다시 찾아야 함).
    게임 창을 다른 모니터로 옮기거나 해상도를 바꾸면 저장된 화면 좌표가 틀어진다(2026-10-01)."""
    win = win or screen.find_game_window()
    if not win:
        return screen_rect(r)
    if (win["w"], win["h"]) != (r["window"]["w"], r["window"]["h"]):
        return None
    return {"x": win["x"] + r["chat"]["x"], "y": win["y"] + r["chat"]["y"], "w": r["chat"]["w"], "h": r["chat"]["h"],
            "line_h": r["line_height"]}


def current() -> dict | None:
    """지금 쓸 채팅 영역(화면 좌표) — 저장된 것을 창에 맞추고, 창 크기가 바뀌었으면 새로 찾는다. 못 찾으면 None."""
    good = last_good()
    rect = locate(good) if good else None
    if rect:
        return rect
    try:
        return screen_rect(find_and_save())
    except NotFound:
        return None


def screen_rect(r: dict) -> dict:
    return {"x": r["window"]["screen_x"] + r["chat"]["x"], "y": r["window"]["screen_y"] + r["chat"]["y"],
            "w": r["chat"]["w"], "h": r["chat"]["h"], "line_h": r["line_height"]}


def find_and_save() -> dict:
    """찾아서 저장(chat_region.json). 쓸 만하면 chat_region_good.json 에도."""
    r = find()
    paths.ensure()
    paths.REGION.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    if not usable(r):
        raise NotFound(f"찾은 영역이 너무 작습니다({r['chat']['w']}×{r['chat']['h']}) — 채팅 줄이 더 보일 때 다시 시도하세요")
    paths.REGION_GOOD.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    return r


def last_good() -> dict | None:
    try:
        return json.loads(paths.REGION_GOOD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    print(json.dumps(find(), ensure_ascii=False, indent=1))
