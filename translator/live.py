"""WoW 실시간 통역(시제품) — 채팅창을 읽어 영어·중국어 메시지를 한국어로 번역해 게임 위 창에 띄운다.

python -m watt --role live           # 채팅 영역 자동 찾기 → 0.5초마다 읽기 → 번역 창
흐름: watt.chat_region(영역) → watt.ocr(바뀌었을 때만, en/ko/zh/ru 엔진) → 줄마다 엔진 고르기 → 줄바꿈 합치기
      → 새 메시지만 → 캐시 → gemma4 번역(incoming) → 창. 게임에 입력하지 않는다.
창: 끌어서 옮기기 · 오른쪽 클릭 → 영역 다시 찾기 / 종료.
"""
import ctypes
import difflib
import unicodedata
import json
import queue
import re
import logging
import threading
import time
import tkinter as tk
from collections import Counter, deque

import numpy as np
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


RADICAL_NOISE = re.compile(r"[丿彐凵匚卜乃乬巳丨亅冂刂廾乂囗乁冖勹匸亠]")


def is_junk(body: str, named: bool = False) -> bool:
    """OCR 잡음(*fifi + +, R-fi±T2, 4/ixFf#) — 번역하지 않는다. 글자(한글·한자·라틴·키릴)가 4자 미만이거나 기호·숫자가 35%↑."""
    chars = re.sub(r"\s", "", body)
    letters = len(re.findall(r"[A-Za-zЀ-ӿ一-鿿가-힣]", chars))
    # 이름이 읽힌 메시지의 짧은 대답(yes · thx · wow! · ye · PVP)은 진짜 말 — 영어권 채널에서 이런 말이 많이 빠졌다(2026-10-02)
    if letters < 4 and not (letters >= 2 and (CJK.search(chars) or named)):
        return True
    if len(RADICAL_NOISE.findall(chars)) >= 3:  # 한국어 채널 이름을 중국어 엔진이 부수로 읽은 조각('丿 H - 9 彐引 ]')
        return True
    # 흔한 문장 부호(~ ! ? . = 등)는 잡음으로 세지 않는다 — '怒焰来本地人 =2DPS~~~' 를 버렸다(2026-10-01)
    rest = re.sub(r"[A-Za-zЀ-ӿ一-鿿가-힣~!?.,=！？。，、…～]", "", chars)
    odd = len(re.sub(r"\d", "", rest)) + 0.5 * len(re.findall(r"\d", rest))  # 숫자는 반만('LF 2 dps 20+')
    return odd / max(1, len(chars)) >= 0.35


def _outside_links(body: str) -> str:
    return re.sub(r"\[[^\[\]]*\]", " ", body)


# 사람이 쓴 말이 아닌 줄 — 번역하지 않는다. PC방 기록에서 번역 249건 중 56건이 이런 줄이었다(CPU 로 한 건 4초, #138)
SYSTEM_NAMES = {"전리품"}  # 한국어 클라이언트의 '[전리품]: [아이템]' 채널
LOOT_HISTORY = re.compile(r"(?i)[lih|]{0,3}[li]oot\s*history")  # LootHistory 애드온 조각(HlootHistory · lHIootHistory …)
# 이름 없는 애드온 안내 — 'EllesmereUI CDM: …' · 'ForeverLibraryPins' 처럼 붙여 쓴 대문자 이름으로 시작(PC방 2026-10-05)
ADDON_LINE = re.compile(r"(?i)\bloaded\b|\btype /\w+|\bhas known incompatib|^\s*RestedXP\b|"
                        r"(?-i:^\s*(?:[A-Z][a-z]+){2,}[A-Z]*\b|^\s*[A-Z][a-z]+[A-Z]{2,}\b)")
SLASH_ONLY = re.compile(r"^\s*/\w{1,16}(?:\s+\S{1,16})?\s*$")  # '/rl' · '/w 이름' — 번역할 말이 없다('/리로드'로 옮겼다)


def link_only(body: str) -> bool:
    """[링크]만 있는 글 — 번역할 말이 없다(라틴 · 한글 링크는 번역해도 원문 그대로 둔다). 한자 · 키릴 링크는 옮길 것이 있어 뺀다."""
    links = re.findall(r"\[[^\[\]]*\]", body)
    return (bool(links) and not re.search(r"\w", _outside_links(body))
            and not any(CJK.search(x) or CYRILLIC.search(x) for x in links))


CHANNEL_NAME = re.compile(r"^\s*\d{1,2}\s*[.,]")  # '1. 공개 • 불모' — 채널 이름을 사람 이름으로 읽은 것(진짜 이름은 모른다)
# 한국어 엔진이 아닌 엔진이 한글을 닮은 한자 · 부수로 읽은 조각: 卜(ㅏ) 匚(ㄷ) 彐(ㅋ) 凵 巳 丨 + 'LI' · 'L |'(니) —
# '17 明 S 合 LI 匚卜，'(…합니다) · '闷 7d 里叫彐彐'(ㅋㅋ) · 이름 '匚 H 人卜昌'(내 PC 2026-10-06, #140)
HANZI_JAMO = re.compile(r"[卜匚彐凵巳丨鬯吲叱岂矧旨暑]|(?<![A-Za-z])L\s?[I|](?![A-Za-z])")  # 기록 셋의 진짜 중국어에는 2개↑ 없음


def korean_name(name: str) -> bool:
    """한글 이름(한글 2자↑ · 글자의 절반↑, 채널 이름 꼴은 아님)."""
    h = len(HANGUL.findall(name))
    return h >= 2 and h * 2 >= len(re.findall(r"\w", name)) and not CHANNEL_NAME.match(name)


def hangul_as_hanzi(text: str) -> bool:
    return len(HANZI_JAMO.findall(text)) >= 2


def korean_body(body: str) -> bool:
    """링크를 빼고 한글이 4자 이상이고 라틴보다 많다 — 한국어 글."""
    s = _outside_links(body)
    h, la = len(HANGUL.findall(s)), len(LATIN.findall(s))
    return (h >= 4 and h >= la) or (h >= 1 and la == 0)  # '넵' · '네' 같은 짧은 대답도


def garbled_ko(body: str) -> bool:
    """링크 밖에 한글 조각 몇 자 + 짧은 라틴 — 깨진 한국어 줄을 영어로 읽은 것('EH Al 여', 'g Ed 니다', 2026-10-01)."""
    s = re.sub(r"\d+\s*[가-힣]", " ", _outside_links(body))  # '2명' · '1분' 같은 수 단위는 빼고
    h, la = len(HANGUL.findall(s)), len(LATIN.findall(s))
    return 0 < h < 4 and la < 6


# 라틴 문자 줄의 언어 짐작 — 흔한 낱말로(한 화면 안에서 줄마다 영어 · 스페인어 · 독일어 …가 섞인다). 영어가 아니면 그 줄은
# 라틴 모델(악센트)로 읽은 것을 쓴다. 짧은 줄(낱말 2개 미만)은 영어로 둔다
STOPWORDS = {
    "en": "the and you for are is to of in it that this with have can lf lfg lfm wts wtb any anyone just get my me i",
    "es": "que de la el los las para por con una uno ya hay quien busca busco somos estamos nuestro gremio jugadores y en es quiero hacer ahora alguien tengo vamos",
    "de": "und der die das für mit ist nicht ich wir sucht suchen noch auch auf bei gilde spieler ein eine zu jemand hat haben heute",
    "fr": "le la les des pour est une avec pas qui nous vous sur dans guilde joueurs recrute cherche ce et quelqu soir je tu on",
    "pt": "que de para com uma não você voce nós nos estamos procurando guilda jogadores é e o os quer fazer agora comigo alguem tem",
    "it": "che di per con una non sono gilda giocatori cerchiamo il gli della",
}
_SW = {k: set(v.split()) for k, v in STOPWORDS.items()}


def guess_latin(text: str) -> str:
    """'en' · 'es' · 'de' · 'fr' · 'pt' · 'it' 중 흔한 낱말이 가장 많이 맞는 것(동점 · 짧으면 en)."""
    words = re.findall(r"[a-z]+", _plain(text))
    if len(words) < 2:
        return "en"
    score = {k: sum(w in s for w in words) for k, s in _SW.items()}
    best = max(score, key=lambda k: (score[k], k == "en"))
    return best if best != "en" and score[best] >= 2 and score[best] > score["en"] else "en"


