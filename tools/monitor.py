"""게임 모니터링 보고서(#37) — 통역을 켜 둔 채로 돌린다.

python -m tools.monitor --minutes 30          # 6초마다 채팅창을 찍고, 끝나면 보고서
python -m tools.monitor --report <폴더>        # 찍어 둔 것으로 보고서만 다시

같은 시간에 WATT 가 번역한 것(logs/live.jsonl)과, 찍은 화면을 지금 코드로 다시 읽은 외국어 메시지를 맞춰 본다.
- 놓친 외국어 메시지: 화면에 있었는데 번역 기록에 없는 것(광고 접기 · 숨기기는 번역 기록에 ko 없이 남는다)
- 옛 메시지 다시 번역: 같은 글을 60초 넘게 지나 다시 번역한 것(같은 사람이 다시 올린 것은 제외할 수 없어 참고용)
화면 · 보고서는 eval/private/monitor/ 에(다른 플레이어 이름 · 대화 — 저장소에 올리지 않는다).
"""
import argparse
import difflib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def capture(out: Path, minutes: float, every: float) -> None:
    from watt import chat_region, screen
    screen.dpi_aware()
    r = chat_region.current()
    if not r:
        raise SystemExit("채팅 영역을 찾지 못했습니다 — 게임과 WATT 환경 설정의 채팅 영역을 확인하세요")
    out.mkdir(parents=True, exist_ok=True)
    shots, end = [], time.time() + minutes * 60
    while time.time() < end:
        p = out / f"{time.strftime('%H%M%S')}.png"
        screen.save_png(screen.capture_game(r["x"], r["y"], r["w"], r["h"]), p)
        shots.append({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "png": p.name})
        print(f"\r{len(shots)}장", end="", flush=True)
        time.sleep(every)
    (out / "meta.json").write_text(json.dumps({"region": r, "shots": shots}, indent=0), encoding="utf-8")
    print()


def dk(s: str) -> str:
    from translator.live import dkey
    return dkey(s)


