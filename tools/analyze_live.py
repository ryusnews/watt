"""실시간 통역 추적 기록 분석 — 고도화 포인트 찾기.

python tools/analyze_live.py                 # 오늘 기록
python tools/analyze_live.py --date 20261001 # 특정 날
python tools/analyze_live.py --all           # logs/trace 전부
보고서: logs/analysis/analysis_<시각>.md (화면에도 요약)
"""
import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE = ROOT / "logs" / "trace"
OUT = ROOT / "logs" / "analysis"
HANGUL = re.compile(r"[가-힣]")
LATIN_WORD = re.compile(r"[A-Za-z]{4,}")
# 번역에 남아도 괜찮은 영어(게임 약어·고유명사로 흔히 그대로 쓰는 것)
OK_LATIN = {"lfg", "lfm", "dps", "tank", "heal", "healer", "pvp", "pve", "raid", "boss", "buff", "loot", "gold", "silver",
            "copper", "summon", "whisper", "rogue", "mage", "druid", "hunter", "priest", "shaman", "warrior", "paladin",
            "warlock", "wts", "wtb", "afk", "brb"}
CHAT_LIKE = re.compile(r"^\s*[\[〔(\|lI1]\s*\d+|\]\s*[:：]|^\S{1,24}\s*[:：]")


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(p / 100 * (len(s) - 1) + 0.5))]


def fmt_s(v):
    return "-" if v is None else f"{v:.2f}s"


def load(paths):
    evs = []
    for p in paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                evs.append(json.loads(line))
            except ValueError:
                pass
    return evs


