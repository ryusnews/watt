"""받기 번역 — 외국어 채팅 한 줄(글자)을 한국어로. OCR·이미지 판독 뒤 단계에서 쓴다.

python -m translator.incoming --test "LF tank for SFK"
"""
import argparse
import json
import re
from pathlib import Path

from . import ocrfix, terms
from .llm import chat_json

HERE = Path(__file__).resolve().parent
MODEL = "gemma4:12b"
SCHEMA = {"type": "object", "properties": {"ko": {"type": "string"}}, "required": ["ko"]}


BASE = ("You translate one World of Warcraft Classic chat message into natural Korean for a Korean player. "
        "Expand chat abbreviations (LF/LFM = 구함, LFG = 파티 찾음, WTS = 팝니다, WTB = 삽니다, afk = 자리 비움, inv = 초대, "
        "summ = 소환). Use the official Korean client names for dungeons, zones and classes. "
        "Prices 20s / 5g / 50c = 20실버, 5골드, 50코퍼. "
        "Never add words or requests that are not in the message; if parts are unreadable OCR noise, translate only the "
        "readable parts. Keep player names, numbers and [bracketed links] unchanged; Korean text in [brackets] is an in-game "
        "quest/item link already shown in Korean — copy it exactly. Put only the translation in 'ko'.")
# 글자 종류에 따라 붙이는 규칙 — 전부 넣으면 870토큰이 되어 게임 중 번역이 3~7초로 느려졌다(2026-10-01)
LATIN_RULES = ("'<class or role> LFG <dungeon>' means the writer is that class/role and wants to JOIN a group "
               "(Tank lfg WC = 탱커가 통곡의 동굴 파티를 찾음); 'LF/LFM <role>' = recruiting that role. "
               "Map misspelled game names to the closest real one (STORWIND = 스톰윈드). "
               "OCR misreads g as a and l as I: Ifg/lfa = lfg, Ifm = lfm, roaue = rogue, aoina = going, IVI = lvl, If = lf. "
               "'X would go hard' = X would be awesome.")
CHINESE_RULES = ("Chinese LFG ads shorten dungeons (怒焰 = 성난불길 협곡) and even to pinyin initials "
                 "(AH = 哀嚎 = 통곡의 동굴, never the auction house). 'N=M' counts players (哀嚎4=1 = 4명 있음 1명 더 구함; "
                 "'=3' = 3명 더 구함). 任意 = any class, 缺 = need, 来/组人 = recruiting, 求组 = wants to JOIN a group, "
                 "奶 or N = healer, T = tank, 近战 = 근접 딜러, 远程 = 원거리 딜러, 速刷 = 빠르게 도는, 开搞 = 출발, 带 = 버스. "
                 "Classes by pinyin initials: MS 사제, FS 마법사, SS 흑마법사, LR 사냥꾼, DZ 도적, XD 드루이드, SM 주술사, "
                 "QS 성기사, ZS 전사; a level glued to a class is level + class (20LR = 20레벨 사냥꾼). "
                 "Traditional Chinese is common (補 = fill the missing spot).")
CYRILLIC_RULES = "Ukrainian is read with Russian OCR, so letters may be off (е for є, c06i = собі); still translate it."
_CJK = re.compile(r"[一-鿿]")
_CYR = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-z]{2,}")


def system_prompt(terms: dict, text: str = "", chinese: bool = False) -> str:
    parts = [BASE]
    if not text or _LAT.search(text):
        parts.append(LATIN_RULES)
    if not text or chinese or _CJK.search(text):
        parts.append(CHINESE_RULES)
    if not text or _CYR.search(text):
        parts.append(CYRILLIC_RULES)
    s = " ".join(parts)
    if terms:
        s += " Game terms in this message (original = Korean meaning): " + "; ".join(f"{k} = {v}" for k, v in terms.items()) + "."
    return s


def matched_terms(text: str, chinese: bool = False) -> dict:
    """용어 사전(wow_terms.json): 영어 약어·중국어·러시아어 → 한국 클라이언트 이름."""
    return terms.incoming(text, chinese)


_LF_NEXT = (r"(?:tank|heal|healer|heals|dps|dd|group|grp|more|party|full|\d|wc|vc|dm|sfk|bfd|rfc|rfk|rfd|sm|st|brd|zf|"
            r"ulda|mara|strat|scholo|ubrs|lbrs|rol|stocks|gnomer|mage|priest|rogue|hunter|warrior|druid|shaman|lock|pally)")
OCR_FIXES = [  # 번역 전에 코드로 — 프롬프트로만 알려 주면 'If wc' 를 '만약'으로 읽는다(2026-09-30)
    (re.compile(r"\b(?:IVI|Ivl|IvI|lvI)\b"), "lvl"),
    (re.compile(r"\b(?:Ifg|Ifa|lfa|IFG)\b"), "lfg"),
    (re.compile(r"\b(?:Ifm|IFM)\b"), "lfm"),
    (re.compile(r"\bL[Ff][Il](?=M?\b)"), "LF1"),  # LF1 · LF1M 의 1 을 I 로(LFI DPS SFK — 2026-10-01 모니터링)
    (re.compile(rf"\bIf(?=\s+{_LF_NEXT}\b)", re.I), "lf"),
    (re.compile(r"(?<![A-Za-z0-9])[Il](?=\s?DPS\b)"), "1"),  # 'AH4 = IDPS' — 숫자 1 을 I 로 읽음(2026-09-30 로그)
]


def normalize_ocr(text: str) -> str:
    text = ocrfix.fix(text)  # 0 · O · o, 1 · l · I · | — 사전 · 상용어 · 숫자 자리로(#31)
    for rx, rep in OCR_FIXES:
        text = rx.sub(rep, text)
    return text


def translate(text: str, model: str = MODEL, chinese: bool = False) -> tuple[str, float]:
    """chinese: 중국 사용자가 쓴 글(이름이 한자 등) — 한자 없이 'NY*3DPS' 만 써도 병음 약자(NY = 怒焰 성난불길 협곡)로 읽는다.
    한자가 없으면 영어 약어로 읽어 NY 를 '냥꾼'으로 옮겼다(2026-10-01 모니터링)."""
    text = normalize_ocr(text)
    terms = matched_terms(text, chinese)
    out, secs = chat_json(model, system_prompt(terms, text, chinese), text, SCHEMA)
    return out.get("ko", "").strip(), secs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", required=True)
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()
    ko, secs = translate(args.test, args.model)
    print(f"{ko}  ({secs:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
