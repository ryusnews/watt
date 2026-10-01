"""WoW 실시간 통역(시제품) — 채팅창을 읽어 영어·중국어 메시지를 한국어로 번역해 게임 위 창에 띄운다.

python -m watt --role live           # 채팅 영역 자동 찾기 → 0.5초마다 읽기 → 번역 창
흐름: watt.chat_region(영역) → watt.ocr(바뀌었을 때만, en/ko/zh/ru 엔진) → 줄마다 엔진 고르기 → 줄바꿈 합치기
      → 새 메시지만 → 캐시 → gemma4 번역(incoming) → 창. 게임에 입력하지 않는다.
창: 끌어서 옮기기 · 오른쪽 클릭 → 영역 다시 찾기 / 종료.
"""
import ctypes
import difflib
import json
import queue
import re
import threading
import time
import tkinter as tk
from collections import Counter, deque
from pathlib import Path

from watt import chat_region, paths, screen, settings
from watt import tkstyle as tks
from watt.ocr import Reader

from . import incoming
from .adfilter import AdFilter

LOG = paths.LOGS / "live.jsonl"
INTERVAL = 0.5
SCALE = 2

HANGUL = re.compile(r"[가-힣]")
CJK = re.compile(r"[一-鿿]")
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
LATIN = re.compile(r"[A-Za-z]")
# 라틴 문자와 모양이 다른 키릴 문자 — 러시아어 엔진이 영어를 읽으며 만드는 가짜 키릴(ТВ, Ме1оп)과 가르는 데 쓴다
CYR_DISTINCT = re.compile(r"[БГДЖЗИЙЛФЦЧШЩЪЫЬЭЮЯбвгдёжзийлфцчшщъыьэюяЄєЇїІіҐґ]")


# <길드명> — 라틴 길드명이 키릴 비율을 끌어내리지 않게. [ ] 안은 센다: 머리([이름])는 body_of 가 떼고, 본문의 [링크]는
# 그 사람 언어(중국어 클라이언트의 [哀嚎洞穴])라 빼면 '[哀嚎洞穴] 来T' 가 중국어로 판정되지 않았다(2026-10-01)
TAGS = re.compile(r"<[^>]*>")


CYR_WORD = re.compile(r"[Ѐ-ӿ]{3,}")


def looks_russian(text: str) -> bool:
    """키릴 60%↑ + 러시아어에만 있는 글자 1개↑ + 키릴 낱말(3자↑) 2개↑. 마지막 조건: 러시아어 엔진이 짧은 영어를
    닮은 키릴로 읽은 줄(LF3M DPS DM → Г-ЗМ DPS ОМ, any → апу)을 러시아어로 보지 않게(2026-10-01 모니터링)."""
    core = re.sub(r"\[[^\]]*\]", " ", TAGS.sub(" ", text))  # [링크]는 빼고 — 영어 글 속 러시아어 링크(LFM [Чол'арук])
    cyr, lat = len(CYRILLIC.findall(core)), len(LATIN.findall(core))
    return (cyr >= 3 and cyr >= 0.6 * (cyr + lat) and len(CYR_DISTINCT.findall(core)) >= 1
            and len(CYR_WORD.findall(core)) >= 2)


def is_junk(body: str) -> bool:
    """OCR 잡음(*fifi + +, R-fi±T2, 4/ixFf#) — 번역하지 않는다. 글자(한글·한자·라틴·키릴)가 4자 미만이거나 기호·숫자가 35%↑."""
    chars = re.sub(r"\s", "", body)
    letters = len(re.findall(r"[A-Za-zЀ-ӿ一-鿿가-힣]", chars))
    if letters < 4 and not (letters >= 2 and CJK.search(chars)):
        return True
    return (len(chars) - letters) / max(1, len(chars)) >= 0.35


def _letters(text: str) -> int:
    return (len(HANGUL.findall(text)) + len(CJK.findall(text)) + len(LATIN.findall(text))
            + len(CYRILLIC.findall(text)))


def looks_chinese(text: str) -> bool:
    """한자가 글자의 25%↑([이름]·<길드> 빼고) — 키릴·라틴을 읽다 섞여 나온 한자 몇 개(月角 沩)는 걸러진다."""
    core = TAGS.sub(" ", text)
    han = len(CJK.findall(core))
    # 0.25: 중국 사용자는 병음 · 영어 약자를 섞는다(NY来法系DPS和N — 한자 4/13). 키릴 · 라틴을 읽다 나온 가짜 한자는 그보다 적다
    return han >= 2 and han >= 0.25 * _letters(core)


def looks_korean(text: str) -> bool:
    """한글 2자↑이고 글자의 절반↑([이름]·<길드> 빼고) — 한국어 엔진이 키릴·한자를 읽다 만든 가짜 한글(너, 뇌) 몇 개는 걸러진다."""
    core = TAGS.sub(" ", text)
    hangul = len(HANGUL.findall(core))
    return hangul >= 2 and hangul >= 0.5 * _letters(core)


