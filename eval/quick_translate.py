"""번역 빠른 확인 — 평가 세트(translate_cases.json)를 게임 대기 없이 돌리고, 새 사전 사례도 함께 본다.

python -m eval.quick_translate
"""
import json
import time
from pathlib import Path

from translator import incoming, outgoing

from .compare_translate import check

HERE = Path(__file__).resolve().parent
EXTRA_OUT = [  # 한국어 줄임말(사전) — 번역에 들어가야 할 영어 약어
    {"ko": "통곡 가실분 모십니다", "to": "en", "must": [["WC"], ["LF"]], "must_not": ["LFG"]},
    {"ko": "성불 탱 구해요 귓 주세요", "to": "en", "must": [["RFC"], ["tank"], ["pst", "/w", "whisper", "w me", "pm"]], "must_not": ["LFG"]},
    {"ko": "화심 힐러 구함, 흑마 환영", "to": "en", "must": [["MC"], ["heal"], ["lock", "warlock"]]},
    {"ko": "죽폐 풀런 가실분 딜 2 구함", "to": "en", "must": [["DM", "VC", "Deadmines"], ["full run"], ["dps"]]},
    {"ko": "검둥 막공 탱 구합니다", "to": "en", "must": [["BWL"], ["tank"], ["pug"]]},
    {"ko": "그송 가요 법사 구함", "to": "zh", "must": [["影牙"], ["法师"]], "script": "zh"},
]
EXTRA_IN = [
    {"src": "哀嚎4=1 来ms", "must": [["통곡의 동굴"], ["사제"], ["1"]]},
    {"src": "死矿来奶 4=1", "must": [["죽음의 폐광"], ["힐러"]]},
    {"src": "YY任务队，来个奶 4=1", "must": [["힐러"], ["4명 있음"], ["1명"]], "must_not": ["힐러 4명"]},
    {"src": "来T N =2", "must": [["탱커"], ["힐러"], ["2명 더"]]},
    {"src": "LFM MC need heals and 2 locks", "must": [["화산 심장부"], ["힐러"], ["흑마법사"]]},
    {"src": "WTS TB Summons 20s", "must": [["썬더 블러프"], ["소환"], ["20실버"]]},
    # 중국 병음 약자 — 실제 로그(2026-09-30~10-01)에서 틀렸던 줄
    {"src": "20LR 求组 AH ，要的直接组", "must": [["사냥꾼"], ["통곡의 동굴"], ["20"]], "must_not": ["경매장", "도적"]},
    {"src": "AH3=2 来 T,N ，来了就开搞。", "must": [["통곡의 동굴"], ["탱"], ["힐"], ["2"]], "must_not": ["성난불길"]},
    {"src": "AH4 = IDPS 来个近战", "must": [["통곡의 동굴"], ["근접", "근거리"], ["딜"]], "must_not": ["AH4"]},
    {"src": "AH20 级速刷队来点暴力的猎人报名进组", "must": [["통곡의 동굴"], ["사냥꾼"]], "must_not": ["경매장"]},
    {"src": "血色 4=1 来个SM", "must": [["붉은십자군"], ["주술사"]]},
    # 작은 모델이 중국어 던전을 '성난불길 협곡'으로(PC방 2026-10-05, #137)
    {"src": "有没有刷哀嚎的", "must": [["통곡의 동굴"]], "must_not": ["성난불길"]},
    {"src": "哀嚎来T来治疗", "must": [["통곡의 동굴"], ["탱"], ["힐"]], "must_not": ["성난불길"]},
    {"src": "NY来3个DPS 16+", "must": [["성난불길 협곡"], ["딜"]]},
    # PC방 2026-10-05 저녁 — 이름 OCR 변형 · 독일어 · 프랑스어
    {"src": "Ifm Cholaruk", "must": [["촐라루크"], ["구함", "구해"]], "must_not": ["콜라룩"]},
    {"src": "Suche Verzauberer für 2H Waffe", "must": [["마법부여"]], "must_not": ["마법사"]},
    {"src": "du monde pour ragefeu ?", "must": [["성난불길 협곡"], ["사람", "분"]], "must_not": ["길?"]},
]


def main() -> int:
    cases = json.loads((HERE / "translate_cases.json").read_text(encoding="utf-8"))
    rows = []
    for c in cases["outgoing"] + EXTRA_OUT:
        out, sec = outgoing.translate(c["ko"], c["to"], log=False)
        rows.append(("out" + ("*" if c in EXTRA_OUT else ""), c["ko"], out, sec, check(out, c, c.get("script", c["to"]))))
    for c in cases["incoming"] + EXTRA_IN:
        ko, sec = incoming.translate(c["src"])
        rows.append(("in" + ("*" if c in EXTRA_IN else ""), c["src"], ko, sec, check(ko, c, "ko")))
    for kind in ("out", "in", "out*", "in*"):
        rs = [r for r in rows if r[0] == kind]
        ok = sum(1 for r in rs if not r[4])
        print(f"{kind:5} {ok}/{len(rs)}  (중앙 {sorted(r[3] for r in rs)[len(rs) // 2]:.2f}s)")
    for r in rows:
        if r[4] or r[0].endswith("*"):
            print(f"  {'✗' if r[4] else '✓'} [{r[0]}] {r[1]} → {r[2]}" + (f"  ({', '.join(r[4])})" if r[4] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
