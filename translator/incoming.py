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
        "readable parts. Keep player names, numbers and [bracketed] text as units. Put only the translation in 'ko'.")
# 글에 [ 가 있을 때만 — 늘 넣으면 링크가 없는 글의 용어(血色)까지 괄호를 씌우고 사전 용어를 놓쳤다(2026-10-01)
LINK_RULES = ("[Bracketed text] is an item, quest or NPC link in the writer's client language: translate each one as a single "
              "[bracketed] unit in place — the official Korean client name if you know it, otherwise a plain transliteration "
              "(never a different place or item); Korean ones stay exactly as written.")
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
                 "Traditional Chinese is common (補 = fill the missing spot). "
                 "Whole pinyin words appear too: zuwo / zu wo / zw = invite me (저 초대해 주세요), FM = enchanting (마법부여), "
                 "SK = 死矿 (죽음의 폐광). Never turn an unknown short word into a place name — keep it as written.")
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
    if "[" in text:
        parts.append(LINK_RULES)
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


def fix_terms(out: str, hints: dict) -> str:
    """번역에 사전 용어가 한 글자 틀리게 들어갔으면 바로잡는다(통곡의 동율 → 통곡의 동굴, 성난불킬 → 성난불길).
    알려 준 영어 약어가 번역에 그대로 남았으면 한국어 이름으로(TB 소환 → 썬더 블러프 소환)."""
    for surface, name in hints.items():
        core = name.split("(")[0].strip()
        if (surface.isascii() and surface.isupper() and len(surface) >= 2 and re.search(r"[가-힣]", core)
                and core not in out and "~" not in core):
            out = re.sub(rf"(?<![A-Za-z]){re.escape(surface)}(?![A-Za-z])", core, out, count=1)
    for name in set(hints.values()):
        core = name.split("(")[0].strip()
        if len(core) < 3 or core in out or not re.search(r"[가-힣]", core):
            continue
        n = len(core)
        for i in range(len(out) - n + 1):
            seg = out[i:i + n]
            if sum(a != b for a, b in zip(seg, core)) == 1 and seg[0] == core[0]:
                out = out[:i] + core + out[i + n:]
                break
    return out


def translate(text: str, model: str | None = None, chinese: bool = False) -> tuple[str, float]:
    """model 을 안 주면 지금의 MODEL(통역 창이 설정에서 바꾼다) — 기본값에 MODEL 을 박으면 처음 값(12b)에 묶였다(2026-10-01).
    chinese: 중국 사용자가 쓴 글(이름이 한자 등) — 한자 없이 'NY*3DPS' 만 써도 병음 약자(NY = 怒焰 성난불길 협곡)로 읽는다.
    한자가 없으면 영어 약어로 읽어 NY 를 '냥꾼'으로 옮겼다(2026-10-01 모니터링)."""
    text = normalize_ocr(text)
    terms = matched_terms(text, chinese)
    out, secs = chat_json(model or MODEL, system_prompt(terms, text, chinese), text, SCHEMA)
    return fix_terms(out.get("ko", "").strip(), terms), secs


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
