"""남은 인식 오류가 어디서 나오나 — OCR 이 못 읽었나(학습으로 고칠 몫), 어느 엔진은 맞게 읽었는데 고르기 · 나누기가 틀렸나.

python -m tools.error_analysis [--ai ko,zh,ru] [--models 폴더] [--cpu]

이름 · 본문 오류마다 엔진별 읽기(cand)에서 정답에 가장 가까운 것을 찾는다:
  ocr       어느 엔진도 정답 근처로 못 읽음 → 모델 학습 · 전처리로만
  select    어떤 엔진은 맞게(≥0.9) 읽었는데 다른 것을 골랐음 → 고르기 규칙
  split     못 찾음 · 언어 오류 등 메시지 나누기 · 판정 문제
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import eval_ocr as E  # noqa: E402
from translator import live  # noqa: E402
from watt import ocr  # noqa: E402


def name_in(text: str) -> list[str]:
    return [m for m in re.findall(r"\[([^\[\]]{1,40})\]", text or "")]


def best_name(rows: list[dict], gt: str) -> tuple[float, str]:
    best = (0.0, "")
    for r in rows[:1]:  # 이름은 첫 줄
        for eng, t in (r.get("cand") or {}).items():
            for n in name_in(t):
                s = E.sim(n, gt)
                if s > best[0]:
                    best = (s, f"{eng}:{n}")
    return best


def best_body(rows: list[dict], gt: str) -> tuple[float, str]:
    best = (0.0, "")
    engines = {e for r in rows for e in (r.get("cand") or {})}
    for eng in engines:
        parts = []
        for i, r in enumerate(rows):
            t = (r.get("cand") or {}).get(eng) or ""
            parts.append(live.body_of(t) if i == 0 else t)
        s = E.sim(" ".join(parts), gt)
        if s > best[0]:
            best = (s, eng)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", default=str(ROOT / "eval" / "private" / "gt.json"))
    ap.add_argument("--ai", default="")
    ap.add_argument("--models", default="")
    ap.add_argument("--cpu", action="store_true")
    ap.add_argument("--show", action="store_true")
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
            raise SystemExit(eng.ai_error)
    kinds: Counter = Counter()
    rows_out = []
    n = 0
    for rel, gt in frames.items():
        msgs, _ = E.read_frame(eng, E.load_png(gt_path.parent / rel))
        for g, m in E.match(gt, msgs):
            n += 1
            want = g[2] if g[2] in ("ko", "zh", "ru") else "en"
            if not m:
                kinds["못 찾음 → split"] += 1
                rows_out.append(("split", "못 찾음", rel, g, None, ""))
                continue
            if m["lang"] != want:
                kinds["언어 → split"] += 1
                rows_out.append(("split", "언어", rel, g, m, ""))
            if E.sim(m["name"], g[1]) < 0.85:
                s, where = best_name(m["rows"], g[1])
                k = "select" if s >= 0.9 else "ocr"
                kinds[f"이름 → {k}"] += 1
                rows_out.append((k, "이름", rel, g, m, f"가장 가까운 읽기 {s:.2f} {where}"))
            if g[2] != "ko" and E.sim(m["body"], g[3]) < 0.9:
                s, where = best_body(m["rows"], g[3])
                k = "select" if s >= 0.9 else "ocr"
                kinds[f"본문 → {k}"] += 1
                rows_out.append((k, "본문", rel, g, m, f"가장 가까운 엔진 {s:.2f} {where}"))
    print(f"메시지 {n}개 · 오류 {sum(kinds.values())}건")
    for k, v in sorted(kinds.items()):
        print(f"  {k:16s} {v}")
    if a.show:
        for k, what, rel, g, m, note in sorted(rows_out, key=lambda r: r[0]):
            got = f"{m['name']} | {m['body'][:50]}" if m else "-"
            print(f"[{k}] {what} {rel}\n   정답 {g[1]} | {g[3][:50]}\n   읽음 {got}\n   {note}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