def ocr_suspects(text: str) -> list[str]:
    """OCR 이 흔히 틀리는 모양: 숫자·글자 섞인 낱말, 낱말 첫 I 뒤 소문자(Ifg), a 로 끝나는 lfg 류."""
    out = []
    for w in re.findall(r"[A-Za-z0-9]{3,}", text):
        if re.search(r"\d", w) and re.search(r"[A-Za-z]", w) and not re.fullmatch(r"\d+[a-zA-Z]{1,2}", w):
            out.append(w)
        elif re.fullmatch(r"I[a-z]{2,}", w) and w.lower() not in {"ice", "iron", "ill", "inv", "ive", "its", "isle"}:
            out.append(w)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=time.strftime("%Y%m%d"))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--since", help="이 시각 이후만(예: 2026-09-30T23:45)")
    args = ap.parse_args()
    paths = sorted(TRACE.glob("trace_*.jsonl")) if args.all else [TRACE / f"trace_{args.date}.jsonl"]
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("추적 기록 없음:", TRACE)
        return 1
    evs = load(paths)
    if args.since:
        evs = [e for e in evs if e["t"] >= args.since]
    if not evs:
        print("해당 기간 기록 없음")
        return 1
    by = defaultdict(list)
    for e in evs:
        by[e["ev"]].append(e)
    frames, msgs, trs, dups, orphans = by["frame"], by["msg"], by["tr"], by["dup"], by["orphan"]
    stats, errors, sessions = by["stats"], by["error"], by["session"]
    L = []  # 보고서 줄
    w = L.append
    t_first, t_last = evs[0]["t"], evs[-1]["t"]
    w(f"# 실시간 통역 분석 — {t_first[:16]} ~ {t_last[11:16]}  (세션 {len(sessions)}개)\n")

    # 1. 읽기
    w("## 1. 읽기(캡처·OCR)")
    ocr = [f["ocr_ms"] for f in frames if f.get("ocr_ms")]
    loop = [f["loop_ms"] for f in frames if f.get("loop_ms")]
    if stats:
        fr, ch = stats[-1]["frames"], stats[-1]["changed"]
        w(f"- 마지막 통계: 읽기 {fr}회 중 화면 바뀜 {ch}회 ({100 * ch / max(1, fr):.0f}%)")
    w(f"- 바뀐 화면 {len(frames)}개 · OCR 중앙 {pct(ocr, 50)}ms / p90 {pct(ocr, 90)}ms / 최대 {max(ocr) if ocr else '-'}ms")
    w(f"- 한 바퀴(OCR+처리) 중앙 {pct(loop, 50)}ms / p90 {pct(loop, 90)}ms  (0.5초 주기 기준 {sum(1 for x in loop if x > 500)}회 초과)")
    w(f"- 오류 {len(errors)}건" + ("" if not errors else ": " + "; ".join(f"{e['where']}:{e['msg'][:60]}" for e in errors[:5])))
    w("")

    # 2. 메시지
    w("## 2. 메시지")
    dec = Counter(m["decision"] for m in msgs)
    lang = Counter(m["lang"] for m in msgs)
    mins = max(1, len({m["t"][:16] for m in msgs}))
    w(f"- 새 메시지 {len(msgs)}개 (메시지가 있던 분당 {len(msgs) / mins:.1f}개) · 처리: {dict(dec)}")
    w(f"- 언어: {dict(lang)}")
    spam = Counter(m["body"] for m in msgs if m["decision"] == "queued")
    rep = [(b, c) for b, c in spam.most_common(args.top) if c > 1]
    if rep:
        w(f"- 같은 본문 반복(새 메시지로 다시 들어온 것) 상위: " + " · ".join(f"{c}회 `{b[:40]}`" for b, c in rep[:5]))
    w("")

    # 3. 번역
    w("## 3. 번역")
    llm = [t["llm_s"] for t in trs if not t["cached"] and not t.get("error")]
    qwait = [t["queue_s"] for t in trs]
    total = [t["total_s"] for t in trs]
    cached = sum(1 for t in trs if t["cached"])
    fails = [t for t in trs if t.get("error")]
    w(f"- 번역 {len(trs)}건 · 캐시 적중 {cached}건 ({100 * cached / max(1, len(trs)):.0f}%) · 실패 {len(fails)}건")
    w(f"- LLM 시간 중앙 {fmt_s(pct(llm, 50))} / p90 {fmt_s(pct(llm, 90))} · 대기열 대기 중앙 {fmt_s(pct(qwait, 50))} / p90 {fmt_s(pct(qwait, 90))}")
    w(f"- 화면에 보이고 번역이 뜨기까지(대기+번역) 중앙 {fmt_s(pct(total, 50))} / p90 {fmt_s(pct(total, 90))} / 최대 {fmt_s(max(total) if total else None)}")
    back = [t["backlog"] for t in trs]
    if back:
        w(f"- 밀린 번역 최대 {max(back)}건")
    w("")

    # 4. 언어 판정
    w("## 4. 언어 판정(줄 고르기)")
    border = []
    for m in msgs:
        for r in m["rows"]:
            f, c = r["feat"], r["cand"]
            if r["lang"] == "en" and f.get("ru"):
                border.append(("en인데 러시아어 조건도 맞음(영어 엔진이 깨끗해서 영어로)", m, r))
            elif r["lang"] == "en" and f.get("en_garbled"):
                border.append(("en인데 영어 엔진 결과가 깨짐(키릴·한자 못 읽은 줄?)", m, r))
            elif r["lang"] == "zh" and c.get("en") and not re.search(r"[^\x00-\x7f]", c.get("zh", "") or ""):
                border.append(("zh로 골랐는데 한자가 없음", m, r))
            elif r["lang"] == "en" and 0 < f.get("hangul", 0) < 2:
                border.append(("en인데 한글 1자", m, r))
    kinds = Counter(k for k, _, _ in border)
    w(f"- 애매한 판정 {len(border)}건: {dict(kinds)}")
    for k, m, r in border[: args.top]:
        w(f"  - {k} · 선택 `{m['body'][:50]}` · en `{(r['cand'].get('en') or '')[:40]}` · ru `{(r['cand'].get('ru') or '')[:40]}`")
    w("")

    # 5. 머리 인식 실패
    w("## 5. 채팅 줄 머리([채널] [이름]:) 못 알아본 줄")
    chatlike = [o for o in orphans if CHAT_LIKE.search(o["text"])]
    w(f"- 버린 줄 {len(orphans)}종 중 채팅 줄처럼 보이는 것 {len(chatlike)}종")
    for o in chatlike[: args.top]:
        w(f"  - ({o['lang']}) `{o['text'][:80]}`")
    w("")

    # 6. 중복 판정
    w("## 6. 중복 판정")
    w(f"- OCR 이 흔들려 비슷하게 읽힌 같은 메시지로 거른 것 {len(dups)}건 (유사도 중앙 {pct([d['sim'] for d in dups], 50)})")
    low = sorted(dups, key=lambda d: d["sim"])[:5]
    for d in low:
        w(f"  - 유사도 {d['sim']} · `{d['body'][:40]}` ↔ `{d['near'][:40]}`")
    # 놓친 중복: 같은 이름, 1분 안, 본문이 비슷한데 둘 다 새 메시지로 번역
    q = [m for m in msgs if m["decision"] == "queued"]
    missed = []
    for i, a in enumerate(q):
        for b in q[i + 1:i + 15]:
            if a["name"] == b["name"] and a["body"] != b["body"]:
                s = SequenceMatcher(None, a["body"], b["body"]).ratio()
                if s >= 0.75:
                    missed.append((s, a, b))
    w(f"- 놓친 중복 의심(같은 이름·비슷한 본문이 두 번 번역) {len(missed)}건")
    for s, a, b in missed[:5]:
        w(f"  - {s:.2f} · `{a['body'][:40]}` / `{b['body'][:40]}`")
    w("")

    # 7. 번역 품질 신호
    w("## 7. 번역 품질 신호(자동 추정 — 사람 확인 필요)")
    no_ko = [t for t in trs if not HANGUL.search(t["ko"] or "") and not t.get("error")]
    same = [t for t in trs if norm(t["ko"]) == norm(t["body"])]
    left = []
    for t in trs:
        words = [x for x in LATIN_WORD.findall(t["ko"] or "") if x.lower() not in OK_LATIN]
        if words and t["lang"] == "en":
            left.append((t, words))
    w(f"- 한글이 없는 번역 {len(no_ko)}건 · 원문 그대로 {len(same)}건 · 영어 낱말이 번역에 남음 {len(left)}건")
    for t in no_ko[:5]:
        w(f"  - 한글 없음 `{t['body'][:50]}` → `{t['ko'][:50]}`")
    for t, ws in left[:8]:
        w(f"  - 남은 영어 {ws[:4]} · `{t['body'][:50]}` → `{t['ko'][:50]}`")
    sus = Counter()
    for m in q:
        for s in ocr_suspects(m["body"]):
            sus[s] += 1
    w(f"- OCR 오인식 의심 낱말(원문) 상위: " + (", ".join(f"{k}×{v}" for k, v in sus.most_common(15)) or "없음"))
    w("")

    # 8. 저장된 화면
    pngs = [f["png"] for f in frames if f.get("png")]
    w("## 8. 저장된 화면")
    w(f"- {len(pngs)}장 (채점용). 최근: " + ", ".join(pngs[-3:]))

    report = "\n".join(L)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"analysis_{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(report, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(report)
    print(f"\n저장: {path}")
    return 0


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s or "").lower()


if __name__ == "__main__":
    raise SystemExit(main())
