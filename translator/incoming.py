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
SCHEMA = {"type": "object", "properties": {"ko": {"type": "string"}, "src": {"type": "string"}}, "required": ["ko", "src"]}
# 라틴 · 키릴 문자로 쓰는 언어 중 원문 표시를 따로 붙일 것 — 화면의 EN 대신 ES · DE · FR … (#76)
SRC_TAGS = {"es", "de", "fr", "pt", "it", "nl", "pl", "tr", "sv", "da", "no", "fi", "cs", "ro", "hu", "uk", "bg", "sr"}


BASE = ("You translate one World of Warcraft Classic chat message into natural Korean for a Korean player. "
        "Expand chat abbreviations (LF/LFM = 구함, LFG = 파티 찾음, WTS = 팝니다, WTB = 삽니다, afk = 자리 비움, inv = 초대, "
        "summ = 소환). Use the official Korean client names for dungeons, zones and classes. "
        "Prices 20s / 5g / 50c = 20실버, 5골드, 50코퍼. "
        "Never add words or requests that are not in the message; if parts are unreadable OCR noise, translate only the "
        "readable parts. Keep player names, numbers and [bracketed] text as units. Put only the translation in 'ko'. "
        "Put the language the message is written in as an ISO 639-1 code in 'src' (en, es, de, fr, pt, ru, uk, zh, ...).")
# 글에 [ 가 있을 때만 — 늘 넣으면 링크가 없는 글의 용어(血色)까지 괄호를 씌우고 사전 용어를 놓쳤다(2026-10-01)
LINK_RULES = ("[Bracketed text] is an item, quest or NPC link in the writer's client language: keep each one as a single "
              "[bracketed] unit in place. English ones stay exactly as written; Chinese or Russian ones become a plain Korean "
              "transliteration (never a different place or item); Korean ones stay exactly as written.")
# 글자 종류에 따라 붙이는 규칙 — 전부 넣으면 870토큰이 되어 게임 중 번역이 3~7초로 느려졌다(2026-10-01)
LATIN_RULES = ("'<class or role> LFG <dungeon>' means the writer is that class/role and wants to JOIN a group "
               "(Tank lfg WC = 탱커가 통곡의 동굴 파티를 찾음); 'LF/LFM <role>' = recruiting that role. "
               "Map misspelled game names to the closest real one (STORWIND = 스톰윈드). "
               "OCR misreads g as a and l as I: Ifg/lfa = lfg, Ifm = lfm, roaue = rogue, aoina = going, IVI = lvl, If = lf. "
               "'X would go hard' = X would be awesome. '/who 21' = the /who player search (21레벨 검색), not a channel. "
               "summ/sum = a summoning service (소환 서비스), never a pet. Keep slash commands as typed (/reload, /inv, /roll), "
               "never translate them. French 'du monde pour X ?' = anyone for X? (X 갈 사람?), 'dispo' = available to join.")
# 'tank LF RFC' — 역할이 LF 앞이면 그 사람이 파티를 찾는다(LFG). 모델은 LF 를 보고 '탱커 구함'으로 옮겼고, 프롬프트에 예를
# 넣으면 거꾸로 'LF heal WC'(힐러 구함)까지 '힐러가 파티 찾음'으로 바꿨다 — 번역 전에 LFG 로 바꿔 넘긴다(2026-10-02)
ROLE_LF = re.compile(r"(?i)^\s*(tanks?|heals?|healers?|dps|melee|ranged|warrior|warr|rogue|hunter|mage|priest|warlock|lock|"
                     r"druid|shaman|sham|paladin|pala|pally)\s+LF(?=\s+[A-Za-z])"
                     r"(?!\s+(?:tanks?|heals?|healers?|dps|more|\d)\b)")  # LF 뒤가 사람(LF 1 dps · LF heal)이면 모집 그대로
# 시간 뒤의 PST · EST 는 미국 시간대 — 사전의 'pst = 귓속말 주세요' 를 쓰면 '1 pm pst' 가 '오후 1시 귓속말 주세요'(2026-10-02)
TIMEZONE = re.compile(r"(?i)(?:\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\b(?:morning|noon|afternoon|evening|tonight|midnight))\s*"
                      r"(?:pst|pdt|pt|est|edt|et|cst|cdt|mst|mdt|cet|cest|gmt|utc|bst)\b")
TIME_RULES = ("Here PST/PDT/EST/CST/CET/GMT after a time are time zones (PST = 미국 태평양 시간, EST = 미국 동부 시간, "
              "CET = 중앙유럽 시간), not 'please send tell'; am/pm are 오전/오후.")