def _plain(s: str) -> str:
    """악센트를 뗀 소문자(é → e)."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _accents(s: str) -> int:
    return sum(1 for ch in s if ch.isalpha() and not ch.isascii() and unicodedata.category(ch).startswith("L")
               and "LATIN" in unicodedata.name(ch, ""))


def _letters(text: str) -> int:
    return (len(HANGUL.findall(text)) + len(CJK.findall(text)) + len(LATIN.findall(text))
            + len(CYRILLIC.findall(text)))


def looks_chinese(text: str) -> bool:
    """한자가 글자의 25%↑([이름]·<길드> 빼고) — 키릴·라틴을 읽다 섞여 나온 한자 몇 개(月角 沩)는 걸러진다."""
    core = TAGS.sub(" ", text)
    han = len(CJK.findall(core))
    # 0.25: 중국 사용자는 병음 · 영어 약자를 섞는다(NY来法系DPS和N — 한자 4/13). 키릴 · 라틴을 읽다 나온 가짜 한자는 그보다 적다
    return han >= 2 and han >= 0.25 * _letters(core)


JAMO = re.compile(r"[ㄱ-ㅣ]")  # 자음 · 모음만(ㅋㅋ · ㄷㄷ · ㅠㅠ)


def looks_korean(text: str) -> bool:
    """한글 2자↑이고 글자의 절반↑([이름]·<길드> 빼고) — 한국어 엔진이 키릴·한자를 읽다 만든 가짜 한글(너, 뇌) 몇 개는 걸러진다.
    자음만 쓴 말(ㅋㅋ · ㄷㄷ)도 한글로 센다 — 안 세면 중국어 엔진이 읽은 닮은 한자(彐彐 · 匚匚)를 골라 중국어로 '번역'했다(2026-10-03)."""
    core = TAGS.sub(" ", text)
    hangul = len(HANGUL.findall(core)) + len(JAMO.findall(core))
    return hangul >= 2 and hangul >= 0.5 * (_letters(core) + len(JAMO.findall(core)))


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
HEADER = re.compile(r"^\s*(?:[\[〔(]?\s*(?P<ch>\d{1,2})\s*[\]〕)lIJj|]?\s*)?[\[〔(]\s*(?P<name>[^\[\]〔〕(（]{1,32}?)\s*[\]〕)lIJj]"
                    r"\s*(?P<kind>님\s?(?:의|에게)\s*\S{1,4}(?:\s\S{1,3})?|(?:[ßB]|Lel)?\s*9(?:\s*\|\s*9)?|says|yells|whispers)?\s*[:：.,;|]\s*(?P<body>.*)$")


# 줄 앞에 무엇이 붙든(기본 채팅 [1. 공개 - 오그리마] · 시간 표시 · 애드온) 마지막 [이름]: 을 머리로 —
# 왼쪽 여백에서 시작하는 줄에만 쓴다(들여쓴 뒷줄의 [아이템]: 을 머리로 오인하지 않게). #10 의 첫 단계
GENERIC = re.compile(r"^(?P<pre>.{0,60}?)[\[〔(]\s*(?P<name>[^\[\]〔〕(（]{1,32}?)\s*[\]〕)lJj1I|]"
                     r"\s*(?P<kind>님\s?(?:의|에게)\s*\S{1,4}(?:\s\S{1,3})?|(?:[ßB]|Lel)?\s*9(?:\s*\|\s*9)?|says|yells|whispers"
                     r"|[^\s:：\[\]()（]{1,4}(?:\s+[^\s:：\[\]()（]{1,4}){0,3})?\s*[:：]\s*(?P<body>.*)$")  # 끝: 깨진 '님의 외침'(Е91 91 Е)
PRE_CH = re.compile(r"(?:^|[\[〔(lI|])\s*(\d{1,2})\s*[.,\]〕)]")  # [1. 공개] · [6] — [12:30] 같은 시간은 아님


# 채팅 애드온의 시간 표시([23:10:02] · [23:10] · 23:10:02) — 머리를 읽기 전에 뗀다. 그대로 두면 '[23:' 을 이름으로,
# '10' 의 1 을 닫는 괄호로 읽어 이름이 '23:', 본문이 '02] [2] [이름]: …' 가 됐다(Prat, 2026-10-01)
TIMESTAMP = re.compile(r"^\s*[\[〔(]?\s*[\dOoIl]{1,2}\s*[:：.]\s*[\dOoIl]{2}(?:\s*[:：.]\s*[\dOoIl]{2})?\s*[\]〕)jlI|]?\s*")  # 0 을 O 로 읽기도([0O:11:03])


def parse_header(text: str, generic: bool = False) -> dict | None:
    """{ch, name, body} — [6] [이름]: 꼴, generic 이면 앞에 무엇이 붙은 [이름]: 도."""
    text = TIMESTAMP.sub("", text or "", count=1)
    m = HEADER.match(text or "")
    if not m and LEAD_JUNK.match(text):
        # 머리 앞 잡음 1–2자 — AI 한국어가 채팅창 왼쪽 가장자리까지 읽어 '쇠 [11 [이름]:' 이 되면 머리로 못 보고 앞 메시지
        # 뒷줄로 붙여 두 메시지를 이어 번역했다(0.1.75 모니터링 30분에 28번)
        text = LEAD_JUNK.sub(lambda j: "[" if j.group(0).lstrip().startswith(("[", "(", "〔")) else "", text, count=1)
        m = HEADER.match(text)
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


LEAD_JUNK = re.compile(r"^\s*(?:[^\s\[〔(]{1,2}\s+(?=[\[〔(]\s*\d{1,2}\s*[\]〕)lIJj|1]?\s*[\[〔(])"  # 쇠 [11 [이름]: · 71 [31 [이름]:
                       r"|[\[〔(]\s*[가-힣]\s*(?=\d{1,2}\s*[\]〕)lIJj|1]?\s*[\[〔(]))")  # [회 11 [이름]:
BRACKET = re.compile(r"\[[^\[\]]{2,60}\]")
HANGUL_LINK = re.compile(r"\[[^\[\]]*[가-힣][^\[\]]*\]")  # [늙은 불꽃눈] — 한국어 클라이언트가 보여 주는 링크


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


# 채팅 속 주소 — https://… · www.… · discord.gg/… · support.blizzard.com/article/… (점이 든 이름 + / 로 이어지는 것)
URL = re.compile(r"(?:https?://|www\.)[^\s\]\)>\"']+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|gg|io|tv|me|co|kr|ru|cn)/[^\s\]\)>\"']*")


def overlap(a: dict, b: dict) -> float:
    """두 영역(x, y, w, h)의 겹침 비율(IoU)."""
    ix = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union else 0.0


def find_region() -> dict:
    """채팅 영역(화면 좌표). 너무 작게 잡히면(채팅 줄이 한두 개뿐인 순간) 오류 — 지난 정상 영역을 쓴다."""
    return chat_region.screen_rect(chat_region.find_and_save())


# ---- 줄 고르기 · 메시지 만들기
HEAD_CUT = re.compile(r"^.{0,72}?[\]〕)lJ1I|]\s*(?:님(?:의|에게)\s*\S{1,4}(?:\s\S{1,3})?)?\s*[:：]\s*")


def body_of(text: str) -> str:
    """언어 판정용 본문 — 머리([6. 파티찾기] [이름]:)를 뺀다. 기본 채팅은 머리에 한국어 채널 이름이 붙고 이름은 라틴이라,
    머리째 판정하면 한국어 본문이 영어로, 영어 본문이 한국어로 갈린다(2026-10-01 모니터링)."""
    # 시간 표시를 먼저 뗀다 — 안 떼면 '[00:11:00]' 의 '1:' 을 머리 끝으로 보고 본문이 '00] [2] [이름]: …' 이 됐다(2026-10-02)
    return HEAD_CUT.sub("", TIMESTAMP.sub("", text or "", count=1), count=1)


def compose(k: dict | None, chosen: dict, lang: str, line_h: int) -> str:
    """머리([6. 파티찾기] [이름]:)는 한국어 엔진, 본문은 본문 언어 엔진 — 낱말 위치로 나눈다.
    한 엔진으로 줄째 읽으면 한국어 채널 이름 · 라틴 이름이 중국어 엔진에서 가짜 한자로 깨졌다(2026-10-01 모니터링)."""
    if not k or not k.get("w") or not chosen.get("w"):
        return chosen["t"]
    acc = ""
    ts = TIMESTAMP.match(k["t"] or "")
    skip_to = len(re.sub(r"\s", "", ts.group(0))) if ts else 0  # 시간 표시 낱말은 머리 찾기에서 뺀다(띄어쓰기는 세지 않음)
    seen = 0
    for i, (t, _, right) in enumerate(k["w"]):
        seen += len(re.sub(r"\s", "", t))
        if seen <= skip_to:
            continue
        acc = f"{acc} {t}" if acc else t
        m = HEAD_CUT.match(acc + " ")
        if m and m.end() >= len(acc) and parse_header(acc + " x", True):
            # 본문은 머리 끝(콜론) 오른쪽부터 — 한국어 엔진의 다음 낱말로 잡으면, 한국어 엔진이 한자를 못 읽은 줄에서
            # 본문 앞이 잘렸다('今晚冲30的有吗' → '30的有吗' 를 쓰레기로 버림, 2026-10-01)
            body = [w for w in chosen["w"] if w[1] >= right - line_h * 0.3]
            if not body:
                return chosen["t"]
            sep = "" if lang == "zh" else " "
            return acc + " " + sep.join(w[0] for w in body)
        if len(acc) > 72:
            break
    return chosen["t"]


def strip_grip(lines: dict, w: int, h: int, line_h: int) -> dict:
    """채팅창 오른쪽 아래 크기 조절 표시(◢ · //)를 글자로 읽은 조각을 뺀다 — '4' · '√' · '/' 로 읽혀 마지막 메시지에 붙었다
    ('ㅋㅋ 4', 2026-10-03). 영역 오른쪽 아래 구석(줄 높이 2개)에 있는 두 글자 이하 낱말만, 한글은 빼지 않는다."""
    x_min, y_min = w - 2.0 * line_h, h - 2.2 * line_h
    out = {}
    for k, ls in lines.items():
        keep = []
        for l in ls:
            if l["y"] + l.get("h", line_h) < y_min:
                keep.append(l)
                continue
            words = l.get("w") or []
            grip = [q for q in words if q[1] >= x_min and len(q[0].strip()) <= 2 and not HANGUL.search(q[0])]
            if not grip:
                if not words and l["x"] >= x_min and len(l["t"].strip()) <= 2 and not HANGUL.search(l["t"]):
                    continue  # 낱말 정보 없이 구석의 짧은 조각만
                keep.append(l)
                continue
            rest = [q for q in words if q not in grip]
            if not rest:
                continue
            keep.append({**l, "t": " ".join(q[0] for q in rest), "w": rest, "x": min(q[1] for q in rest)})
        out[k] = keep
    return out


def pick_lines(lines: dict, line_h: int) -> list[dict]:
    """엔진들의 같은 위치 줄 중 그럴듯한 것: 한글 → ko, 한자 → zh, 진짜 러시아어(키릴 60%↑) → ru, 그 밖 → en."""
    en, ko, zh, ru = (lines.get("en-US") or [], lines.get("ko") or [], lines.get("zh-Hans-CN") or [],
                      lines.get("ru-RU") or [])
    lat = lines.get("latin") or []  # AI 라틴(유럽어) 모델 — 영어 · 스페인어 · 독일어 … 본문(악센트)
    anchors = sorted(en + ko + zh + ru, key=lambda l: l["y"])
    rows: list[list[dict]] = []
    for l in anchors:  # 세로 위치로 같은 줄 묶기
        if rows and abs(rows[-1][0]["y"] - l["y"]) <= line_h * 0.5:
            rows[-1].append(l)
        else:
            rows.append([l])
    # 한 엔진만 읽은 줄이 위아래 줄과 같은 글이면 버린다 — AI 한국어가 아랫줄을 8px 위에 읽어 '쇠 [11 (이름l: …' 유령 줄이
    # 생기고, 머리 앞의 잡음 때문에 앞 메시지 뒷줄로 붙어 두 메시지를 이어 번역했다(2026-10-02)
    def _same(a, b):
        return difflib.SequenceMatcher(None, norm(a["t"])[-40:], norm(b["t"])[-40:]).ratio() >= 0.75
    rows = [row for i, row in enumerate(rows)
            if len(row) > 1 or not row[0].get("ai") or not any(abs(q["y"] - row[0]["y"]) <= line_h * 0.75 and _same(row[0], q)
                                       for j in (i - 1, i + 1) if 0 <= j < len(rows) and len(rows[j]) > 1 for q in rows[j])]
    out = []
    for row in rows:
        def by(src):
            """그 엔진의 이 줄 — 사이가 떠서 여러 토막으로 읽었으면 위치 순서대로 잇는다(뒷토막이 빠지던 문제)."""
            mine = sorted((l for l in row if any(l is s for s in src)), key=lambda l: l["y"])
            if not mine:
                return None
            # 같은 줄의 토막만 — 세로가 가깝고 가로로 겹치지 않는 것(아랫줄 토막까지 묶이면 두 메시지가 붙었다)
            first = mine[0]
            parts = [first]
            for l in mine[1:]:
                right = lambda q: max((w[2] for w in q.get("w", [])), default=q["x"])  # noqa: E731
                if abs(l["y"] - first["y"]) <= line_h * 0.35 and all(l["x"] >= right(q) - 4 or right(l) <= q["x"] + 4 for q in parts):
                    parts.append(l)
            parts.sort(key=lambda l: l["x"])
            if len(parts) == 1:
                return parts[0]
            return {"t": " ".join(p["t"] for p in parts), "x": parts[0]["x"], "y": min(p["y"] for p in parts),
                    "h": max(p.get("h", 0) for p in parts), "w": [w for p in parts for w in p.get("w", [])]}
        k, z, e, r = by(ko), by(zh), by(en), by(ru)
        # 퀘스트 · 아이템 링크는 보는 사람의 클라이언트 언어(한국어)로 보인다 — 보낸 사람 언어와 무관하므로 언어 판정에서 뺀다.
        # 'LFM [늙은 불꽃눈]' 을 한국어로 판정해 건너뛰거나, 중국어 엔진이 링크를 가짜 한자([旨吕罟])로 읽어 중국어로 갈랐다(2026-10-01)
        links = [s for s in HANGUL_LINK.findall(body_of(k["t"])) if k and
                 len(HANGUL.findall(s)) >= 0.5 * len(re.findall(r"[^\W\d_]", s))] if k else []  # 한글이 절반↑ — [니on'apyK] 는 아님

        def ft(c):
            b_ = body_of(c["t"])
            return re.sub(r"\[[^\[\]]*\]", " ", b_) if links else b_
        feat = {"hangul": len(HANGUL.findall(ft(k))) if k else 0, "ko": bool(k and looks_korean(ft(k))),
                "zh": bool(z and looks_chinese(ft(z))), "ru": bool(r and looks_russian(ft(r))),
                "en_garbled": bool(e and english_garbled(ft(e)))}
        ko_mixed = k and feat["hangul"] >= 4 and feat["hangul"] >= 0.3 * _letters(ft(k))  # 한국어 + 영어(로데론폐허 딜 / LFM DPS)
        if feat["ko"] or ko_mixed:
            chosen, lang = k, "ko"
        elif feat["zh"]:
            chosen, lang = z, "zh"
        elif feat["ru"] and (not e or feat["en_garbled"]):
            chosen, lang = r, "ru"  # 영어 엔진이 깨끗하게 읽은 줄은 영어로 둔다(짧은 영어가 가짜 키릴로 읽힐 때)
        else:
            chosen, lang = (e or k or z or r), "en"  # 러시아어 엔진만 찾은 줄도 있다
        la = next((q for q in lat if abs(q["y"] - chosen["y"]) <= line_h * 0.5), None) if lat and lang == "en" else None
        if la and z and z.get("ai"):
            # 라틴 모델과 중국어 모델이 같은 줄을 비슷하게 읽었으면 악센트가 적은 쪽 — 둘 다 영어 대문자에 없는 악센트를
            # 붙인다(THÉ · BUTTOÑ / STARTIÑĠ · mainteñance). 크게 다르면 중국어 모델 것(정답 표본에서 라틴만 쓰면 99.3 → 98.8%)
            za, lb = body_of(z["t"]), body_of(la["t"])
            if guess_latin(za) != "en" or guess_latin(lb) != "en":
                chosen = la  # 유럽어 줄 — 악센트(ñ · ü · é)는 라틴 모델이 바르게 읽는다
            elif difflib.SequenceMatcher(None, _plain(za), _plain(lb)).ratio() >= 0.9 and _accents(lb) < _accents(za):
                chosen = la
            elif not CJK.search(za) and len(LATIN.findall(za)) >= 0.7 * max(1, _letters(za)):
                chosen = z
        elif lang == "en" and z and z.get("ai"):
            zb = body_of(z["t"])
            if zb and not CJK.search(zb) and len(LATIN.findall(zb)) >= 0.7 * max(1, _letters(zb)):
                # 영어 본문은 AI 중국어 모델 것으로(머리는 그대로 한국어 엔진) — Windows 영어 엔진은 작은 글꼴에서 3배로 읽어도
                # g 를 q · a 로(lookinq … qroup, aot), AI 는 looking … group, got(2026-10-01). 줄째 바꾸면 머리가 깨져 메시지를 놓쳤다
                chosen = z
        text = compose(k, chosen, lang, line_h) if lang != "ko" else chosen["t"]
        text = CJK_GAP.sub("", text) if lang == "zh" else text
        if lang == "en":
            text = repair_segments(text, r and r["t"], z and z["t"])
        if links and lang != "ko":  # 링크 글자는 한국어 엔진이 읽은 것으로
            mine = BRACKET.findall(body_of(text))
            if len(mine) == len(links):
                for a_, b_ in zip(mine, links):
                    text = text.replace(a_, b_, 1)
            elif lang == "en":  # 영어 엔진이 링크를 빠뜨렸으면(LFM [ … ] → LFM) 한국어 엔진 줄째로(라틴도 읽는다)
                text = k["t"]
        xs = sorted(l["x"] for l in row)  # 가운데 값 — 한 엔진이 채팅창 왼쪽 아이콘을 글자로 읽어 x 가 튀어도(EllesmereUI)
        row = {"y": chosen["y"], "x": xs[(len(xs) - 1) // 2], "text": text, "lang": lang, "feat": feat,
               "cand": {"en": e and e["t"], "ko": k and k["t"], "zh": z and z["t"], "ru": r and r["t"]}}  # 추적용
        if not parse_header(text):  # 고른 엔진이 머리를 깨뜨렸으면 머리를 제대로 읽은 다른 엔진 것을 따로 둔다
            # 채널이 있거나, 외침 · 귓속말 · 일반 대화(채널 없음)처럼 한국어 엔진이 '님의 외침:' 표시만 읽고 이름을 놓친 줄
            # ('[收白菜出白菜]님의 외침:' → 한국어 엔진 '[님의외침:', 중국어 엔진 '[收白菜出白菜]召：', 2026-10-01)
            yell = re.search(r"님\s*의|외\s*침|귓\s*속|말\s*[:：]", text)
            row["alt_header"] = next((c["t"] for c in (e, k, r, z) if c and (h := parse_header(c["t"], True)) and
                                      (h["ch"] or (yell and h["name"].strip()))), None)
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


def _pure(name: str, rx: re.Pattern) -> tuple[int, float]:
    letters = re.findall(r"[^\W\d_]", name)
    n = len(rx.findall(name))
    return n, n / max(1, len(letters) + len(re.findall(r"\d", name)))


def best_name(row: dict, current: str) -> str:
    return re.sub(r"^\s*\d{1,2}\s*[:：]\s*", "", _best_name(row, current))  # Prat 의 레벨 표시 '20:Miracle Bolt'


def _best_name(row: dict, current: str) -> str:
    """이름은 그 문자를 제대로 읽는 엔진 것으로 — 한글은 한국어 엔진, 키릴은 러시아어 엔진, 한자는 중국어 엔진, 라틴은 영어 엔진.
    줄 엔진 하나로 읽으면 이름이 엔진마다 다르게 깨진다(Катя Мизулина → KaTß M 囝 3Y 月 囝 Ha, 2026-10-01)."""
    names = {}
    for lang, c in row["cand"].items():
        h = parse_header(c or "", True)
        if h:
            names[lang] = h["name"].strip()
    n, p = _pure(names.get("ko", ""), HANGUL)
    if n >= 2 and p >= 0.8:
        return names["ko"]
    n, p = _pure(names.get("ru", ""), CYRILLIC)
    if n >= 3 and p >= 0.8 and CYR_DISTINCT.search(names["ru"]):
        return names["ru"]
    n, p = _pure(names.get("zh", ""), CJK)
    if n >= 2 and p >= 0.8:
        return CJK_GAP.sub("", names["zh"])
    n, p = _pure(names.get("en", ""), LATIN)
    if n >= 3 and p >= 0.9:
        return names["en"]
    return current


def read_variant(eng, img: np.ndarray, k: float, line_h: int) -> list[dict]:
    """조각 하나를 k 배로 읽어 메시지로(좌표는 조각 기준 원래 크기)."""
    from watt import screen
    big = screen.upscale2(img) if k == 2 else screen.upscale(img, k)
    lines = {lg: [{a: b for a, b in l.items() if a in ("t", "x", "y", "h", "w", "ai")} for l in v]
             for lg, v in eng.read_lines(big, k).items()}
    rows = pick_lines(lines, line_h)
    return build_messages(rows, line_pitch(rows, line_h))


REFINE = ((2.5, 0), (3, 0), (2, 1))  # (배율, 위로 몇 픽셀 밀기) — 같은 픽셀이면 OCR 은 늘 같게 읽으므로 조금씩 바꿔 읽는다


def refine(m: dict, img: np.ndarray, line_h: int, eng) -> dict:
    """새 메시지의 줄만 잘라 배율 · 위치를 바꿔 몇 번 더 읽고 투표 — 이름은 가장 많이 나온 것, 본문은 다른 읽기와 가장 닮은 것.
    화면마다 附魔 · 咐魔, 埋伏十面 · 哩伏十面 처럼 번갈아 읽던 것을 한 번에 가린다(2026-10-01)."""
    ys = [r["y"] for r in m["rows"]]
    top, bot = max(0, min(ys) - int(line_h * 0.35)), min(img.shape[0], max(ys) + int(line_h * 1.25))
    if bot - top < line_h:
        return m
    readings = [m]
    for k, dy in REFINE:
        crop = img[max(0, top - dy):bot - dy]
        cands = [c for c in read_variant(eng, crop, k, line_h) if c["body"]]
        if cands:  # 조각 안의 메시지 중 원래 것과 가장 닮은 것
            best = max(cands, key=lambda c: difflib.SequenceMatcher(None, dkey(c["body"]), dkey(m["body"])).ratio())
            # 조각 안에서 메시지가 줄마다 갈리면 마지막 줄만 닮은 후보가 뽑혀 투표에서 앞 줄이 사라졌다
            # (세 줄짜리 중국어 '到拍賣場…' → '造公式有…' 만, 2026-10-01) — 원래 길이의 70% 이상만
            if difflib.SequenceMatcher(None, dkey(best["body"]), dkey(m["body"])).ratio() >= 0.5 and                     len(dkey(best["body"])) >= 0.7 * len(dkey(m["body"])):
                readings.append(best)
    if len(readings) < 3:
        return m

    def medoid(vals: list[str]) -> str:
        return max(vals, key=lambda v: sum(difflib.SequenceMatcher(None, dkey(v), dkey(o)).ratio() for o in vals))
    names = [r["name"] for r in readings if r["name"]]
    langs = Counter(r["lang"] for r in readings)
    out = dict(m)
    out["body"] = medoid([r["body"] for r in readings])
    if names:
        out["name"] = medoid(names)
    if langs.most_common(1)[0][1] > len(readings) / 2:
        out["lang"] = langs.most_common(1)[0][0]
    out["readings"] = len(readings)
    return out


# 채팅 입력칸 — 영역 맨 아래에 입력칸이 들어오면 '이름님에게 귓: (내가 치는 답장)' 을 앞 메시지 뒷줄로 붙여 이름이 본문에
# 섞이고 내 답장까지 번역했다(2026-10-03). 한국어 클라이언트의 입력칸 머리: 이름님에게 귓: · 말하기: · 외치기: · 길드: · 파티: …
EDIT_PROMPT = re.compile(r"님에게\s*(?:귓\S{0,3})?\s*[:：]|^\s*(?:말하기|외치기|길드|파티|공격대|공격대\s*경보|관리자|일반|대화)\s*[:：]")


def edit_box(r: dict) -> bool:
    """이 줄이 채팅 입력칸인가 — 엔진 중 하나라도 입력칸 머리로 읽었으면."""
    return any(EDIT_PROMPT.search(x or "") for x in [r.get("text")] + list((r.get("cand") or {}).values()))


def build_messages(rows: list[dict], line_h: int, orphans: list | None = None) -> list[dict]:
    """머리([채널] [이름]:)로 시작하는 줄 + 이어지는 줄바꿈 줄 = 메시지. 머리 없는 맨 위 조각·시스템 메시지는 버린다.
    orphans 를 주면 버린 줄(머리도 아니고 이어지는 줄도 아닌 것)을 담는다 — 머리 인식 실패를 찾는 데 쓴다."""
    msgs, cur, last_y = [], None, None
    # 들여쓰기는 채팅창 왼쪽 여백 기준 — 머리 줄 x 기준이면, OCR 이 [6] 을 빠뜨려 머리 줄이 오른쪽에서 시작할 때
    # 들여쓴 뒷줄이 떨어져 나간다(2026-09-30 frame 122: 3줄 광고가 첫 줄만 번역)
    # 왼쪽 여백 = 머리를 읽은 줄들의 왼쪽 끝. 그냥 가장 왼쪽 줄로 하면 채팅창 왼쪽 버튼(스피커 아이콘)을 읽은
    # 부스러기가 기준이 돼, 메시지 첫 줄을 앞 메시지의 뒷줄로 붙였다(2026-10-01, 채팅창을 키웠을 때)
    heads = [r["x"] for r in rows if parse_header(r["text"], True) or r.get("alt_header")]
    heads.sort()
    # 가운데 값 — 아이콘 부스러기로 x 가 튄 머리 줄 하나가 기준을 끌어가지 않게(가장 왼쪽 값이면 18 로 끌려가 시스템 줄이 붙었다)
    margin = heads[len(heads) // 2] if heads else min((r["x"] for r in rows), default=0)
    for i, r in enumerate(rows):
        # 채팅창 옆 아이콘(맨 아래로 ⌄ · 복사 · 설정)을 읽은 조각 — 메시지 왼쪽 여백보다 훨씬 왼쪽의 두 글자 이하.
        # 맨 아래로 버튼을 'K' · '4' · 'V' 로 읽어 마지막 메시지에 ' K' 가 붙었다(2026-10-03)
        if r["x"] < margin - line_h * 0.8 and len(re.sub(r"\s", "", r["text"])) <= 2 and not HANGUL.search(r["text"]):
            if orphans is not None:
                orphans.append(r)
            continue
        if edit_box(r):  # 채팅 입력칸 — 메시지가 아니다(내가 치는 글)
            cur = None
            if orphans is not None:
                orphans.append(r)
            continue
        # 왼쪽 여백에서 시작 = 새 메시지(뒷줄은 들여쓴다). 여백보다 왼쪽에서 시작해 '[이름]:' 머리가 읽히는 줄도 — 예전엔
        # 8px 왼쪽의 '[G] [이름]:'(한국어 엔진: '[이 [이름]:')을 들여쓴 뒷줄로 보고 앞 메시지에 이어 번역했다(PC방 2026-10-05).
        # 머리가 없으면 그대로 — OCR 이 뒷줄 상자를 왼쪽 끝부터 잡기도 한다(정답 표본 en_001112 의 주소 뒷줄 x=11)
        at_margin = abs(r["x"] - margin) < line_h * 0.4 or (r["x"] < margin and bool(parse_header(r["text"], True)))
        h = parse_header(r["text"], at_margin)
        body = h["body"] if h else None
        if not h and r.get("alt_header"):  # 머리는 다른 엔진 것으로, 본문은 고른 엔진 것(첫 ]: 뒤)으로
            h = parse_header(r["alt_header"], True)
            txt = TIMESTAMP.sub("", r["text"], count=1)  # 시간 표시의 ':' 를 머리 끝으로 보지 않게
            colon = re.search(r"[\]〕)lJ]\s*\S{0,6}\s*[:：]", txt[:72]) or re.search(r"[:：]", txt[:72])
            body = txt[colon.end():] if colon else h["body"]
        if h:
            ch = h["ch"]
            if not ch:  # 고른 엔진이 채널을 빠뜨렸으면 다른 엔진이 읽은 것으로(외침·귓말은 한국어 엔진이 잘 읽는다)
                ch = next((c["ch"] for c in (parse_header(x or "", at_margin) for x in r["cand"].values()) if c and c["ch"]),
                          None)
            cur = {"ch": ch or "?", "name": best_name(r, h["name"].strip()), "body": body.strip(), "lang": r["lang"], "y": r["y"],
                   "x": r["x"], "rows": [r]}
            msgs.append(cur)
            last_y = r["y"]
        elif cur and r["lang"] == "ko" and cur["lang"] != "ko" and cur["name"] and not parse_header(r["text"], True):
            # 외국어 메시지 아래의 한국어 줄 = 한국어 클라이언트의 시스템 · 이모트 줄 — 뒷줄로 붙이면 '… at tarag 오랜 시간
            # 아무런 행동도 하지 않아 자동으로 접속 종료 합니다' 를 이어 번역했다(2026-10-02). 보낸 사람의 언어는 한 메시지 안에서 같다
            cur = None
            if orphans is not None:
                orphans.append(r)
        elif cur and r["y"] - last_y <= line_h * 1.6 and r["x"] - cur["x"] >= line_h * 0.25:
            # 바로 위 메시지의 머리 줄보다 들여쓴 줄 = 그 메시지의 뒷부분 — 화면 전체의 여백(머리 줄 x 의 가운데 값)은 화면마다
            # 47–53px 로 흔들려, 10px 들여쓴 뒷줄을 여백에서 시작한 새 메시지로 갈랐다(여러 줄 메시지가 줄마다 번역, 2026-10-01)
            cur["body"] += ("" if r["lang"] == "zh" else " ") + r["text"].strip()
            if r["lang"] == "zh":
                cur["lang"] = "zh"
            cur["rows"].append(r)
            last_y = r["y"]
        elif at_margin and i > 0:  # 머리 모양을 못 읽었어도 여백에서 시작하면 새 메시지(이름 모름). 맨 윗줄은 잘린 조각·탭 이름
            txt = TIMESTAMP.sub("", r["text"], count=1)
            colon = re.search(r"[\]〕)lJ]\s*\S{0,6}\s*[:：]", txt[:72])
            cur = {"ch": "?", "name": "", "body": (txt[colon.end():] if colon else txt).strip(),
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
        settle_jamo(m)
    return msgs


# 한국어 엔진이 초성을 닮은 라틴 글자로 읽는다(Sarasa Gothic K 16px, 2026-10-05 그려서 잼): ㅇ → o · 0, ㄱ → 7, ㅌ → E,
# ㅍ → Ⅱ, ㄴ → L, ㄳ → μ · u, ㅊ → 츠. 그래서 ㅎㅇ · ㅇㅇ · ㄱㄱ 같은 초성 채팅이 영어(oo · 77 · EE)로 보여 번역됐다(#133)
JAMO_LOOK = str.maketrans({"o": "ㅇ", "O": "ㅇ", "0": "ㅇ", "7": "ㄱ", "E": "ㅌ", "Ⅱ": "ㅍ", "L": "ㄴ", "μ": "ㄳ"})
JAMO_BODY = re.compile(r"^[ㄱ-ㅣoO07EⅡLμ~!?.^;,\s]+$")
CHU = re.compile(r"츠(?=[ㄱ-ㅎ]|$)")  # ㅊㅋ → 츠ㅋ — 초성 옆의 츠는 ㅊ


def settle_jamo(m: dict) -> None:
    """한 줄짜리 짧은 메시지가 한국어 엔진으로 초성(+ 초성을 닮은 글자)뿐이면 한국어 초성 채팅으로 — 닮은 글자는 초성으로."""
    if m["lang"] == "ko":
        if JAMO.search(m["body"]) and len(m["body"]) <= 12:
            body = m["body"].translate(JAMO_LOOK) if JAMO_BODY.match(m["body"]) else m["body"]  # ㅈㅈo → ㅈㅈㅇ
            m["body"] = CHU.sub("ㅊ", body)
        return
    if len(m["rows"]) != 1:
        return
    ko = m["rows"][0].get("cand", {}).get("ko")
    if not ko:
        return
    h = parse_header(ko, True)
    body = (h["body"] if h else ko).strip()
    if body == "u":  # ㄳ 한 글자를 u 로 — 영어 'u' 한 글자만 보낸 메시지는 번역할 것도 없다
        body = "μ"
    core = re.sub(r"[~!?.^;,\s]", "", body)
    if not core or len(core) > 8 or not JAMO_BODY.match(body):
        return
    if not JAMO.search(core) and len(core) > 4:  # 초성이 하나도 없으면 짧은 것만(oo · 77 · EE)
        return
    m["lang"], m["body"] = "ko", CHU.sub("ㅊ", body.translate(JAMO_LOOK))


def _letters_outside_links(rows: list[dict]) -> Counter:
    """줄마다 [ ] 밖 글자 수 — 링크가 다음 줄로 넘어가도([塞 / 克隆尼亚]) 이어서 본다."""
    out, inside = Counter(), False
    for r in rows:
        n = 0
        for ch in r["text"]:
            if ch == "[":
                inside = True
            elif ch == "]":
                inside = False
            elif not inside and ch.isalpha():
                n += 1
        out[r["lang"]] += n
    return out


def settle_language(m: dict) -> None:
    """메시지 언어를 줄 전체로 정한다(첫 줄만 보지 않는다). 줄바꿈된 러시아어·우크라이나어가 첫 줄만 영어로 읽혀
    메시지 전체가 영어로 번역되던 문제(2026-09-30 분석). 러시아어로 정해지면 영어로 고른 줄은 러시아어 엔진 결과로 바꾼다."""
    weight = Counter()
    for r in m["rows"]:
        weight[r["lang"]] += len(r["text"])
    outside = _letters_outside_links(m["rows"])
    if sum(outside.values()):  # [링크] · [채널] [이름] 밖의 글자로 — 영어 문장 끝 중국어 퀘스트 링크 하나로 zh 가 되던 것(#115)
        weight = outside
    foreign = [(w, lang) for lang, w in weight.items() if lang in ("ru", "zh") and w]
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
        self.at: dict[str, float] = {}  # 처음 본 시각 — 다시 올린 글인지 볼 때

    def check(self, key: str, ignore: tuple = ()) -> tuple[bool, float, str]:
        """(새것인가, 가장 비슷한 것과의 유사도, 그 키). 새것이면 기억한다. ignore: 비교에서 뺄 키(같은 메시지의 처음 읽기)."""
        best, best_key = 0.0, ""
        for k in self.keys:
            if k in ignore:
                continue
            ratio = 1.0 if k == key else difflib.SequenceMatcher(None, k, key).ratio()
            if ratio > best:
                best, best_key = ratio, k
            if ratio >= self.THRESHOLD:
                return False, ratio, k
        if len(self.keys) == self.keys.maxlen:
            self.at.pop(self.keys[0], None)
        self.keys.append(key)
        self.at[key] = time.monotonic()
        return True, best, best_key

    def age(self, key: str) -> float:
        return time.monotonic() - self.at.get(key, time.monotonic())


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
        g = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", saved or "")
        if g and not screen.on_screen(int(g[3]), int(g[4]), int(g[1]), int(g[2])):
            saved = None  # 화면 밖(예: 684x279+-346+-636) — 채팅창 위로
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
                            height=self.show, state="disabled", cursor="xterm", spacing1=2, spacing3=4, padx=12, pady=6,
                            selectbackground=tks.CTRL, selectforeground=tks.FG, inactiveselectbackground=tks.CTRL, exportselection=False)
        self.text.pack(fill="both", expand=True)
        self.items: deque[dict] = deque(maxlen=self.show)
        self.apply_font(size)
        for w in (self.win, self.status, bar, mark):
            w.bind("<ButtonPress-1>", self._start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._save)
            w.bind("<Button-3>", self._menu)
        # 글을 긁어 복사(주소 같은 것은 칠 수 없다, 사용자) — 끌어 고르면 놓을 때 바로 클립보드에. 통역 창은 게임에서 포커스를
        # 가져오지 않아 Ctrl+C 를 기다리지 않는다. 고르는 동안은 새 메시지가 와도 다시 그리지 않는다(선택이 지워지지 않게)
        self.text.bind("<Button-3>", self._menu)
        self.text.bind("<ButtonPress-1>", self._sel_start, add="+")
        self.text.bind("<ButtonRelease-1>", self._sel_end, add="+")
        self.text.tag_configure("url", underline=True)
        self.text.tag_bind("url", "<Enter>", lambda e: self.text.configure(cursor="hand2"))
        self.text.tag_bind("url", "<Leave>", lambda e: self.text.configure(cursor="xterm"))
        self.text.tag_bind("url", "<Double-Button-1>", self._copy_url)
        self.selecting = False
        self.dirty = False
        grip = tk.Label(body, text="◢", fg="#3A4454", bg=tks.BG, cursor="size_nw_se", font=(tks.SANS, 8))
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<ButtonPress-1>", self._grip_start)
        grip.bind("<B1-Motion>", self._grip_drag)
        grip.bind("<ButtonRelease-1>", self._save)
        self.menu = tk.Menu(self.win, tearoff=0, bg=tks.PANEL, fg=tks.FG, activebackground=tks.CTRL,
                            activeforeground=tks.TEAL, bd=0, font=(tks.SANS, 9))
        self.on_refind = None
        self.on_translate = None  # 광고 아님으로 고친 접은 광고를 번역 줄에
        self.menu.add_command(label="채팅 영역 다시 찾기", command=lambda: self.on_refind and self.on_refind())
        self.menu.add_command(label="원문 보기", command=self._toggle_original)
        self.menu.add_separator()
        self.menu.add_command(label="통역 끄기", command=root.destroy)

    def _default_geometry(self, size: int) -> str:
        """채팅창 바로 위, 같은 폭."""
        r = self.region
        h = int(size * 4.2) * min(self.show, 6) + 40
        w = max(r["w"], 320)
        y = r["y"] - h - 8
        if not screen.on_screen(r["x"], y, w, h):  # 채팅창이 화면 위쪽이면 채팅창 아래로
            y = r["y"] + r["h"] + 8
        return f"{w}x{h}+{r['x']}+{y}"

    def keep_on_screen(self) -> None:
        """모니터 구성이 바뀌어 창이 화면 밖으로 나가면 채팅창 위로 되돌린다."""
        if not screen.on_screen(self.win.winfo_x(), self.win.winfo_y(), self.win.winfo_width(), self.win.winfo_height()):
            self.reset_position()

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
        it = None
        if e.widget is self.text:
            idx = self.text.index(f"@{e.x},{e.y}")
            n = next((int(t[2:]) for t in self.text.tag_names(idx) if t.startswith("it") and t[2:].isdigit()), None)
            if n is not None and n < len(self.items):
                it = list(self.items)[n]
        menu = self.menu
        if it:  # 메시지 위에서 — 복사, 그 사람을 바로 고친다(#13)
            menu = tk.Menu(self.win, tearoff=0, bg=tks.PANEL, fg=tks.FG, activebackground=tks.CTRL,
                           activeforeground=tks.TEAL, bd=0, font=(tks.SANS, 9))
            sel = self.text.tag_ranges("sel")
            if sel:
                picked = self.text.get(*sel[:2])
                menu.add_command(label="고른 글 복사", command=lambda: self.copy(picked))
            for u in dict.fromkeys(URL.findall(f"{it.get('body', '')} {it.get('ko') or ''}")):
                menu.add_command(label="주소 복사  " + (u if len(u) <= 34 else u[:33] + "…"), command=lambda u=u: self.copy(u))
            if it.get("ko") and not it.get("pass"):
                menu.add_command(label="번역 복사", command=lambda: self.copy(it["ko"]))
            menu.add_command(label="원문 복사", command=lambda: self.copy(it["body"]))
            menu.add_separator()
        if it and it.get("name"):
            name = it["name"]
            menu.add_command(label=f"{name} 숨기기", command=lambda: self.person(name, "hide"))
            if it.get("kind") == "ad":
                menu.add_command(label="광고 아님", command=lambda: self.person(name, "allow", it))
            else:
                menu.add_command(label="광고로", command=lambda: self.person(name, "block", it))
            menu.add_separator()
        if menu is not self.menu:
            for i in range(self.menu.index("end") + 1):
                if self.menu.type(i) == "separator":
                    menu.add_separator()
                else:
                    menu.add_command(label=self.menu.entrycget(i, "label"), command=self.menu.entrycget(i, "command"))
        menu.tk_popup(e.x_root, e.y_root)

    def person(self, name: str, what: str, it: dict | None = None) -> None:
        """숨기기 · 광고 아님 · 광고로 — 설정에 남기고(런처 번역 설정에서 되돌림) 지금 창에도 바로."""
        key = {"hide": "hidden_names", "allow": "ad_allow", "block": "ad_block"}[what]
        names = [n for n in self.cfg.get(key, []) if n != name] + [name]
        other = {"allow": "ad_block", "block": "ad_allow"}.get(what)
        changes = {key: names[-200:]}
        if other:
            changes[other] = [n for n in self.cfg.get(other, []) if n != name]
        self.cfg.update(settings.save(changes))
        if what == "hide":
            for x in [x for x in self.items if x.get("name") == name]:
                self.items.remove(x)
        elif it is not None:
            it["kind"] = "chat" if what == "allow" else "ad"
            if what == "allow" and not it.get("ko") and self.on_translate:
                self.on_translate(it)  # 접어 둔 광고 — 이제 번역
        self.render()

    def add(self, item: dict):
        if any(x is item for x in self.items):  # 광고 아님으로 고쳐 다시 번역하는 접은 광고 — 이미 있는 줄
            self.render()
            return
        self.items.append(item)
        self.render()

    def render(self):
        if self.selecting or self.text.tag_ranges("sel"):
            self.dirty = True  # 고른 글이 지워지지 않게 — 선택을 풀면(다른 곳을 누르면) 그린다
            return
        self.dirty = False
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for n, it in enumerate(self.items):
            if n:
                self.text.insert("end", "\n")
            start = self.text.index("end-1c")
            self._tag_item(n, start)
            lang = it["lang"] if it["lang"] in tks.LANG else "en"
            tag = it.get("tag") or lang  # 원문 언어(ES · DE …) — 색은 그 언어 것, 없으면 문자 종류 것
            self.text.insert("end", tag.upper(), "lang_" + (tag if tag in tks.LANG else lang))
            if it.get("kind") == "ad" and not it.get("ko"):  # 접은 광고 — 한 줄
                self.text.insert("end", f"  {it['name']}  ", "meta")
                self.text.insert("end", "광고", "ad")
                continue
            self.text.insert("end", f"  {it['name']}" + ("  ↻" if it.get("repeat") else "") + "\n", "meta")
            if it.get("ko"):
                self.text.insert("end", it["ko"], "ko")
                if self.cfg.get("show_original") and not it.get("pass"):
                    self.text.insert("end", "\n" + it["body"], "orig")
            else:
                self.text.insert("end", it["body"], "pending")
        self._tag_item(None, None)
        for m in URL.finditer(self.text.get("1.0", "end-1c")):  # 주소 — 두 번 누르면 통째로 복사
            self.text.tag_add("url", f"1.0+{m.start()}c", f"1.0+{m.end()}c")
        self.text.configure(state="disabled")
        self.text.see("end")

    # ---- 복사
    def _sel_start(self, _e=None):
        self.selecting = True
        if self.dirty and not self.text.tag_ranges("sel"):
            self.selecting = False
            self.render()
            self.selecting = True

    def _sel_end(self, _e=None):
        self.selecting = False
        ranges = self.text.tag_ranges("sel")
        if ranges:
            self.copy(self.text.get(*ranges[:2]))
            self.win.after(1500, self._release)  # 복사했으면 선택을 풀고 밀린 메시지를 그린다 — 고른 채로 두면 창이 멈춘다
        elif self.dirty:
            self.render()

    def _release(self) -> None:
        if self.selecting:
            return
        self.text.tag_remove("sel", "1.0", "end")
        if self.dirty:
            self.render()

    def _copy_url(self, e):
        idx = self.text.index(f"@{e.x},{e.y}")
        rng = self.text.tag_prevrange("url", idx + "+1c")
        if rng:
            self.text.tag_remove("sel", "1.0", "end")
            self.copy(self.text.get(*rng))
        return "break"

    def copy(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.win.clipboard_clear()
        self.win.clipboard_append(text)
        self.status.config(text="복사함: " + (text if len(text) <= 28 else text[:27] + "…"))
        self.note_until_copy = time.monotonic() + 3

    def _tag_item(self, n, start) -> None:
        """앞 메시지의 범위를 'it<번호>' 태그로 닫고 새 메시지를 연다."""
        prev = getattr(self, "_open", None)
        if prev:
            self.text.tag_add(f"it{prev[0]}", prev[1], "end-1c")
        self._open = (n, start) if n is not None else None


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
        self.overlay.on_translate = self.jobs.put  # jobs 를 만든 뒤에(0.1.70 은 앞에 두어 켤 때 멈췄다)
        self.seen = Seen()
        self.shown: set[str] = set()  # 통역 창에 올린 글(중복 키) — 안 올린 글을 다시 올리면 한 번은 번역
        self.prev_keys: list[tuple[str, int]] = []  # 바로 앞 화면의 (메시지, 위치) — 새 메시지가 나타나는 쪽 판단
        self.seen_body = Seen(30)  # 이름을 매번 다르게 읽어도(舜应盖特 · 舜廐 盖碍) 같은 글이면 한 번만
        self.ads = AdFilter()
        self.cache: dict[str, str] = {}
        self.started_mono = time.monotonic()
        self.started = time.time()  # 이번 통역을 켠 때 — 런처가 '이번 실행' 시간을 보인다
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
        threading.Thread(target=self.keep_warm, daemon=True).start()
        self.root.after(200, self.poll)

    def keep_warm(self) -> None:
        """'켤 때 모델 미리 올리기'를 통역을 켤 때 한다. 예전엔 모델을 바꿀 때만 올려 첫 번역이 28초 걸렸다(2026-10-04 14:19).
        얼마나 올려 둘지는 설정의 '자동 내리기'(llm.keep_alive) — 끄면 WATT 를 끌 때까지, 켜면 그만큼 번역이 없을 때 내린다."""
        from . import llm
        if self.cfg.get("preload", True):
            t0 = time.monotonic()
            llm.preload(incoming.MODEL)
            logging.info("warm %s %.1fs keep_alive=%s", incoming.MODEL, time.monotonic() - t0, llm.keep_alive_setting())

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

    def apply_ai(self, cfg: dict) -> None:
        """AI 글자 인식 언어가 바뀌면 뒤에서 모델을 띄운다(1–2초) — 그동안은 Windows OCR 만."""
        want = (tuple(cfg.get("ai_langs") or []), bool(cfg.get("ai_gpu", False)))
        if want == getattr(self, "ai_want", None):
            return
        self.ai_want = want

        def load():
            eng = self.ocr.ocr
            eng.set_ai(list(want[0]), want[1])
            self.trace.write("ai", langs=list(want[0]), gpu=bool(eng.ai and eng.ai.gpu), error=eng.ai_error or None)
        threading.Thread(target=load, daemon=True).start()

    DRIFT_AFTER = 15  # 초 — 켠 뒤 이만큼 지나 한 번(채팅 줄이 몇 개 보이고, 지금 영역으로 몇 장 읽은 뒤)

    def check_drift(self) -> None:
        """게임 안에서 채팅창을 옮기거나 키우면 예전 영역을 계속 읽어 잘린 줄 · 채팅 탭을 번역했다(2026-10-02 18:08, 다시 접속하며
        배치가 바뀜). 게임 창을 옮긴 것은 follow_game_window 가 따라간다. 채팅창을 옮기는 일은 드물어 늘 훑지 않고(사용자),
        **통역을 켤 때 한 번** 게임 화면 전체로 채팅 줄을 찾아 맞춘다. 켜 둔 채 옮겼으면 통역 창 메뉴의 '채팅 영역 다시 찾기'.
        사용자가 직접 지정한 영역(manual)은 건드리지 않는다."""
        if getattr(self, "drift_done", False):
            return
        if time.monotonic() - self.started_mono < self.DRIFT_AFTER:
            return
        self.drift_done = True
        good = chat_region.last_good()
        if good and good.get("manual"):
            return
        threading.Thread(target=self._drift_probe, daemon=True).start()

    def _drift_probe(self) -> None:
        """두 번(5초 사이) 찾아 같은 곳일 때만 — 잠깐 가려졌거나 다른 창이 떴을 수 있다."""
        found = []
        for i in range(2):
            if i:
                time.sleep(5)
            try:
                r = chat_region.find()
            except Exception:
                return
            if not chat_region.usable(r):
                return
            found.append((r, chat_region.screen_rect(r)))
        (r, cand), (_, again) = found
        cur = self.region
        if overlap(cand, again) < 0.8 or overlap(cand, cur) >= 0.7 or r["chat_lines_found"] <= getattr(self, "last_heads", 0):
            return  # 그대로 맞거나, 흔들리거나, 지금 영역도 그만큼 채팅 줄을 본다(다른 채팅창을 찾았을 수 있다)
        try:
            paths.ensure()
            text = json.dumps(r, ensure_ascii=False, indent=1)
            paths.REGION.write_text(text, encoding="utf-8")
            paths.REGION_GOOD.write_text(text, encoding="utf-8")
            self.seen_mtimes["region"] = paths.REGION_GOOD.stat().st_mtime  # 런처가 바꾼 것으로 다시 읽지 않게
        except OSError:
            pass
        self.region = cand
        self.trace.write("region", region=cand, by="drift", old=cur, lines=r["chat_lines_found"])
        self.events.put(("status", f"채팅창이 옮겨져 영역을 다시 맞췄습니다: {cand['w']}×{cand['h']}"))

    def watch_launcher(self):
        self.follow_game_window()
        self.check_drift()
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
            from . import llm
            llm.reset_device()  # 실행 장치(llm_device)를 바꿨을 수 있다
            self.apply_ai(cfg)
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

    def _cjk_echo(self, m: dict) -> bool:
        """방금 번역한 중국어 글을 줄이 올라간 다음 화면에서 한국어 엔진만 깨진 한자로 다시 읽은 것('兄弟們拍美行迭//A屳事',
        언어 en) — 글자가 많이 달라 0.8 기준을 못 넘었다(PC방 2026-10-05, 5건). 한자 글인데 zh 가 아니고, 12초 안에 이름이
        비슷한 사람의 zh 글이 있으면 같은 메시지로 본다. 같은 사람의 다른 중국어 글(zh)은 이 규칙에 걸리지 않는다."""
        if m["lang"] == "zh" or len(CJK.findall(m["body"])) < 2:
            return False
        name, body = dkey(m["name"]), dkey(m["body"])
        # 이름도 깨진다(大油边油边大 → 大;由逾 5由逾大 0.31, 说你行你就行 → i兇f尔行 f尔就行 0.4) — 기록 셋에서 이 기준(이름 0.3 ·
        # 본문 0.4)에 걸린 29건이 모두 메아리였다(哀嚎来T来治疗 → 哀ß豪采T采 治疔 를 '성난불길 협곡'으로 옮긴 것도, #140)
        return any(x[2] == "zh" and difflib.SequenceMatcher(None, name, x[3]).ratio() >= 0.3
                   and difflib.SequenceMatcher(None, body, x[4]).ratio() >= 0.4 for x in self.recent)

    FRAME_SAVE_EVERY = 3.0   # 초 — 화면 저장 간격
    FRAME_SAVE_MAX = 300     # 하루 최대 장수

    def process_frame(self, r: dict, t0: float) -> None:
        self.frame_no += 1
        self.stats["changed"] += 1
        self.stats["ocr_ms"] = r.get("ms", 0)
        lh = self.region["line_h"]
        rows = pick_lines(strip_grip(r["lines"], self.region["w"], self.region["h"], lh), lh)
        lh = line_pitch(rows, lh)
        orphans: list = []
        msgs = build_messages(rows, lh, orphans)
        self.last_heads = sum(1 for m in msgs if m["name"])  # 이 영역에서 머리를 읽은 메시지 수 — 영역 어긋남 판단에
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
        self.prev_keys_old, self.prev_keys = self.prev_keys, cur
        stay_set = set(stay)
        newest_top = top_mode
        edge = (min(stay) if newest_top else max(stay)) if stay else None
        for i, (m, new, sim, near) in enumerate(checked):
            # 다시 올린 글: 통역 창에 올린 적 없는 글이 이번 화면에 새로 나타남(앞 화면에 같은 자리 · 아래에 없음) +
            # 새 메시지 쪽이거나 처음 본 지 20초가 지남. 위치만 보면 놓치는 경우가 있었다(러시아어 광고를 44번 올려도 안 보임)
            if not new and not self.first and near not in self.shown and i not in stay_set and \
                    (edge is None or (i < edge if newest_top else i > edge)
                     or max(self.seen.age(near), self.seen_body.age(near)) > 20):
                new = True  # 켰을 때 보이던(번역하지 않은) 글을 다시 올린 것 — 한 번은 번역(怒焰来T 4=1 을 수십 번 올려도 안 보이던 문제)
            if not new and not self.first and near in self.shown and i not in stay_set and edge is not None and \
                    (i < edge if newest_top else i > edge) and max(self.seen.age(near), self.seen_body.age(near)) > 3 and \
                    not any(difflib.SequenceMatcher(None, cur[i][0], q).ratio() >= 0.8
                            and ((py <= cur[i][1] + 2) if top_mode else (py >= cur[i][1] - 2)) for q, py in self.prev_keys_old):
                new = True  # 같은 글을 다시 올림 — 통역 창도 채팅창처럼 한 번 더(번역은 저장된 것). 앞 화면 같은 자리 · 아래에
                m["repeat"] = True  # 비슷한 글이 있었으면 OCR 이 흔들려 다르게 읽은 같은 줄이라 뺀다
            if not new:
                if sim < 1.0:  # OCR 이 흔들려 비슷하게 읽힌 같은 메시지 — 중복 판정이 맞는지 볼 수 있게
                    events.append(("dup", {"body": m["body"], "near": near, "sim": round(sim, 3), "lang": m["lang"]}))
                continue
            self.msg_no += 1
            m["id"] = f"{self.trace.sid}-{self.msg_no}"
            if m["lang"] == "en" and korean_body(m["body"]):
                m["lang"] = "ko"  # 링크 뒤 한국어('[지식인의 부적]혹시 이거 퀘스트 어디서…')를 영어로 판정했다
            if m["lang"] != "ko" and link_only(m["body"]) and HANGUL_LINK.search(m["body"]):
                m["lang"] = "ko"  # '[이름]: [예언자의 단망토]' — 한국어 클라이언트 링크만, 한국 사람 글(#138)
            if m["name"] and m["name"] in self.cfg.get("hidden_names", []):
                decision = "hidden"  # 통역 창에서 숨긴 사람(#13)
            elif self.first:
                decision = "skip_first"  # 켰을 때 이미 보이던 줄은 번역하지 않는다
            elif edge is not None and (i > edge if newest_top else i < edge):
                decision = "skip_old"  # 앞 화면에도 있던 메시지보다 옛 쪽
            elif (m["name"] in SYSTEM_NAMES or LOOT_HISTORY.search(m["body"])
                  or (not m["name"] and ADDON_LINE.search(m["body"]))):
                decision = "skip_system"  # 전리품 알림 · 애드온 안내(#138)
            elif m["name"] and re.fullmatch(r"[\d\s.]+", m["name"]):
                decision = "skip_noname"  # 채널 퇴장 · 입장 알림('채널 퇴:[1.공개-오그리마]') — 이름이 숫자뿐이면 사람이 아니다
            elif not m["name"]:
                # 머리(이름)를 못 읽은 줄은 번역하지 않는다 — 기록 셋(내 PC 10-06 · PC방 10-05 두 번)에서 이름 없이 번역한 69줄이
                # 모두 시스템 · 애드온 안내 · 깨진 한국어였다(쓸모 있는 외국어 0, #140)
                decision = "skip_noname"
            elif korean_name(m["name"]) and m["lang"] != "ko":
                # 한글 이름 = 한국어 클라이언트를 쓰는 한국 사람 — 번역할 필요가 없다. 외국어처럼 보이는 것은 초성을 한자로 읽은
                # 것(古古 · 大大 · 丁丁 = ㄱㄱ · ㅊㅊ · ㅜㅜ)이거나 한/영을 안 바꾸고 친 한국어(djtjdhtpdu = 어서오세요, #140)
                if HANGUL.search(m["body"]):
                    m["lang"] = "ko"
                    decision = "pass_ko" if self.cfg.get("show_korean", True) else "skip_ko"
                else:
                    decision = "skip_ko"
            elif m["lang"] == "ko":
                decision = "pass_ko" if self.cfg.get("show_korean", True) and m["name"] else "skip_ko"
            elif len(re.sub(r"\W", "", m["body"])) < 2:
                decision = "skip_short"
            elif link_only(m["body"]) or SLASH_ONLY.match(m["body"]):
                decision = "pass_ko"  # 영어 링크만('[Deep Fathom Ring]') — 번역 없이 그대로 보인다(#138)
            elif is_junk(m["body"], bool(m["name"])) or garbled_ko(m["body"]) or hangul_as_hanzi(f"{m['name']} {m['body']}"):
                decision = "skip_junk"
            else:
                decision = "queued"
            first_body = m["body"]
            if decision == "queued" and not m.get("pass") and not m.get("repeat") and self.ocr.last_img is not None                     and not self.ocr.ocr.ai:  # AI 글자 인식을 켜면 다시 읽기는 끈다 — 정답 표본에서 오히려 낮았고(이름 98.0 → 97.0) CPU 를 세 배로 썼다
                # 번역할 메시지만 — 배율 · 위치를 바꿔 다시 읽고 투표
                try:
                    m.update(refine(m, self.ocr.last_img, lh, self.ocr.ocr))
                except Exception as e:  # 다시 읽기가 안 되면 처음 읽은 그대로
                    self.trace.write("error", where="refine", msg=f"{type(e).__name__}: {e}")
                if m.get("readings"):  # 다시 읽어 글이 바뀌었으면(깨진 글 → 한국어) 언어 · 중복을 다시 본다 —
                    # 한국어가 번역 모델로 가거나('해주것죠' → '해주겠죠'), 띄어쓰기만 다른 같은 글이 두 번 나왔다(2026-10-01)
                    k2 = dkey(f"{m['ch']}{m['name']}{m['body']}")
                    if k2 != m["_key"]:
                        b2 = dkey(m["body"])
                        dup = not self.seen.check(k2, (m["_key"],))[0] or                             (len(b2) >= 4 and not self.seen_body.check(b2, (dkey(first_body),))[0])
                        if dup and not m.get("repeat"):
                            decision = "dup_refined"
                    if decision == "queued" and m["lang"] == "ko":
                        decision = "pass_ko" if self.cfg.get("show_korean", True) and m["name"] else "skip_ko"
            if decision == "pass_ko":  # 한국어 — 번역 없이 그대로, 순서는 번역할 글과 같은 줄로
                decision, m["pass"] = "queued", True
            if decision == "queued":
                # 방금(12초 안) 올린 글과 거의 같으면 한 번만 — OCR 이 같은 메시지를 3줄 · 4줄(깨진 꼬리)로 번갈아 읽어
                # 몇 초 사이 두 번 번역하고 '다시 올린 글'로도 셌다(组队交流 광고, 2026-10-01). 진짜로 다시 올린 글은 12초 뒤
                now, bk = time.monotonic(), dkey(f"{m['name']}|{m['body']}")  # 이름까지 — 다른 사람의 같은 대답(네)은 따로
                self.recent = [x for x in getattr(self, "recent", []) if now - x[0] < 12]
                if any(difflib.SequenceMatcher(None, bk, x[1]).ratio() >= 0.8 for x in self.recent) or self._cjk_echo(m):
                    decision = "dup_recent"
                else:
                    self.recent.append((now, bk, m["lang"], dkey(m["name"]), dkey(m["body"])))
            if decision in ("queued", "skip_first"):  # 켰을 때 보이던 줄도 되풀이 세기에는 넣는다
                ad = self.ads.check(m["name"], m["body"])
                if m["name"] in self.cfg.get("ad_allow", []) and ad["kind"] == "ad":
                    ad = {**ad, "kind": "chat", "why": ad.get("why", []) + ["사용자: 광고 아님"]}
                elif m["name"] in self.cfg.get("ad_block", []):
                    ad = {**ad, "kind": "ad", "why": ad.get("why", []) + ["사용자: 광고"]}
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
                                   "pass": m.get("pass"), "repeat": m.get("repeat"),
                                   "readings": m.get("readings"),
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
        self.trace.write("frame", no=self.frame_no, ocr_ms=r.get("ms"), diff=r.get("diff"), how=r.get("how"),
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

    def _batch_ahead(self, first: dict) -> None:
        """밀려 있으면 줄 선 메시지를 몇 개 더 꺼내 한 번에 번역해 둔다(incoming.translate_batch) — 꺼낸 것은 캐시에 넣고 줄
        맨 앞으로 되돌려, 아래 한 건씩 도는 길이 캐시에서 바로 내보낸다. 실패하면 아무것도 하지 않는다(한 건씩)."""
        if self.jobs.qsize() < 1:
            return
        from . import llm
        if llm.device_options().get("num_gpu") == 0:
            # CPU 로 번역하면 묶음이 오히려 느리다 — 글자 쓰기가 대부분이라 묶어도 덜 들고, 다 끝나야 첫 번역이 보인다
            # (집 PC CPU 6스레드 E4B: 3건 묶음 6.1초 > 한 건씩 약 5초, PC방 14400F 에서 14.9초 — #131)
            return
        picked = [first]
        rest = []
        while len(picked) < incoming.BATCH_MAX:
            try:
                m = self.jobs.get_nowait()
            except queue.Empty:
                break
            if m.get("pass") or norm(m["body"]) in self.cache or any(norm(m["body"]) == norm(p["body"]) for p in picked):
                rest.append(m)
            else:
                picked.append(m)
        if len(picked) > 1:
            try:
                res, sec = incoming.translate_batch(
                    [(m["body"], m["lang"] == "zh" or bool(CJK.search(m["name"]))) for m in picked])
                for m, (ko, src) in zip(picked, res):
                    self.cache[norm(m["body"])] = (ko, src)
                self.trace.write("batch", n=len(picked), llm_s=round(sec, 2), backlog=self.jobs.qsize())
                self.batch_sec = sec / len(picked)
            except Exception as e:
                self.trace.write("batch", n=len(picked), error=f"{type(e).__name__}: {e}")
        # 꺼낸 것을 원래 순서대로 줄 앞에 되돌린다(first 는 부른 쪽이 들고 있다)
        back = picked[1:] + rest
        with self.jobs.mutex:
            for m in reversed(back):
                self.jobs.queue.appendleft(m)
                self.jobs.unfinished_tasks += 1
            self.jobs.not_empty.notify()

    def translate_loop(self):
        while self.running:
            m = self.jobs.get()
            if not m.get("pass") and norm(m["body"]) not in self.cache:
                self._batch_ahead(m)
            self.events.put(("add", m))
            body_key = norm(m["body"])
            t0 = time.monotonic()
            queue_wait = t0 - m.get("t_enq", t0)
            error = None
            if m.get("pass"):
                m["ko"], sec, cached = m["body"], 0.0, True
            elif body_key in self.cache:
                (m["ko"], src), sec, cached = self.cache[body_key], getattr(self, "batch_sec", 0.0), True
                self.batch_sec = 0.0
                m["tag"] = self.src_tag(m, src)
            else:
                src = ""
                try:
                    m["ko"], sec, src = incoming.translate_src(m["body"], chinese=m["lang"] == "zh" or bool(CJK.search(m["name"])))
                except Exception as e:
                    m["ko"], sec, error = f"(번역 실패: {type(e).__name__})", 0.0, f"{type(e).__name__}: {e}"
                if not error:
                    self.cache[body_key] = (m["ko"], src)
                m["tag"] = self.src_tag(m, src)
                cached = False
                self.stats["translated"] += 1
                self.stats["tr_s"] = sec
            if m["lang"] == "zh" and not m.get("pass") and not error and m.get("ko") and not HANGUL.search(m["ko"]):
                # 진짜 중국어는 번역하면 한글이 나온다 — 그대로 돌려받았으면 한국어 줄을 닮은 한자로 읽은 것('吲吲到卫 L 卜闷',
                # '彐彐 4', 2026-10-03). 통역 창 · 피드에 내지 않는다
                m["drop"] = True
            m["wait_s"] = round(time.monotonic() - t0, 2)
            self.trace.write("tr", id=m.get("id"), lang=m["lang"], tag=m.get("tag"), body=m["body"], ko=m["ko"], cached=cached,
                             queue_s=round(queue_wait, 2), llm_s=round(sec, 2), total_s=round(queue_wait + m["wait_s"], 2),
                             backlog=self.jobs.qsize(), error=error)
            if m.get("drop"):
                self.events.put(("drop", m))
                continue
            self.log_item(m, m["ko"], round(sec, 2), cached)
            self.events.put(("update", m))
            if not m.get("pass"):
                now = time.monotonic()
                self.recent_totals = [(t_, s_) for t_, s_ in getattr(self, "recent_totals", []) if now - t_ < 60]
                self.recent_totals.append((now, queue_wait + m["wait_s"]))

    def slow(self) -> float | None:
        """번역이 밀리나 — 최근 1분에 번역 6건↑이고 '표시까지' 가운데 값이 4초↑면 그 값(초). 바쁜 채널에서 큰 모델이
        한 문장씩 처리해 줄이 쌓였다(12b: 가운데 2.1초, 최대 12.9초 / E4B: 0.38초, 2026-10-02)."""
        now = time.monotonic()
        xs = sorted(s_ for t_, s_ in getattr(self, "recent_totals", []) if now - t_ < 60)
        if len(xs) < 6:
            return None
        mid = xs[len(xs) // 2]
        return round(mid, 1) if mid >= 4 else None

    @staticmethod
    def src_tag(m: dict, src: str) -> str | None:
        """화면에 붙일 원문 언어 — 라틴 문자 글은 모델이 본 언어(ES · DE · FR …), 키릴은 우크라이나어(UK)만 따로.
        짧은 글(글자 8개 미만)은 모델이 헷갈리므로(lol · ok) 붙이지 않는다."""
        if src not in incoming.SRC_TAGS or _letters(m["body"]) < 8:
            return None
        if m["lang"] == "en" and src != "uk" or m["lang"] == "ru" and src == "uk":
            return src
        return None

    @staticmethod
    def log_item(m: dict, ko: str, sec: float = 0.0, cached: bool = False) -> None:
        """런처 피드가 읽는 번역 기록."""
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "ch": m["ch"], "name": m["name"], "lang": m["lang"],
                                "tag": m.get("tag"), "body": m["body"], "ko": ko, "sec": sec, "cached": cached, "kind": m.get("kind")},
                               ensure_ascii=False) + "\n")

    def poll(self):
        while not self.events.empty():
            kind, arg = self.events.get()
            if kind == "drop":  # 번역해 보니 잡음 — 통역 창에서 뺀다
                for x in [x for x in self.overlay.items if x is arg]:
                    self.overlay.items.remove(x)
                self.overlay.render()
            elif kind in ("add", "update"):
                if kind == "add":
                    self.overlay.add(arg)
                else:
                    self.overlay.render()
            elif kind == "status":  # 알림은 6초 보이고 다시 숫자로
                self.overlay.status.config(text=arg)
                self.note_until = time.monotonic() + 6
        s = self.stats
        now = time.monotonic()
        if now - getattr(self, "screen_checked", 0) > 3:
            self.screen_checked = now
            self.overlay.keep_on_screen()
        if s["frames"] and now >= max(self.note_until, getattr(self.overlay, "note_until_copy", 0)):
            self.overlay.status.config(text=f"{s['translated']} · {s['tr_s']:.1f}s"
                                            + (f" · +{self.jobs.qsize()}" if self.jobs.qsize() else ""))
        if now - self.last_status >= 2:  # 런처 대시보드가 읽는 상태 · 런처가 바꾼 설정/영역/명령
            self.last_status = now
            self.watch_launcher()
            self.count_runtime(now)
            try:
                paths.LIVE_STATUS.write_text(json.dumps({
                    "t": time.time(), "region": self.region, "frames": s["frames"], "changed": s["changed"],
                    "ocr_ms": s["ocr_ms"], "translated": s["translated"], "last_s": round(s["tr_s"], 2),
                    "started": self.started,
                    "ai": sorted(self.ocr.ocr.ai.rec) if self.ocr.ocr.ai else [],
                    "ai_gpu": bool(self.ocr.ocr.ai and self.ocr.ocr.ai.gpu), "ai_error": self.ocr.ocr.ai_error or None,
                    "backlog": self.jobs.qsize(), "model": incoming.MODEL, "slow": self.slow()}, ensure_ascii=False),
                    encoding="utf-8")
            except OSError:
                pass
        self.root.after(300, self.poll)

    def count_runtime(self, now: float) -> None:
        """오늘 통역이 돈 시간을 runtime.json 에 더한다 — 홈의 '오늘 번역 · 표시까지'가 몇 시간 동안의 것인지 보이게.
        잠자기 등으로 크게 비면(10초↑) 더하지 않는다. 날짜가 바뀌면 새로."""
        last = getattr(self, "runtime_mark", None)
        self.runtime_mark = now
        if last is None or not 0 < now - last <= 10:
            return
        day = time.strftime("%Y-%m-%d")
        try:
            rt = json.loads(paths.RUNTIME.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rt = {}
        sec = (rt.get("sec", 0) if rt.get("day") == day else 0) + (now - last)
        try:
            paths.RUNTIME.write_text(json.dumps({"day": day, "sec": round(sec, 1)}), encoding="utf-8")
        except OSError:
            pass

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
