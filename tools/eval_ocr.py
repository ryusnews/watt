"""정답 표본으로 인식 점수 내기(#37) — 0.2.0 통과 기준(docs/ROADMAP.md).

python -m tools.eval_ocr                 # eval/private/gt.json
python -m tools.eval_ocr --show          # 틀린 것도 보이기

정답 표본(eval/private/ — 저장소에 올리지 않는다): gt.json {"frames": {"frames/x.png": [[채널, 이름, 언어, 본문], ...]}}
화면은 WATT 가 읽는 그대로(2배 확대 → 4엔진 → 줄 고르기 → 메시지 나누기). 정답 메시지와 읽은 메시지를 하나씩 짝짓는다.
"""
import argparse
import difflib
import json
import re
import struct
import sys
import time
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from translator import live  # noqa: E402
from translator.ocrfix import fix  # noqa: E402
from watt import ocr, screen  # noqa: E402

CRITERIA = {"found": 0.98, "lang": 0.97, "body_foreign": 0.95, "name": 0.90}


def load_png(path: Path) -> np.ndarray:
    """PNG(RGB · RGBA, 필터 0–4) → BGRA."""
    d = path.read_bytes()
    i, idat, w, h, ct = 8, b"", 0, 0, 2
    while i < len(d):
        n, tag = struct.unpack(">I4s", d[i:i + 8])
        data = d[i + 8:i + 8 + n]
        i += 12 + n
        if tag == b"IHDR":
            w, h, _, ct = struct.unpack(">IIBB", data[:10])
        elif tag == b"IDAT":
            idat += data
    bpp = 4 if ct == 6 else 3
    raw = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, 1 + w * bpp)
    out = np.zeros((h, w * bpp), np.uint8)
    for y in range(h):
        ft, line = raw[y, 0], raw[y, 1:].astype(np.int32)
        prev = out[y - 1].astype(np.int32) if y else np.zeros(w * bpp, np.int32)
        if ft == 0:
            cur = line
        elif ft == 2:
            cur = (line + prev) & 255
        else:
            cur = np.zeros(w * bpp, np.int32)
            for x in range(w * bpp):
                a = cur[x - bpp] if x >= bpp else 0
                b, c = prev[x], (prev[x - bpp] if x >= bpp else 0)
                pr = a if ft == 1 else (a + b) // 2 if ft == 3 else (
                    a if abs(b - c) <= abs(a - c) and abs(b - c) <= abs(a + b - 2 * c) else b if abs(a - c) <= abs(a + b - 2 * c) else c)
                cur[x] = (line[x] + pr) & 255
        out[y] = cur
    px = out.reshape(h, w, bpp)
    return np.dstack([px[..., 2], px[..., 1], px[..., 0], np.full((h, w), 255, np.uint8)])


def nz(s: str) -> str:
    return re.sub(r"[\W_]+", "", s or "").lower()


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, nz(a), nz(b)).ratio()


def read_frame(eng: ocr.Ocr, img: np.ndarray) -> tuple[list[dict], float]:
    t0 = time.perf_counter()
    r = eng.read_lines(screen.upscale2(img), 2)
    ms = (time.perf_counter() - t0) * 1000
    lines = {k: [{a: b for a, b in l.items() if a in ("t", "x", "y", "h", "w", "ai")} for l in v] for k, v in r.items()}
    hs = [l["h"] for ls in lines.values() for l in ls]
    lh = int(np.median(hs) * 1.25) if hs else 15
    rows = live.pick_lines(lines, lh)
    return live.build_messages(rows, live.line_pitch(rows, lh)), ms