# 던전 예를 하나(怒焰 = 성난불길 협곡) 들었더니 작은 모델이 중국어 던전을 거의 다 '성난불길 협곡'으로 옮겼다 — 이름은 사전 힌트로만(#137)
CHINESE_RULES = ("Chinese LFG ads shorten dungeon names and even to pinyin initials "
                 "(AH = 哀嚎 = 통곡의 동굴, never the auction house). 'N=M' counts players (哀嚎4=1 = 4명 있음 1명 더 구함; "
                 "'=3' = 3명 더 구함). 任意 = any class, 缺 = need, 来/组人 = recruiting, 求组 = wants to JOIN a group, "
                 "奶 or N = healer, T = tank, 近战 = 근접 딜러, 远程 = 원거리 딜러, 速刷 = 빠르게 도는, 开搞 = 출발, 带 = 버스. "
                 "Classes by pinyin initials: MS 사제, FS 마법사, SS 흑마법사, LR 사냥꾼, DZ 도적, XD 드루이드, SM 주술사, "
                 "QS 성기사, ZS 전사; a level glued to a class is level + class (20LR = 20레벨 사냥꾼). "
                 "Traditional Chinese is common (補 = fill the missing spot). "
                 "Whole pinyin words appear too: zuwo / zu wo / zw = invite me (저 초대해 주세요), FM = enchanting (마법부여), "
                 "SK = 死矿 (죽음의 폐광). 开30 / 升30 = the level cap 30 opens (30레벨 열리다), not recruiting. "
                 "Never turn an unknown short word into a place name — keep it as written.")
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
    if TIMEZONE.search(text):
        parts.append(TIME_RULES)
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


# '1DD' · '2dps' · '1tank' — 숫자에 붙은 역할을 띄워 사전이 찾게(LF 1DD RFC → '1던전', PC방 2026-10-02)
NUM_ROLE = re.compile(r"(?i)\b(\d{1,2})(dds?|dps|tanks?|heals?|healers?)\b")


# 파티 인원 'N=M'(N명 있음, M명 더 구함) · '=M'(M명 더 구함) — 번역 전에 한국어로 풀어 둔다. 프롬프트로 알려 줘도 E4B 는
# '来个奶 4=1' 을 '힐러 4명 구함 1명 더 구함'으로 옮겼다(PC방 2026-10-05, #132). '=>RFC' · '@225' · 소수점은 아님
PARTY_COUNT = re.compile(r"(?<![0-9.=<>])(\d{1,2})\s*=\s*(\d{1,2})(?![0-9>.])")
PARTY_NEED = re.compile(r"(?<![0-9A-Za-z.=<>])=\s*(\d{1,2})(?![0-9>.])")


def party_counts(text: str) -> str:
    text = PARTY_COUNT.sub(lambda m: f" ({m[1]}명 있음, {m[2]}명 더 구함) ", text)
    text = PARTY_NEED.sub(lambda m: f" ({m[1]}명 더 구함) ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def normalize_ocr(text: str) -> str:
    text = ocrfix.fix(text)  # 0 · O · o, 1 · l · I · | — 사전 · 상용어 · 숫자 자리로(#31)
    for rx, rep in OCR_FIXES:
        text = rx.sub(rep, text)
    return party_counts(NUM_ROLE.sub(r"\1 \2", text))


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
    # 알려 준 던전이 빠지고 다른 던전이 들어갔으면 바꾼다 — 작은 모델(E2B)이 哀嚎 = 통곡의 동굴을 알려 줘도 '성난불길 협곡'으로
    # 옮겼다(PC방 2026-10-05, #137). 알려 준 던전이 하나 · 들어간 엉뚱한 던전이 하나일 때만
    places = terms.place_names()
    want = {v.split("(")[0].strip() for v in hints.values()} & places
    missing = [p for p in want if p not in out]
    wrong = [p for p in places - want if p in out]
    if len(missing) == 1 and len(wrong) == 1:
        out = out.replace(wrong[0], missing[0])
    return out


LINK = re.compile(r"\[[^\[\]]+\]")
_link_ko: dict[str, str] = {}  # 한자 · 키릴 링크 → 처음 옮긴 한국어 — 같은 링크는 늘 같은 이름으로(#113)


def fix_links(src: str, out: str, hints: dict) -> str:
    """번역의 [링크]를 원문 링크와 차례로 맞춘다. 같은 링크를 볼 때마다 다른 이름으로 지어냈다
    ([Coldflame Saber] → [차가운 불꽃의 검], 몇 분 뒤 [냉기불꽃 세이버] — 2026-10-04 모니터링, #113).
    - 사전에 있는 링크 → 사전 이름
    - 영어 등 라틴 · 한글 링크 → 원문 그대로(한국 클라이언트 이름은 모델이 모른다 — 지어내지 않게)
    - 한자 · 키릴 링크 → 처음 옮긴 이름을 기억해 다음부터 같게
    링크 개수가 다르면(모델이 합치거나 뺐으면) 손대지 않는다."""
    a, b = LINK.findall(src), LINK.findall(out)
    if not a or len(a) != len(b):
        return out
    low = {k.lower(): v.split("(")[0].strip() for k, v in hints.items()}
    parts, pos = [], 0
    for s_link, o_link, m in zip(a, b, LINK.finditer(out)):
        inner = s_link[1:-1].strip()
        if inner.lower() in low:
            want = f"[{low[inner.lower()]}]"
        elif _CJK.search(inner) or _CYR.search(inner):
            want = _link_ko.setdefault(s_link, o_link)
        else:
            want = s_link
        parts.append(out[pos:m.start()] + want)
        pos = m.end()
    return "".join(parts) + out[pos:]


def translate(text: str, model: str | None = None, chinese: bool = False) -> tuple[str, float]:
    ko, secs, _ = translate_src(text, model, chinese)
    return ko, secs


def translate_src(text: str, model: str | None = None, chinese: bool = False) -> tuple[str, float, str]:
    """(번역, 걸린 초, 원문 언어 코드) — 원문 언어는 모델이 본 것(소문자 두 글자, 모르면 '')."""
    """model 을 안 주면 지금의 MODEL(통역 창이 설정에서 바꾼다) — 기본값에 MODEL 을 박으면 처음 값(12b)에 묶였다(2026-10-01).
    chinese: 중국 사용자가 쓴 글(이름이 한자 등) — 한자 없이 'NY*3DPS' 만 써도 병음 약자(NY = 怒焰 성난불길 협곡)로 읽는다.
    한자가 없으면 영어 약어로 읽어 NY 를 '냥꾼'으로 옮겼다(2026-10-01 모니터링)."""
    text = ROLE_LF.sub(r"\1 LFG", normalize_ocr(text))
    terms = matched_terms(text, chinese)
    if TIMEZONE.search(text):  # 시간대로 쓴 pst · pm 은 '귓속말 주세요' 가 아니다
        terms = {k: v for k, v in terms.items() if k.lower() not in ("pst", "pm")}
    out, secs = chat_json(model or MODEL, system_prompt(terms, text, chinese), text, SCHEMA)
    src = str(out.get("src", "")).strip().lower()[:2]
    return fix_links(text, fix_terms(out.get("ko", "").strip(), terms), terms), secs, src if src.isalpha() else ""


BATCH_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "ko": {"type": "string"}, "src": {"type": "string"}},
    "required": ["i", "ko", "src"]}}}, "required": ["items"]}
