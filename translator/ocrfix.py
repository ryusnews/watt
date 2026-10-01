"""OCR 이 헷갈리는 글자(0 · O · o, 1 · l · I · |) 바로잡기 — 번역 전에, 라틴 낱말만.

12px 글자에서는 모양으로 가를 수 없다(글꼴 · 굵기 실험, #31). 그래서 문맥으로:
- 낱말: 헷갈리는 글자를 바꿔 본 후보 중 게임 용어 사전 · 채팅 상용어에 있는 것(Ifg → lfg, HeaIer → healer, t00 → too)
- 숫자: 숫자가 섞인 자리는 숫자로(1Og → 10g, 2Os → 20s, lv2O → lv20, LFIM → LF1M)
- 홀로 선 l · | 는 영어 I
모르는 낱말은 그대로 둔다(스페인어 Ilegada 등 — 번역 AI 가 문맥으로 읽는다).
"""
import itertools
import re

from . import terms

# 채팅 상용어(파티 찾기 · 거래 · 잡담) — 사전(wow_terms.json)의 영어 이름 · 약어와 함께 쓴다
COMMON = """
lf lfg lfm lfw lf1 lf2 lf3 lf4 lf1m lf2m lf3m lf4m lvl lvls lv pst pls plz inv invite invites whisper w tank tanks heal heals
healer healers dps dd need needs only looking look last spot spots group grp party run runs boost boosting carry summon summons
sum sums port ports wts wtb wtt sell selling sells buy buying price each ea gold mats enchant enchants enchanting quest quests
elite kill help anyone lol lmao lmfao omg ty thx thanks np gg ok okay yes no all the to too for and of in on at with my your me
you we they it is are be have has will can cant dont im u ur r k kk brb afk gl hf world pvp raid raids guild recruiting core
roster dungeon dungeons boss bosses loot hello hi hey what where when who why how this that there here good bad nice cool
please full clear cleared still left long walk going go come came coming online offline team leveling level levels
items item bags bag slot slots cloth leather mail plate weapon weapons ring rings neck trinket trinkets chest
mage mages priest priests rogue rogues hunter hunters warrior warriors warlock warlocks lock locks druid druids shaman shamans
paladin paladins pally hunt war sham resto holy disc prot fury arms feral ret shadow
also just like love know want wants think need people play playing player players server servers
if i a an as at am an ill lil all old oil hold told sold gold till tell well will fill full kill skill still
lost lot lots soon so do does done off one on onto open or our out over own we well wolf
""".split()


def _lexicon() -> set[str]:
    lex = set(COMMON)
    for t in terms.TERMS:
        for s in [t.get("en", "")] + t.get("en_abbr", []):
            for w in re.findall(r"[A-Za-z0-9]+", s):
                lex.add(w.lower())
    return lex


LEX = _lexicon()
ALT = {"l": "lI1i", "I": "lI1i", "1": "1lI", "|": "lI1", "i": "il", "0": "0oO", "O": "Oo0", "o": "o0O"}
NUM_UNIT = re.compile(r"^(?P<num>[0-9OoIl|]+)(?P<unit>k|kk|g|s|c|m|x|h|hr|hrs|min|mins|lr|lvl)?$", re.I)
LV_NUM = re.compile(r"^(?P<lv>lvl?|lv)(?P<num>[0-9OoIl|]+)$", re.I)
TOKEN = re.compile(r"[A-Za-z0-9|]+")


def _digits(s: str) -> str:
    return s.translate(str.maketrans("OoIl|", "00111"))


def _cased(word: str, like: str) -> str:
    """원래 낱말의 대소문자 꼴을 따라(LFIM → LF1M, Ifg → lfg)."""
    if like.isupper() and len(like) > 1:
        return word.upper()
    if like[:1].isupper() and like[1:].islower() and word[:1].isalpha() and like[:1] not in "Il":
        return word[:1].upper() + word[1:]
    return word


def fix_token(tok: str) -> str:
    low = tok.lower()
    if low in LEX or len(tok) == 1:  # 한 글자(I · 1 · l)는 문맥 없이 못 가른다 — 홀로 선 l 은 fix() 가 I 로
        return tok
    # 숫자 자리: 진짜 숫자(0 · 2–9)가 하나라도 있어야 숫자로 본다 — log · lol 같은 낱말은 건드리지 않는다
    m = LV_NUM.match(tok)
    if m and re.search(r"\d", m["num"]):
        return m["lv"] + _digits(m["num"])
    m = NUM_UNIT.match(tok)
    if m and len(m["num"]) >= 2 and re.search(r"\d", m["num"]):  # 1Og · 2Os · 3O — 0K(OK) 같은 한 자리는 낱말 쪽으로
        return _digits(m["num"]) + (m["unit"] or "")
    # 낱말: 헷갈리는 글자를 바꿔 본 후보 중 아는 낱말(바꾸는 자리 4곳까지).
    # 두 글자 이하는 lf · lv 만(Mi → Ml 같은 엉뚱한 바꿈 방지), 숫자 1 로 시작하면 숫자로 둔다(4=1T · 1m — 1명 · 1분)
    if len(tok) <= 2 and tok.lower() not in ("if", "lf", "iv", "lv", "lt", "0k") or tok[0] in "123456789":
        return tok
    pos = [i for i, c in enumerate(tok) if c in ALT]
    if not pos or len(pos) > 4:
        return tok
    best = None
    for combo in itertools.product(*(ALT[tok[i]] for i in pos)):
        cand = list(tok)
        for i, c in zip(pos, combo):
            cand[i] = c
        w = "".join(cand)
        if w.lower() in LEX:
            changes = sum(1 for i, c in zip(pos, combo) if tok[i] != c)
            if best is None or changes < best[0]:
                best = (changes, w.lower())
    return _cased(best[1], tok) if best else tok


def fix(text: str) -> str:
    """라틴 낱말만 고친다(한글 · 한자 · 키릴은 그대로). 홀로 선 l · | 는 I."""
    text = re.sub(r"(?:(?<=\s)|^)[l|](?=\s+[a-z])", "I", text)
    return TOKEN.sub(lambda m: fix_token(m.group(0)), text)