def report(out: Path) -> str:
    import numpy as np
    from tools.eval_ocr import load_png, read_frame
    from translator.adfilter import AdFilter
    from translator.live import LOOT_HISTORY, SYSTEM_NAMES, is_junk
    from watt import ocr, paths
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    t0, t1 = meta["shots"][0]["t"], meta["shots"][-1]["t"]
    log = [json.loads(l) for l in (paths.LOGS / "live.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    done = [r for r in log if t0 <= r["t"] <= t1]
    earlier = [r for r in log if r["t"] < t0]
    eng, ads, seen, frames_ms = ocr.Ocr(), AdFilter(), {}, []
    # 앱과 같게 읽는다 — 설정에 켠 AI 글자 인식까지(없으면 Windows OCR 만). 예전엔 Windows OCR 만으로 다시 읽어 Prat
    # 화면에서 메시지를 이어 붙이고 '놓침'을 10–18% 로 잘못 셌다(2026-10-01)
    from watt import settings
    langs = settings.load().get("ai_langs") or []
    if langs:
        eng.set_ai(langs, gpu=False)
    print(f"다시 읽기: Windows OCR" + (f" + AI {','.join(sorted(eng.ai.rec))}" if eng.ai else "") +
          (f" (AI 못 띄움: {eng.ai_error})" if langs and not eng.ai else ""))
    for s in meta["shots"]:  # 화면에 나온 외국어 메시지(지금 코드로 다시 읽음)
        msgs, ms = read_frame(eng, load_png(out / s["png"]))
        frames_ms.append(ms)
        ys = sorted({r["y"] for m in msgs for r in m["rows"]})
        pitch = float(np.median(np.diff(ys))) if len(ys) > 2 else 20
        for m in msgs:
            w = len(re.sub(r"\W", "", m["body"]))
            # 앱이 번역하지 않는 것은 빼고 — 한국어(그대로 보임), 이름 없는 줄, 잡음, 화면 맨 위에서 잘린 메시지(머리가 위로 밀려 나감)
            if m["lang"] == "ko" or not m["name"] or w < 2 or is_junk(m["body"], True) or m["rows"][0]["y"] < pitch * 0.8:
                continue
            if m["name"] in SYSTEM_NAMES or LOOT_HISTORY.search(m["body"]):  # 전리품 알림 · 애드온 조각 — 앱도 건너뛴다(#138)
                continue
            # 본문이 한글 [괄호]뿐 — 머리말 [1. 공개 - 오그리마] 를 본문으로 다시 읽은 것('[1.개= 오극리마]', #116)
            if not re.sub(r"\W", "", re.sub(r"\[[^\]]*[가-힣][^\]]*\]?", "", m["body"])):
                continue
            k = dk(m["body"])
            if not any(difflib.SequenceMatcher(None, k, q).ratio() >= 0.85 for q in seen):
                seen[k] = (s["t"], m)
    got = [dk(r["body"]) for r in done]
    before = [dk(r["body"]) for r in earlier]
    # 통역을 켤 때 이미 보이던 글은 일부러 번역하지 않는다(skip_first) — 추적 기록으로 가려낸다
    first = []
    trace = paths.LOGS / "trace" / f"trace_{t0[:10].replace('-', '')}.jsonl"
    if trace.exists():
        for line in trace.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e.get("ev") == "msg" and e.get("decision") == "skip_first" and e["t"][:19] <= t1:
                first.append(dk(e["body"]))
    missed, at_start = [], []
    for k, (t, m) in seen.items():
        # 흐려지며 사라지는 줄은 앞부분만 읽힌다('[Cold' ← [Coldflame Saber], #116) — 번역한 글의 앞부분이면 같은 것
        hit = any(difflib.SequenceMatcher(None, k, q).ratio() >= 0.7 or (len(k) >= 3 and q.startswith(k)) for q in got)
        old = any(difflib.SequenceMatcher(None, k, q).ratio() >= 0.85 for q in before)  # 켜기 전 · 창 밖에서 이미 번역
        if hit or old:
            continue
        if any(difflib.SequenceMatcher(None, k, q).ratio() >= 0.8 for q in first):
            at_start.append((t, m))
        else:
            missed.append((t, m))
    n_screen = len(seen) - len(at_start)
    retr = []
    for i, r in enumerate(done):
        k = dk(r["body"])
        for q in done[:i]:
            gap = (datetime.fromisoformat(r["t"]) - datetime.fromisoformat(q["t"])).total_seconds()
            if gap > 60 and difflib.SequenceMatcher(None, k, dk(q["body"])).ratio() >= 0.9:
                retr.append(r)
                break
    lines = [f"# 모니터링 {t0} – {t1[11:]} ({len(meta['shots'])}장)", "",
             f"| 항목 | 값 |", "|---|---|",
             f"| 화면에 나온 외국어 메시지(다시 읽기, 켤 때 보이던 것 빼고) | {n_screen} |",
             f"| 켤 때 이미 보이던 것(번역 안 함 — 정상) | {len(at_start)} |",
             f"| 같은 시간 번역 | {len(done)} |",
             f"| **놓친 외국어 메시지** | {len(missed)} ({len(missed) / max(1, n_screen) * 100:.1f}%, 기준 ≤ 2%) |",
             f"| 같은 글 다시 번역(60초↑, 참고) | {len(retr)} |",
             f"| 번역 시간 중앙 | {np.median([r['sec'] for r in done if not r.get('cached')] or [0]):.2f}s |",
             f"| OCR(다시 읽기) 중앙 | {np.median(frames_ms):.0f}ms |", ""]
    if missed:
        lines += ["## 놓친 것", ""] + [f"- {t[11:]} [{m['lang']}] {m['name']} | {m['body'][:80]}" for t, m in missed] + [""]
    if retr:
        lines += ["## 다시 번역", ""] + [f"- {r['t'][11:]} {r['name']} | {r['body'][:80]}" for r in retr] + [""]
    lines += ["## 번역", ""] + [f"- {r['t'][11:]} [{r['lang']}] {r['name']} | {r['body'][:60]} → {(r['ko'] or '(광고)')[:60]}"
                                for r in done]
    text = "\n".join(lines) + "\n"
    (out / "report.md").write_text(text, encoding="utf-8")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--every", type=float, default=6)
    ap.add_argument("--report", help="찍어 둔 폴더로 보고서만")
    a = ap.parse_args()
    os.environ.setdefault("WATT_HOME", str(Path(os.environ["LOCALAPPDATA"]) / "WATT"))  # 설치판 데이터(번역 기록)
    out = Path(a.report) if a.report else ROOT / "eval" / "private" / "monitor" / time.strftime("%Y%m%d_%H%M")
    if not a.report:
        capture(out, a.minutes, a.every)
    print(report(out))
    print(f"보고서: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