def match(gt: list, msgs: list[dict]) -> list[tuple[list, dict | None]]:
    """정답 · 읽은 메시지를 하나씩 짝짓기(본문 + 이름이 가장 닮은 것부터)."""
    pairs = sorted(((sim(m["body"], g[3]) + 0.3 * sim(m["name"], g[1]), gi, mi)
                    for gi, g in enumerate(gt) for mi, m in enumerate(msgs)), reverse=True)
    used_g, used_m, got = set(), set(), {}
    for s, gi, mi in pairs:
        if gi in used_g or mi in used_m or sim(msgs[mi]["body"], gt[gi][3]) < 0.5:
            continue
        used_g.add(gi)
        used_m.add(mi)
        got[gi] = msgs[mi]
    return [(g, got.get(i)) for i, g in enumerate(gt)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", default=str(ROOT / "eval" / "private" / "gt.json"))
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--refine", action="store_true", help="메시지마다 배율 · 위치를 바꿔 다시 읽고 투표(live.refine)")
    ap.add_argument("--ai", default="", help="AI 글자 인식으로 보강할 언어(ko,zh,ru) — 모델은 aiocr.MODEL_DIR 또는 --models")
    ap.add_argument("--models", default="", help="모델 폴더(개발용)")
    ap.add_argument("--cpu", action="store_true", help="AI 를 GPU 없이")
    a = ap.parse_args()
    gt_path = Path(a.gt)
    frames = json.loads(gt_path.read_text(encoding="utf-8"))["frames"]
    eng = ocr.Ocr()
    if a.ai:
        from watt import aiocr
        if a.models:
            aiocr.MODEL_DIR = Path(a.models)
        eng.set_ai(a.ai.split(","), gpu=not a.cpu)
        if not eng.ai:
            raise SystemExit("AI 를 못 띄움: " + eng.ai_error)
        print(f"AI 보강: {','.join(eng.ai.rec)} ({'GPU' if eng.ai.gpu else 'CPU'})")
    n = found = lang_ok = name_ok = 0
    body_f, body_all, ms_all, misses = [], [], [], []
    for rel, gt in frames.items():
        img = load_png(gt_path.parent / rel)
        if not ms_all:
            read_frame(eng, img)  # 데우기(GPU 첫 실행)
        msgs, ms = read_frame(eng, img)
        if a.refine:
            t0 = time.perf_counter()
            hs = [r["y"] for m in msgs for r in m["rows"]]
            lh = live.line_pitch([{"y": y} for y in sorted(set(hs))], 25)
            msgs = [live.refine(m, img, lh, eng) if m["body"] else m for m in msgs]
            ms += (time.perf_counter() - t0) * 1000 / max(1, len(msgs))  # 메시지 하나당 더 든 시간
        ms_all.append(ms)
        for g, m in match(gt, msgs):
            n += 1
            if not m:
                misses.append((rel, g, None, "못 찾음"))
                continue
            found += 1
            lang_ok += m["lang"] == (g[2] if g[2] in ("ko", "zh", "ru") else "en")
            name_ok += sim(m["name"], g[1]) >= 0.85
            b = sim(m["body"], g[3])
            body_all.append(b)
            if g[2] != "ko":
                body_f.append(max(b, sim(fix(m["body"]), g[3])))  # 번역 전 글자 바로잡기(ocrfix)까지
            why = [w for w, bad in (("언어", m["lang"] != (g[2] if g[2] in ("ko", "zh", "ru") else "en")),
                                    ("이름", sim(m["name"], g[1]) < 0.85), ("본문", g[2] != "ko" and b < 0.9)) if bad]
            if why:
                misses.append((rel, g, m, " · ".join(why)))
    res = {"found": found / n, "lang": lang_ok / max(1, found), "body_foreign": float(np.mean(body_f)) if body_f else 0,
           "name": name_ok / max(1, found)}
    label = {"found": "외국어 · 한국어 메시지 찾음", "lang": "언어 판정", "body_foreign": "외국어 본문 일치", "name": "이름 정확"}
    print(f"정답 표본: 화면 {len(frames)}장 · 메시지 {n}개 · OCR 중앙 {np.median(ms_all):.0f}ms")
    ok_all = True
    for k, v in res.items():
        ok = v >= CRITERIA[k]
        ok_all &= ok
        print(f"  {'✓' if ok else '✗'} {label[k]:16s} {v * 100:5.1f}%  (기준 {CRITERIA[k] * 100:.0f}%)")
    print(f"  한국어 본문 일치(참고)     {np.mean(body_all) * 100:5.1f}%")
    if a.show:
        for rel, g, m, why in misses:
            got = f"[{m['lang']}] {m['name']} | {m['body'][:60]}" if m else "-"
            print(f"  {why:10s} {rel:22s} 정답 [{g[2]}] {g[1]} | {g[3][:40]}\n{'':35s}읽음 {got}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
