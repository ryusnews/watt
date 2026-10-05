"""번역 표본(#7) 받아 검증 — 동의한 설치가 보낸 표본을 이 PC 의 로컬 AI 로 다시 판정하고 보고서를 쓴다.

python -m tools.fetch_samples                    # 새 표본 받기 → 판정(gemma4:12b) → 서버에 결과 → 보고서
python -m tools.fetch_samples --no-judge         # 받기 · 보고서만(판정 없이)
python -m tools.fetch_samples --model gemma4:e4b --local   # 다른 모델 · wrangler dev(127.0.0.1:8787)
보고서 · 받은 표본: eval/private/samples/ (다른 플레이어 채팅 — 저장소에 올리지 않는다)
관리자 키는 ~/.watt-admin-token(저장소 밖). 게임 중에는 다른 모델을 올리지 않게 --model 로 지금 쓰는 모델을.
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "eval" / "private" / "samples"

JUDGE = ("You check machine translations of World of Warcraft Classic chat into Korean for a Korean player. "
         "Given the original message (may contain OCR noise) and its Korean translation, judge: "
         "'ok' = meaning kept (small wording differences are fine, game slang expanded correctly); "
         "'bad' = wrong meaning, wrong dungeon/zone/class/role, invented content, recruiting vs joining flipped, numbers wrong, "
         "or foreign words left untranslated that should be Korean; "
         "'doubt' = the original is too garbled to tell, or you are not sure. "
         "Answer in 'why' with one short Korean sentence, and in 'better' a corrected Korean translation when 'bad' (else empty). "
         "Game terms in this message (original = Korean name): {terms}. "
         "Chat conventions (these readings are correct, do not mark them wrong): {rules}")
SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["ok", "bad", "doubt"]},
                                           "why": {"type": "string"}, "better": {"type": "string"}},
          "required": ["verdict", "why", "better"]}


def call(base: str, token: str, path: str, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(base + "/v1/admin/" + path, data=json.dumps(body).encode() if body else None, method=method,
                                 headers={"Authorization": f"Bearer {token}", "User-Agent": "WATT/admin",
                                          **({"Content-Type": "application/json"} if body else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def fetch(base: str, token: str, limit: int) -> list[dict]:
    rows, after = [], 0
    while len(rows) < limit:
        page = call(base, token, f"samples?status=new&after={after}&limit={min(1000, limit - len(rows))}")
        if not page:
            break
        rows += page
        after = page[-1]["id"]
    return rows


def judge(rows: list[dict], model: str) -> None:
    from translator import incoming, llm, terms
    for i, r in enumerate(rows, 1):
        zh = r["lang"] == "zh"
        hints = terms.incoming(r["body"], zh)
        # 번역기가 따르는 은어 규칙을 판정에도 — 없으면 12b 가 NY 를 '뉴욕', FM 을 '파밍', 4=1 을 '4명 중 1명'으로 보고
        # 맞는 번역을 틀림으로 판정했다(2026-10-05 첫 시험)
        rules = " ".join(x for x, on in ((incoming.LATIN_RULES, re.search(r"[A-Za-z]{2,}", r["body"])),
                                         (incoming.CHINESE_RULES, zh or re.search(r"[一-鿿]", r["body"])),
                                         (incoming.CYRILLIC_RULES, re.search(r"[Ѐ-ӿ]", r["body"]))) if on)
        sysmsg = JUDGE.format(terms="; ".join(f"{k} = {v}" for k, v in hints.items()) or "none", rules=rules or "-")
        user = json.dumps({"lang": r["lang"], "original": r["body"], "korean": r["ko"]}, ensure_ascii=False)
        try:
            out, _ = llm.chat_json(model, sysmsg, user, SCHEMA)
            r["status"], r["verdict"], r["better"] = out.get("verdict", "doubt"), out.get("why", ""), out.get("better", "")
        except Exception as e:  # 한 건 실패로 멈추지 않는다 — 다음 번에 다시
            r["status"], r["verdict"] = "new", f"판정 실패: {type(e).__name__}"
        print(f"\r판정 {i}/{len(rows)}", end="", flush=True)
    print()


def report(rows: list[dict], model: str | None) -> str:
    st = Counter(r.get("status", "new") for r in rows)
    lines = [f"# 번역 표본 검증 — {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"- 표본 {len(rows)}개 · 판정 모델 {model or '없음'} · 결과 {dict(st)}"]
    by = Counter((r["lang"], r["model"]) for r in rows)
    bad = Counter((r["lang"], r["model"]) for r in rows if r.get("status") == "bad")
    lines += ["", "## 언어 · 모델별 틀림", "| 언어 | 모델 | 표본 | 틀림 | 비율 |", "|---|---|---|---|---|"]
    for (lang, m), n in by.most_common():
        lines.append(f"| {lang} | {m} | {n} | {bad[(lang, m)]} | {bad[(lang, m)] / n:.0%} |")
    for name, want in (("틀림", "bad"), ("의심", "doubt")):
        pick = [r for r in rows if r.get("status") == want]
        if pick:
            lines += ["", f"## {name} {len(pick)}건"]
            for r in pick[:60]:
                lines.append(f"- `{r['lang']}` `{r['body'][:80]}` → {r['ko'][:80]}  \n  {r.get('verdict', '')}"
                             + (f" → **{r['better'][:80]}**" if r.get("better") else ""))
    # 엔진끼리 다르게 읽은 줄 — 정답 표본 후보(#7 4번)
    diff = [r for r in rows if len({re.sub(r"\W", "", v) for v in (r.get("reads") or {}).values() if v}) > 1]
    lines += ["", f"## 엔진끼리 다르게 읽은 줄 {len(diff)}건(정답 표본 후보)"]
    lines += ["- " + " · ".join(f"{k}: `{v[:50]}`" for k, v in r["reads"].items()) for r in diff[:30]]
    # 사전에 없는 낱말 — 여러 표본에 되풀이된 것(#11 사전 후보)
    from translator import terms
    known = {s.lower() for s, _ in terms._IN_PAIRS}
    words = Counter(w for r in rows for w in set(re.findall(r"[A-Za-z]{2,}|[一-鿿]{2,}|[Ѐ-ӿ]{3,}", r["body"]))
                    if w.lower() not in known)
    lines += ["", "## 사전에 없는 말(3번 이상)", ", ".join(f"`{w}`×{n}" for w, n in words.most_common(60) if n >= 3) or "-"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma4:12b")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--local", action="store_true", help="wrangler dev(127.0.0.1:8787)")
    ap.add_argument("--limit", type=int, default=2000)
    a = ap.parse_args()
    base = "http://127.0.0.1:8787" if a.local else "https://watt-api.watt-api.workers.dev"
    token = "local-test-admin" if a.local else (Path.home() / ".watt-admin-token").read_text(encoding="utf-8").strip()
    rows = fetch(base, token, a.limit)
    if not rows:
        print("새 표본 없음")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if not a.no_judge:
        judge(rows, a.model)
        done = [{"id": r["id"], "status": r["status"], "verdict": r["verdict"][:300]} for r in rows if r["status"] != "new"]
        for i in range(0, len(done), 500):
            call(base, token, "samples", "POST", {"items": done[i:i + 500]})
    (OUT / f"samples_{stamp}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    md = report(rows, None if a.no_judge else a.model)
    (OUT / f"report_{stamp}.md").write_text(md, encoding="utf-8")
    print(md.split("\n## ")[0] + f"\n저장: {OUT / f'report_{stamp}.md'}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