BATCH_MAX = 5


def _prep(text: str, chinese: bool) -> tuple[str, dict]:
    text = ROLE_LF.sub(r"\1 LFG", normalize_ocr(text))
    terms = matched_terms(text, chinese)
    if TIMEZONE.search(text):
        terms = {k: v for k, v in terms.items() if k.lower() not in ("pst", "pm")}
    return text, terms


def translate_batch(items: list[tuple[str, bool]], model: str | None = None) -> tuple[list[tuple[str, str]], float]:
    """여러 메시지를 한 번에 — [(번역, 원문 언어)], 걸린 초. 번역이 밀릴 때 통역 창이 줄 선 메시지를 묶어 부른다:
    느린 PC(VRAM 이 모자라 모델 일부가 CPU · 공유 메모리, PC방 RTX 4060 + 와우 2개)에서 한 건씩 부르면 '표시까지' 12초,
    밀린 것 11건이었다(2026-10-02). 각 메시지는 따로 번역하고 섞지 않는다. 결과가 이상하면(개수 · 빈 번역) ValueError —
    부른 쪽이 한 건씩 다시."""
    if len(items) == 1:
        ko, secs, src = translate_src(items[0][0], model, items[0][1])
        return [(ko, src)], secs
    prepped = [_prep(t, c) for t, c in items]
    joined = "\n".join(t for t, _ in prepped)
    terms: dict = {}
    for _, tm in prepped:
        terms.update(tm)
    # 앞부분은 한 건 번역과 똑같이 두고 묶음 안내는 끝에 붙인다 — 첫 문장을 바꾸면 Ollama 의 프롬프트 캐시가 통째로 깨져
    # 규칙 약 740토큰을 매번 다시 읽었다(CPU 6스레드 E4B: 3건 묶음 7.2초 > 한 건씩 3번 5.4초, PC방 14.9초 — #131)
    system = system_prompt(terms, joined, any(c for _, c in items))
    system += (" This time the user gives a JSON list of separate messages from different players. Translate EACH one on its own — "
               "never merge, reorder or carry words between them. Return 'items' with one entry per message: i (its number), "
               "ko, src.")
    user = json.dumps([{"i": n, "text": t} for n, (t, _) in enumerate(prepped)], ensure_ascii=False)
    out, secs = chat_json(model or MODEL, system, user, BATCH_SCHEMA)  # 문맥 길이는 한 건과 같게 — 다르면 Ollama 가 모델을 다시 올린다
    got = {int(x.get("i", -1)): x for x in out.get("items", []) if isinstance(x, dict)}
    if sorted(got) != list(range(len(items))) or any(not str(got[n].get("ko", "")).strip() for n in got):
        raise ValueError(f"묶음 번역 결과가 맞지 않음({len(got)}/{len(items)})")
    res = []
    for n, (_, tm) in enumerate(prepped):
        src = str(got[n].get("src", "")).strip().lower()[:2]
        res.append((fix_links(prepped[n][0], fix_terms(str(got[n]["ko"]).strip(), tm), tm), src if src.isalpha() else ""))
    return res, secs


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