def english_garbled(text: str) -> bool:
    """영어 엔진이 키릴 글을 억지로 읽은 흔적: 숫자·글자가 섞인 낱말(3apa3, C06i), 대소문자가 뒤죽박죽인 낱말(nOTVXHV)."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", text) if len(w) >= 3]
    if not words:
        return False
    bad = sum(1 for w in words if (re.search(r"\d", w) and re.search(r"[A-Za-z]", w))
              or re.search(r"[a-z][A-Z]", w))
    return bad / len(words) >= 0.25
# [채널] [이름]: 본문 — 채널 번호가 빠지거나(OCR) 괄호 없이 읽혀도(2 [이름]:) 새 메시지로 본다.
# 채널을 못 읽은 줄을 줄바꿈으로 오인해 여러 메시지가 한데 붙던 문제(2026-09-30 분석)
# 이름 뒤 콜론을 마침표·쉼표로 읽는 경우도 받는다([6] [Skiddo Prime]. LF3M BFD)
# 외침·귓속말은 채널 번호 없이 [이름]님의 외침: — 중국어 엔진은 '님의'를 '9 | 9' 로 읽는다. 이걸 머리로 못 알아봐
# 외침 광고가 바로 위 메시지에 붙어 번역되던 문제(2026-10-01 분석: 광고 110건 중 75건이 남의 메시지에 붙음)
HEADER = re.compile(r"^\s*(?:[\[〔(]?\s*(?P<ch>\d{1,2})\s*[\]〕)lIJ|]?\s*)?[\[〔(]\s*(?P<name>[^\[\]〔〕(（]{1,32}?)\s*[\]〕)lJ]"
                    r"\s*(?P<kind>님(?:의|에게)\s*\S{1,4}(?:\s\S{1,3})?|(?:[ßB]|Lel)?\s*9(?:\s*\|\s*9)?|says|yells|whispers)?\s*[:：.,;|]\s*(?P<body>.*)$")


# 줄 앞에 무엇이 붙든(기본 채팅 [1. 공개 - 오그리마] · 시간 표시 · 애드온) 마지막 [이름]: 을 머리로 —
# 왼쪽 여백에서 시작하는 줄에만 쓴다(들여쓴 뒷줄의 [아이템]: 을 머리로 오인하지 않게). #10 의 첫 단계
GENERIC = re.compile(r"^(?P<pre>.{0,60}?)[\[〔(]\s*(?P<name>[^\[\]〔〕(（]{1,32}?)\s*[\]〕)lJ1I|]"
                     r"\s*(?P<kind>님(?:의|에게)\s*\S{1,4}(?:\s\S{1,3})?|(?:[ßB]|Lel)?\s*9(?:\s*\|\s*9)?|says|yells|whispers"
                     r"|[^\s:：\[\]]{1,4}(?:\s+[^\s:：\[\]]{1,4}){0,2})?\s*[:：]\s*(?P<body>.*)$")  # 끝: 깨진 '님의 외침'(Е91 91 Е)
PRE_CH = re.compile(r"(?:^|[\[〔(lI|])\s*(\d{1,2})\s*[.,\]〕)]")  # [1. 공개] · [6] — [12:30] 같은 시간은 아님


def parse_header(text: str, generic: bool = False) -> dict | None:
    """{ch, name, body} — [6] [이름]: 꼴, generic 이면 앞에 무엇이 붙은 [이름]: 도."""
    m = HEADER.match(text or "")
    if m:
        return {"ch": _channel(m), "name": m.group("name"), "body": m.group("body")}
    if generic:
        g = GENERIC.match(text or "")
        if g:
            pre = PRE_CH.search(g.group("pre"))
            return {"ch": pre.group(1) if pre else _channel(g), "name": g.group("name"), "body": g.group("body")}
    return None


def _channel(m: re.Match) -> str | None:
    """채널 번호, 없으면 외침·귓말."""
    kind = (m.group("kind") or "").replace(" ", "")
    return m.groupdict().get("ch") or ("외침" if "외침" in kind or kind == "yells" else
                             "귓말" if "귓속말" in kind or kind == "whispers" else None)


BRACKET = re.compile(r"\[[^\[\]]{2,60}\]")


def _clean_cyrillic(s: str) -> bool:
    cyr, lat = len(CYRILLIC.findall(s)), len(LATIN.findall(s))
    return cyr >= 3 and cyr >= 0.6 * (cyr + lat) and len(CYR_DISTINCT.findall(s)) >= 2


def _clean_chinese(s: str) -> bool:
    han = len(CJK.findall(s))
    return han >= 2 and han >= 0.5 * _letters(s)


def repair_segments(text: str, ru: str | None, zh: str | None) -> str:
    """영어로 고른 줄 안의 [링크](러시아어·중국어 아이템·NPC 이름)는 그 언어 엔진이 읽은 것으로 바꾼다
    (2026-09-30: LFM [Hon'apyK CTePBRTHVIK] ← 러시아어 엔진은 [Чол'арук Стерв…])."""
    segs = BRACKET.findall(text)
    if not segs:
        return text
    for other, clean in ((ru, _clean_cyrillic), (zh, _clean_chinese)):
        alt = BRACKET.findall(other or "")
        if len(alt) != len(segs):
            continue
        for mine, theirs in zip(segs, alt):
            if clean(theirs[1:-1]) and not clean(mine[1:-1]):
                text = text.replace(mine, CJK_GAP.sub("", theirs), 1)
    return text
CJK_GAP = re.compile(r"(?<=[　-鿿＀-￯])\s+(?=[　-鿿＀-￯])")


def find_region() -> dict:
    """채팅 영역(화면 좌표). 너무 작게 잡히면(채팅 줄이 한두 개뿐인 순간) 오류 — 지난 정상 영역을 쓴다."""
    return chat_region.screen_rect(chat_region.find_and_save())


# ---- 줄 고르기 · 메시지 만들기
HEAD_CUT = re.compile(r"^.{0,72}?[\]〕)lJ1I|]\s*(?:님(?:의|에게)\s*\S{1,4}(?:\s\S{1,3})?)?\s*[:：]\s*")


def body_of(text: str) -> str:
    """언어 판정용 본문 — 머리([6. 파티찾기] [이름]:)를 뺀다. 기본 채팅은 머리에 한국어 채널 이름이 붙고 이름은 라틴이라,
    머리째 판정하면 한국어 본문이 영어로, 영어 본문이 한국어로 갈린다(2026-10-01 모니터링)."""
    return HEAD_CUT.sub("", text or "", count=1)


def pick_lines(lines: dict, line_h: int) -> list[dict]:
    """엔진들의 같은 위치 줄 중 그럴듯한 것: 한글 → ko, 한자 → zh, 진짜 러시아어(키릴 60%↑) → ru, 그 밖 → en."""
    en, ko, zh, ru = (lines.get("en-US") or [], lines.get("ko") or [], lines.get("zh-Hans-CN") or [],
                      lines.get("ru-RU") or [])
    anchors = sorted(en + ko + zh + ru, key=lambda l: l["y"])
    rows: list[list[dict]] = []
    for l in anchors:  # 세로 위치로 같은 줄 묶기
        if rows and abs(rows[-1][0]["y"] - l["y"]) <= line_h * 0.5:
            rows[-1].append(l)
        else:
            rows.append([l])
    out = []
    for row in rows:
        def by(src):
            return next((l for l in row if l in src), None)
        k, z, e, r = by(ko), by(zh), by(en), by(ru)
        feat = {"hangul": len(HANGUL.findall(body_of(k["t"]))) if k else 0, "ko": bool(k and looks_korean(body_of(k["t"]))),
                "zh": bool(z and looks_chinese(body_of(z["t"]))), "ru": bool(r and looks_russian(body_of(r["t"]))),
                "en_garbled": bool(e and english_garbled(body_of(e["t"])))}
        if feat["ko"]:
            chosen, lang = k, "ko"
        elif feat["zh"]:
            chosen, lang = z, "zh"
        elif feat["ru"] and (not e or feat["en_garbled"]):
            chosen, lang = r, "ru"  # 영어 엔진이 깨끗하게 읽은 줄은 영어로 둔다(짧은 영어가 가짜 키릴로 읽힐 때)
        else:
            chosen, lang = (e or k or z or r), "en"  # 러시아어 엔진만 찾은 줄도 있다
        text = CJK_GAP.sub("", chosen["t"]) if lang == "zh" else chosen["t"]
        if lang == "en":
            text = repair_segments(text, r and r["t"], z and z["t"])
        row = {"y": chosen["y"], "x": min(l["x"] for l in row), "text": text, "lang": lang, "feat": feat,
               "cand": {"en": e and e["t"], "ko": k and k["t"], "zh": z and z["t"], "ru": r and r["t"]}}  # 추적용
        if not parse_header(text):  # 고른 엔진이 머리를 깨뜨렸으면 머리를 제대로 읽은 다른 엔진 것을 따로 둔다
            row["alt_header"] = next((c["t"] for c in (e, k, r, z) if c and (h := parse_header(c["t"], True)) and h["ch"]),
                                     None)
        out.append(row)
    return out


def line_pitch(rows: list[dict], default: int) -> int:
    """화면의 실제 줄 간격(이웃한 줄의 y 차이 중앙값). 저장된 줄 높이는 영역을 찾을 때 잰 값이라, 채팅 글자 크기를 바꾸면
    (14 → 16pt) 뒷줄이 떨어져 나간다 — 매 화면 다시 잰다."""
    ys = sorted(r["y"] for r in rows)
    gaps = [b - a for a, b in zip(ys, ys[1:]) if default * 0.6 <= b - a <= default * 3]
    if len(gaps) < 3:
        return default
    gaps.sort()
    return max(8, int(gaps[len(gaps) // 2]))


def build_messages(rows: list[dict], line_h: int, orphans: list | None = None) -> list[dict]:
    """머리([채널] [이름]:)로 시작하는 줄 + 이어지는 줄바꿈 줄 = 메시지. 머리 없는 맨 위 조각·시스템 메시지는 버린다.
    orphans 를 주면 버린 줄(머리도 아니고 이어지는 줄도 아닌 것)을 담는다 — 머리 인식 실패를 찾는 데 쓴다."""
    msgs, cur, last_y = [], None, None
    # 들여쓰기는 채팅창 왼쪽 여백 기준 — 머리 줄 x 기준이면, OCR 이 [6] 을 빠뜨려 머리 줄이 오른쪽에서 시작할 때
    # 들여쓴 뒷줄이 떨어져 나간다(2026-09-30 frame 122: 3줄 광고가 첫 줄만 번역)
    # 왼쪽 여백 = 머리를 읽은 줄들의 왼쪽 끝. 그냥 가장 왼쪽 줄로 하면 채팅창 왼쪽 버튼(스피커 아이콘)을 읽은
    # 부스러기가 기준이 돼, 메시지 첫 줄을 앞 메시지의 뒷줄로 붙였다(2026-10-01, 채팅창을 키웠을 때)
    heads = [r["x"] for r in rows if parse_header(r["text"], True) or r.get("alt_header")]
    margin = min(heads) if heads else min((r["x"] for r in rows), default=0)
    for i, r in enumerate(rows):
        at_margin = abs(r["x"] - margin) < line_h * 0.4  # 왼쪽 여백에서 시작 = 새 메시지(뒷줄은 들여쓴다)
        h = parse_header(r["text"], at_margin)
        body = h["body"] if h else None
        if not h and r.get("alt_header"):  # 머리는 다른 엔진 것으로, 본문은 고른 엔진 것(첫 ]: 뒤)으로
            h = parse_header(r["alt_header"], True)
            colon = re.search(r"[\]〕)lJ]\s*\S{0,6}\s*[:：]", r["text"][:72]) or re.search(r"[:：]", r["text"][:72])
            body = r["text"][colon.end():] if colon else h["body"]
        if h:
            ch = h["ch"]
            if not ch:  # 고른 엔진이 채널을 빠뜨렸으면 다른 엔진이 읽은 것으로(외침·귓말은 한국어 엔진이 잘 읽는다)
                ch = next((c["ch"] for c in (parse_header(x or "", at_margin) for x in r["cand"].values()) if c and c["ch"]),
                          None)
            cur = {"ch": ch or "?", "name": h["name"].strip(), "body": body.strip(), "lang": r["lang"], "y": r["y"],
                   "x": r["x"], "rows": [r]}
            msgs.append(cur)
            last_y = r["y"]
        elif at_margin and i > 0:  # 머리 모양을 못 읽었어도 여백에서 시작하면 새 메시지(이름 모름). 맨 윗줄은 잘린 조각·탭 이름
            colon = re.search(r"[\]〕)lJ]\s*\S{0,6}\s*[:：]", r["text"][:72])
            cur = {"ch": "?", "name": "", "body": (r["text"][colon.end():] if colon else r["text"]).strip(),
                   "lang": r["lang"], "y": r["y"], "x": r["x"], "rows": [r]}
            msgs.append(cur)
            last_y = r["y"]
        elif cur and r["y"] - last_y <= line_h * 1.6 and not at_margin:  # 바로 아래 + 들여쓴 줄 = 줄바꿈된 뒷부분
            cur["body"] += ("" if r["lang"] == "zh" else " ") + r["text"].strip()
            if r["lang"] == "zh":
                cur["lang"] = "zh"
            cur["rows"].append(r)
            last_y = r["y"]
        else:
            cur = None
            if orphans is not None:
                orphans.append(r)
    for m in msgs:
        settle_language(m)
    return msgs


def settle_language(m: dict) -> None:
    """메시지 언어를 줄 전체로 정한다(첫 줄만 보지 않는다). 줄바꿈된 러시아어·우크라이나어가 첫 줄만 영어로 읽혀
    메시지 전체가 영어로 번역되던 문제(2026-09-30 분석). 러시아어로 정해지면 영어로 고른 줄은 러시아어 엔진 결과로 바꾼다."""
    weight = Counter()
    for r in m["rows"]:
        weight[r["lang"]] += len(r["text"])
    foreign = [(w, lang) for lang, w in weight.items() if lang in ("ru", "zh")]
    if weight.get("ko", 0) > sum(weight.values()) / 2:
        m["lang"] = "ko"
    elif foreign:
        m["lang"] = max(foreign)[1]
    else:
        m["lang"] = "en" if weight.get("en") else m["lang"]
    lang = m["lang"]
    if lang in ("ru", "zh") and any(r["lang"] != lang and r["cand"].get(lang) for r in m["rows"]):
        # 메시지 언어가 정해지면 모든 줄을 그 언어 엔진 결과로 통일(다른 엔진이 만든 가짜 한자·깨진 영어가 섞이지 않게)
        parts = []
        for i, r in enumerate(m["rows"]):
            text = r["cand"].get(lang) or r["text"]
            if lang == "zh":
                text = CJK_GAP.sub("", text)
            if i == 0:
                h = parse_header(text, True)
                text = h["body"] if h else text
            parts.append(text.strip())
        m["body"] = ("" if lang == "zh" else " ").join(p for p in parts if p)


def load_ui() -> dict:
    try:
        return json.loads(paths.LIVE_UI.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_ui(ui: dict) -> None:
    paths.ensure()
    paths.LIVE_UI.write_text(json.dumps(ui, ensure_ascii=False, indent=1), encoding="utf-8")


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s).lower()


_LOOKALIKE = str.maketrans({"i": "l", "1": "l", "|": "l", "0": "o"})


def dkey(s: str) -> str:
    """중복 판단용 키 — OCR 이 헷갈리는 글자를 하나로(lfg · Ifg · 1fg). 같은 글이 두 번 번역되던 문제(lfg rfk, 2026-10-01)."""
    return norm(s).translate(_LOOKALIKE)


class Seen:
    """최근 메시지(흔들리는 OCR 을 감안해 비슷하면 같은 것으로)."""

    THRESHOLD = 0.88

    def __init__(self, size: int = 80):
        self.keys: deque[str] = deque(maxlen=size)

    def check(self, key: str) -> tuple[bool, float, str]:
        """(새것인가, 가장 비슷한 것과의 유사도, 그 키). 새것이면 기억한다."""
        best, best_key = 0.0, ""
        for k in self.keys:
            ratio = 1.0 if k == key else difflib.SequenceMatcher(None, k, key).ratio()
            if ratio > best:
                best, best_key = ratio, k
            if ratio >= self.THRESHOLD:
                return False, ratio, k
        self.keys.append(key)
        return True, best, best_key


class Trace:
    """단계별 추적 기록 — logs/trace/trace_YYYYMMDD.jsonl. 분석: python -m tools.analyze_live (tools/analyze_live.py)."""

    def __init__(self):
        self.dir = paths.LOGS / "trace"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sid = time.strftime("%H%M%S")
        self.lock = threading.Lock()
        self.enabled = True  # 번역 설정 '품질 개선용 기록'을 끄면 남기지 않는다

    def write(self, ev: str, **kw) -> None:
        if not self.enabled:
            return
        now = time.time()
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}",
               "sid": self.sid, "ev": ev, **kw}
        path = self.dir / f"trace_{time.strftime('%Y%m%d')}.jsonl"
        with self.lock, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---- 창 — 런처(WATT)와 같은 색·글꼴(watt.tkstyle). 설정은 런처에서 바꾸면 2초 안에 반영
class Overlay:
    def __init__(self, root: tk.Tk, region: dict, cfg: dict):
        self.root = root
        self.cfg = cfg
        tks.load_fonts(root)
        self.show = int(cfg.get("overlay_lines", 10))
        size = int(cfg.get("overlay_font", 11))
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", float(cfg.get("overlay_alpha", 0.88)))
        self.win.configure(bg=tks.LINE)  # 1px 테두리
        body = tk.Frame(self.win, bg=tks.BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        self.region = region
        saved = load_ui().get("overlay")
        self.win.geometry(saved or self._default_geometry(size))
        bar = tk.Frame(body, bg=tks.BAR, height=24)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        mark = tks.logo(bar, 13, tks.BAR)
        mark.pack(side="left", padx=(9, 7))
        self.status = tk.Label(bar, text="", fg=tks.MUTED, bg=tks.BAR, anchor="w", font=(tks.MONO, 8))
        self.status.pack(side="left", fill="x", expand=True)
        close = tk.Label(bar, text="✕", fg=tks.FAINT, bg=tks.BAR, font=(tks.SANS, 9), cursor="hand2")
        close.pack(side="right", padx=8)
        close.bind("<Button-1>", lambda e: root.destroy())
        self.text = tk.Text(body, bg=tks.BG, fg=tks.FG, relief="flat", bd=0, highlightthickness=0, wrap="word",
                            height=self.show, state="disabled", cursor="arrow", spacing1=2, spacing3=4, padx=12, pady=6)
        self.text.pack(fill="both", expand=True)
        self.items: deque[dict] = deque(maxlen=self.show)
        self.apply_font(size)
        for w in (self.win, self.text, self.status, bar, mark):
            w.bind("<ButtonPress-1>", self._start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._save)
            w.bind("<Button-3>", self._menu)
        grip = tk.Label(body, text="◢", fg="#3A4454", bg=tks.BG, cursor="size_nw_se", font=(tks.SANS, 8))
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<ButtonPress-1>", self._grip_start)
        grip.bind("<B1-Motion>", self._grip_drag)
        grip.bind("<ButtonRelease-1>", self._save)
        self.menu = tk.Menu(self.win, tearoff=0, bg=tks.PANEL, fg=tks.FG, activebackground=tks.CTRL,
                            activeforeground=tks.TEAL, bd=0, font=(tks.SANS, 9))
        self.on_refind = None
        self.menu.add_command(label="채팅 영역 다시 찾기", command=lambda: self.on_refind and self.on_refind())
        self.menu.add_command(label="원문 보기", command=self._toggle_original)
        self.menu.add_separator()
        self.menu.add_command(label="통역 끄기", command=root.destroy)

    def _default_geometry(self, size: int) -> str:
        """채팅창 바로 위, 같은 폭."""
        r = self.region
        h = int(size * 4.2) * min(self.show, 6) + 40
        return f"{r['w']}x{h}+{r['x']}+{r['y'] - h - 8}"

    def apply_font(self, size: int) -> None:
        small = max(8, size - 3)
        self.text.configure(font=(tks.SANS, size))
        self.text.tag_configure("meta", foreground=tks.FAINT, font=(tks.MONO, small))
        for code, color in tks.LANG.items():
            self.text.tag_configure("lang_" + code, foreground=color, font=(tks.MONO, small, "bold"))
        self.text.tag_configure("ko", foreground=tks.FG)
        self.text.tag_configure("orig", foreground=tks.FAINT, font=(tks.SANS, small))
        self.text.tag_configure("pending", foreground=tks.DIM)
        self.text.tag_configure("ad", foreground=tks.FAINT, font=(tks.SANS, small))

    def apply(self, cfg: dict) -> None:
        """런처에서 바꾼 설정 — 투명도·글자 크기·줄 수·원문 보기."""
        self.cfg = cfg
        self.win.attributes("-alpha", float(cfg.get("overlay_alpha", 0.88)))
        self.apply_font(int(cfg.get("overlay_font", 11)))
        show = int(cfg.get("overlay_lines", 10))
        if show != self.show:
            self.show = show
            self.items = deque(self.items, maxlen=show)
        self.render()

    def reset_position(self) -> None:
        self.win.geometry(self._default_geometry(int(self.cfg.get("overlay_font", 11))))
        self._save()

    def _toggle_original(self):
        self.cfg["show_original"] = not self.cfg.get("show_original")
        settings.save({"show_original": self.cfg["show_original"]})
        self.render()

    def _start(self, e):
        self._dx, self._dy = e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y()

    def _drag(self, e):
        self.win.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _grip_start(self, e):
        self._gx, self._gy = e.x_root, e.y_root
        self._gw, self._gh = self.win.winfo_width(), self.win.winfo_height()
        return "break"

    def _grip_drag(self, e):
        w = max(260, self._gw + e.x_root - self._gx)
        h = max(100, self._gh + e.y_root - self._gy)
        self.win.geometry(f"{w}x{h}")
        return "break"

    def _save(self, _e=None):
        ui = load_ui()
        ui["overlay"] = self.win.winfo_geometry()
        save_ui(ui)

    def _menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def add(self, item: dict):
        self.items.append(item)
        self.render()

    def render(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for n, it in enumerate(self.items):
            if n:
                self.text.insert("end", "\n")
            lang = it["lang"] if it["lang"] in tks.LANG else "en"
            self.text.insert("end", lang.upper(), "lang_" + lang)
            if it.get("kind") == "ad" and not it.get("ko"):  # 접은 광고 — 한 줄
                self.text.insert("end", f"  {it['name']}  ", "meta")
                self.text.insert("end", "광고", "ad")
                continue
            self.text.insert("end", f"  {it['name']}\n", "meta")
            if it.get("ko"):
                self.text.insert("end", it["ko"], "ko")
                if self.cfg.get("show_original"):
                    self.text.insert("end", "\n" + it["body"], "orig")
            else:
                self.text.insert("end", it["body"], "pending")
        self.text.configure(state="disabled")
        self.text.see("end")


# ---- 본체
class Live:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 물리 픽셀 좌표로(캡처·OCR 과 같게)
        except (OSError, AttributeError):
            pass
        self.cfg = settings.load()
        incoming.MODEL = self.cfg["model"]
        self.root = tk.Tk()
        self.root.withdraw()
        self.region = self.initial_region()
        self.ocr = Reader(SCALE)
        self.overlay = Overlay(self.root, self.region, self.cfg)
        self.overlay.on_refind = self.refind
        self.events: queue.Queue = queue.Queue()
        self.jobs: queue.Queue = queue.Queue()
        self.seen = Seen()
        self.shown: set[str] = set()  # 통역 창에 올린 글(중복 키) — 안 올린 글을 다시 올리면 한 번은 번역
        self.prev_keys: list[tuple[str, int]] = []  # 바로 앞 화면의 (메시지, 위치) — 새 메시지가 나타나는 쪽 판단
        self.seen_body = Seen(30)  # 이름을 매번 다르게 읽어도(舜应盖特 · 舜廐 盖碍) 같은 글이면 한 번만
        self.ads = AdFilter()
        self.cache: dict[str, str] = {}
        self.stats = {"frames": 0, "changed": 0, "ocr_ms": 0, "translated": 0, "tr_s": 0.0}
        self.first = True
        self.running = True
        self.note_until = 0.0
        self.last_status = 0.0
        self.seen_mtimes: dict = {}
        self.win_key = None
        paths.ensure()
        self.trace = Trace()
        self.frame_no = 0
        self.msg_no = 0
        self.orphans_seen: deque[str] = deque(maxlen=300)
        self.last_save = 0.0
        self.trace.write("session", region=self.region, engines=self.ocr.ready.get("engines"), interval=INTERVAL,
                         scale=SCALE, model=incoming.MODEL)
        threading.Thread(target=self.capture_loop, daemon=True).start()
        threading.Thread(target=self.translate_loop, daemon=True).start()
        self.root.after(200, self.poll)

    @staticmethod
    def initial_region() -> dict:
        """켤 때 영역 찾기가 실패해도(로딩 중·채팅 줄 없음) 꺼지지 않는다: 지난번 영역 → 없으면 5초마다 다시."""
        while True:
            try:
                return find_region()
            except Exception:
                good = chat_region.last_good()
                rect = chat_region.locate(good) if good else None
                if rect:
                    return rect
                time.sleep(5)

    def follow_game_window(self):
        """게임 창을 옮기면 영역도 따라가고, 창 크기(해상도)가 바뀌면 다시 찾는다."""
        win = screen.find_game_window()
        if not win:
            return
        key = (win["x"], win["y"], win["w"], win["h"])
        prev, self.win_key = self.win_key, key
        if prev is None or prev == key:
            return
        good = chat_region.last_good()
        rect = chat_region.locate(good, win) if good else None
        if rect:
            self.region = rect
            self.trace.write("region", region=rect, by="window_moved")
        else:
            self.refind()

    def watch_launcher(self):
        self.follow_game_window()
        def mtime(p):
            try:
                return p.stat().st_mtime
            except OSError:
                return 0
        m = mtime(paths.SETTINGS)
        if m != self.seen_mtimes.get("settings"):
            self.seen_mtimes["settings"] = m
            cfg = settings.load()
            self.cfg = cfg
            self.trace.enabled = bool(cfg["keep_logs"])
            self.overlay.apply(cfg)
            incoming.MODEL = cfg["model"]
        m = mtime(paths.REGION_GOOD)
        if m != self.seen_mtimes.get("region"):
            if self.seen_mtimes.get("region") is not None:
                good = chat_region.last_good()
                if good:
                    self.region = chat_region.screen_rect(good)
                    self.trace.write("region", region=self.region, by="launcher")
            self.seen_mtimes["region"] = m
        try:
            cmd = json.loads(paths.LIVE_CMD.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cmd = None
        if cmd and cmd.get("n") != self.seen_mtimes.get("cmd"):
            first = "cmd" not in self.seen_mtimes
            self.seen_mtimes["cmd"] = cmd.get("n")
            if not first:  # 켜기 전에 남은 명령은 무시
                if cmd.get("cmd") == "refind":
                    self.refind()
                elif cmd.get("cmd") == "reset_pos":
                    self.overlay.reset_position()

    def refind(self):
        self.events.put(("status", "채팅 영역 찾는 중…"))
        threading.Thread(target=self._refind, daemon=True).start()

    def _refind(self):
        try:
            self.region = find_region()
            self.trace.write("region", region=self.region)
            self.events.put(("status", f"채팅 영역 다시 찾음: {self.region['w']}×{self.region['h']}"))
        except Exception as e:
            self.events.put(("status", f"영역 찾기 실패: {e}"))

    FRAME_SAVE_EVERY = 3.0   # 초 — 화면 저장 간격
    FRAME_SAVE_MAX = 300     # 하루 최대 장수

    def process_frame(self, r: dict, t0: float) -> None:
        self.frame_no += 1
        self.stats["changed"] += 1
        self.stats["ocr_ms"] = r.get("ms", 0)
        lh = self.region["line_h"]
        rows = pick_lines(r["lines"], lh)
        lh = line_pitch(rows, lh)
        orphans: list = []
        msgs = build_messages(rows, lh, orphans)
        queued, events = [], []
        checked = []
        for m in msgs:
            key = dkey(f"{m['ch']}{m['name']}{m['body']}")
            if not key:
                continue
            new, sim, near = self.seen.check(key)
            body_key = dkey(m["body"])
            if new and len(body_key) >= 4:
                new_b, sim_b, near_b = self.seen_body.check(body_key)
                if not new_b:
                    new, sim, near = False, sim_b, near_b
            m["_key"] = key
            checked.append((m, new, sim, near))
        # 새 메시지는 새 글이 나타나는 쪽(기본 아래, 설정에 따라 위)에만 생긴다. 바로 앞 화면에도 있던 메시지보다 옛 쪽에서
        # '새로' 읽힌 것은 흐려지며 사라지는 옛 줄을 OCR 이 다르게 읽은 것 — 번역하지 않는다(2026-10-01: 채팅창을 키우면 옛 글을
        # 번역). 기준을 '이미 본 글'로 하면 같은 사람이 채널만 바꿔 다시 쓴 글(아래에 새로 나타남)에 진짜 새 글이 묻힌다
        # 기준: 앞 화면에 같은 글이 같은 자리나 더 아래에 있던 것(채팅은 위로 밀리기만 한다). 아래에 새로 나타난 반복 글은 아님
        cur = [(norm(f"{m['ch']}{m['name']}{m['body']}"), m["y"]) for m, *_ in checked]
        top_mode = self.cfg.get("chat_newest", "bottom") == "top"
        stay = [i for i, (k, y) in enumerate(cur)
                if any((k == q or difflib.SequenceMatcher(None, k, q).ratio() >= 0.95)
                       and ((py <= y + 2) if top_mode else (py >= y - 2)) for q, py in self.prev_keys)]
        self.prev_keys = cur
        newest_top = top_mode
        edge = (min(stay) if newest_top else max(stay)) if stay else None
        for i, (m, new, sim, near) in enumerate(checked):
            if not new and not self.first and near not in self.shown and \
                    (edge is None or (i < edge if newest_top else i > edge)):
                new = True  # 켰을 때 보이던(번역하지 않은) 글을 다시 올린 것 — 한 번은 번역(怒焰来T 4=1 을 수십 번 올려도 안 보이던 문제)
            if not new:
                if sim < 1.0:  # OCR 이 흔들려 비슷하게 읽힌 같은 메시지 — 중복 판정이 맞는지 볼 수 있게
                    events.append(("dup", {"body": m["body"], "near": near, "sim": round(sim, 3), "lang": m["lang"]}))
                continue
            self.msg_no += 1
            m["id"] = f"{self.trace.sid}-{self.msg_no}"
            if self.first:
                decision = "skip_first"  # 켰을 때 이미 보이던 줄은 번역하지 않는다
            elif edge is not None and (i > edge if newest_top else i < edge):
                decision = "skip_old"  # 앞 화면에도 있던 메시지보다 옛 쪽
            elif m["lang"] == "ko":
                decision = "skip_ko"
            elif len(re.sub(r"\W", "", m["body"])) < 2:
                decision = "skip_short"
            elif is_junk(m["body"]):
                decision = "skip_junk"
            elif not m["name"] and (len(re.sub(r"\W", "", m["body"])) < 8 or m["rows"][0]["text"].lstrip()[:1] in "[【〔("):
                decision = "skip_noname"  # 머리를 못 읽은 줄 — 깨진 머리([ 6 ，瞓丿)를 번역하지 않게
            else:
                decision = "queued"
            if decision in ("queued", "skip_first"):  # 켰을 때 보이던 줄도 되풀이 세기에는 넣는다
                ad = self.ads.check(m["name"], m["body"])
                m["kind"], m["ad"] = ad["kind"], ad
                if decision == "queued" and ad["kind"] == "ad" and self.cfg.get("ad_filter", "fold") != "show":
                    decision = "ad_" + self.cfg.get("ad_filter", "fold")  # 번역하지 않는다
                    self.log_item(m, "")
                    if decision == "ad_fold":
                        self.events.put(("add", m))
            if decision == "queued" or decision.startswith("ad_"):
                self.shown.update((m["_key"], dkey(m["body"])))
            if decision == "queued":
                m["t_enq"] = time.monotonic()
                queued.append(m)
            events.append(("msg", {"id": m["id"], "ch": m["ch"], "name": m["name"], "lang": m["lang"], "body": m["body"],
                                   "decision": decision, "near_sim": round(sim, 3), "kind": m.get("kind"),
                                   "ad": m.get("ad"),
                                   "rows": [{"lang": x["lang"], "x": x["x"], "y": x["y"], "feat": x["feat"], "cand": x["cand"]}
                                            for x in m["rows"]]}))
        for o in orphans:  # 머리를 못 알아본 줄 — 같은 글은 한 번만
            k = norm(o["text"])
            if k and k not in self.orphans_seen:
                self.orphans_seen.append(k)
                events.append(("orphan", {"text": o["text"], "lang": o["lang"], "x": o["x"], "y": o["y"], "cand": o["cand"]}))
        frame_png = None
        if queued and self.cfg["keep_logs"] and time.time() - self.last_save >= self.FRAME_SAVE_EVERY:
            day_dir = paths.LOGS / "frames" / time.strftime("%Y%m%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            if len(list(day_dir.glob("*.png"))) < self.FRAME_SAVE_MAX:
                path = day_dir / f"{time.strftime('%H%M%S')}_{self.trace.sid}_{self.frame_no}.png"
                if self.ocr.save_last(path):
                    frame_png = str(path.relative_to(paths.DATA))
                    self.last_save = time.time()
        self.trace.write("frame", no=self.frame_no, ocr_ms=r.get("ms"), diff=r.get("diff"),
                         loop_ms=int((time.monotonic() - t0) * 1000),
                         rows=len(rows), msgs=len(msgs), queued=len(queued), orphans=len(orphans), png=frame_png)
        for ev, data in events:
            self.trace.write(ev, frame=self.frame_no, **data)
        for m in queued:
            self.jobs.put(m)
        self.first = False

    def capture_loop(self):
        last_stats = time.monotonic()
        while self.running:
            t0 = time.monotonic()
            if t0 - last_stats >= 60:  # 1분마다: 읽은 횟수·바뀐 횟수·밀린 번역
                self.trace.write("stats", frames=self.stats["frames"], changed=self.stats["changed"],
                                 translated=self.stats["translated"], backlog=self.jobs.qsize(), cache=len(self.cache))
                last_stats = t0
            try:
                r = self.ocr.read(self.region)
                self.stats["frames"] += 1
                if not r.get("same") and "lines" in r:
                    self.process_frame(r, t0)
                elif r.get("error"):
                    self.events.put(("status", "OCR 오류: " + r["error"]))
                    self.trace.write("error", where="ocr", msg=r["error"])
            except Exception as e:
                self.events.put(("status", f"읽기 오류: {e}"))
                self.trace.write("error", where="capture", msg=f"{type(e).__name__}: {e}")
                time.sleep(2)
            time.sleep(max(0.0, INTERVAL - (time.monotonic() - t0)))

    def translate_loop(self):
        while self.running:
            m = self.jobs.get()
            self.events.put(("add", m))
            body_key = norm(m["body"])
            t0 = time.monotonic()
            queue_wait = t0 - m.get("t_enq", t0)
            error = None
            if body_key in self.cache:
                m["ko"], sec, cached = self.cache[body_key], 0.0, True
            else:
                try:
                    m["ko"], sec = incoming.translate(m["body"], chinese=m["lang"] == "zh" or bool(CJK.search(m["name"])))
                except Exception as e:
                    m["ko"], sec, error = f"(번역 실패: {type(e).__name__})", 0.0, f"{type(e).__name__}: {e}"
                self.cache[body_key] = m["ko"]
                cached = False
                self.stats["translated"] += 1
                self.stats["tr_s"] = sec
            m["wait_s"] = round(time.monotonic() - t0, 2)
            self.trace.write("tr", id=m.get("id"), lang=m["lang"], body=m["body"], ko=m["ko"], cached=cached,
                             queue_s=round(queue_wait, 2), llm_s=round(sec, 2), total_s=round(queue_wait + m["wait_s"], 2),
                             backlog=self.jobs.qsize(), error=error)
            self.log_item(m, m["ko"], round(sec, 2), cached)
            self.events.put(("update", m))

    @staticmethod
    def log_item(m: dict, ko: str, sec: float = 0.0, cached: bool = False) -> None:
        """런처 피드가 읽는 번역 기록."""
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "ch": m["ch"], "name": m["name"], "lang": m["lang"],
                                "body": m["body"], "ko": ko, "sec": sec, "cached": cached, "kind": m.get("kind")},
                               ensure_ascii=False) + "\n")

    def poll(self):
        while not self.events.empty():
            kind, arg = self.events.get()
            if kind in ("add", "update"):
                if kind == "add":
                    self.overlay.add(arg)
                else:
                    self.overlay.render()
            elif kind == "status":  # 알림은 6초 보이고 다시 숫자로
                self.overlay.status.config(text=arg)
                self.note_until = time.monotonic() + 6
        s = self.stats
        now = time.monotonic()
        if s["frames"] and now >= self.note_until:
            self.overlay.status.config(text=f"{s['translated']} · {s['tr_s']:.1f}s"
                                            + (f" · +{self.jobs.qsize()}" if self.jobs.qsize() else ""))
        if now - self.last_status >= 2:  # 런처 대시보드가 읽는 상태 · 런처가 바꾼 설정/영역/명령
            self.last_status = now
            self.watch_launcher()
            try:
                paths.LIVE_STATUS.write_text(json.dumps({
                    "t": time.time(), "region": self.region, "frames": s["frames"], "changed": s["changed"],
                    "ocr_ms": s["ocr_ms"], "translated": s["translated"], "last_s": round(s["tr_s"], 2),
                    "backlog": self.jobs.qsize(), "model": incoming.MODEL}, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        self.root.after(300, self.poll)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.running = False
            self.ocr.close()
            try:
                paths.LIVE_STATUS.unlink()
            except OSError:
                pass


def main() -> int:
    import logging
    paths.ensure()
    from watt import housekeeping
    logging.basicConfig(level=logging.INFO, handlers=[housekeeping.file_handler("live.log")])
    logging.info("live start")
    threading.excepthook = lambda a: logging.critical("thread %s", a.thread and a.thread.name,
                                                      exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    try:
        Live().run()
        logging.info("live exit")
    except Exception:  # 콘솔 없이(pythonw) 돌 때도 원인이 남게
        import traceback
        paths.ensure()
        with (paths.LOGS / "live_error.log").open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + traceback.format_exc() + "\n")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
