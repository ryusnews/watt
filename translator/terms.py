"""와우 클래식(포에버) 용어 사전(wow_terms.json) — 보내기·받기가 같이 쓴다.

outgoing(text, lang): 한국어 글에 나온 용어 → 그 언어로 쓸 말(영어는 채팅 약어 우선: 통곡 → WC)
incoming(text):       외국어 글에 나온 용어 → 한국 클라이언트 이름(RFC·怒焰·Огненная пропасть → 성난불길 협곡)
긴 말부터 찾고 찾은 자리는 가려서, 흑마법사 안의 '마법사'·SM Lib 안의 'SM' 이 따로 잡히지 않게 한다.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CJK = re.compile(r"[一-鿿]")
# 대문자 약어는 채팅에서 소문자로도 쓴다(any uc sums) — 흔한 낱말·다른 뜻과 겹치는 것만 대소문자를 가린다
CASE_SENSITIVE = {"IF", "ST", "SW", "OG", "DM", "SM", "XR", "ARM", "GY", "WS", "BB", "T", "Q", "ML", "SR", "HR",
                  "MS", "SS", "LR", "DZ", "XD", "QS", "ZS", "FS"}
MAX_HINTS = 14


def _load() -> list[dict]:
    return json.loads((HERE / "wow_terms.json").read_text(encoding="utf-8"))["terms"]


TERMS = _load()


def _pattern(surface: str, zh_only: bool = False) -> re.Pattern:
    if surface.isascii():
        # 병음 약자(ms·fs)는 중국어가 섞인 글에서만 쓰므로 대소문자를 가리지 않는다.
        # 앞에 레벨이 붙어 쓰인다(20LR = 20레벨 사냥꾼) — 병음 약자만 앞 숫자를 허용
        flags = 0 if surface in CASE_SENSITIVE and not zh_only else re.I
        before = r"(?<![A-Za-z])" if zh_only else r"(?<![A-Za-z0-9])"
        plural = "(?:s|es)?" if not zh_only and len(surface) >= 3 and surface.isalpha() and surface.islower() else ""  # locks · hunters
        return re.compile(rf"{before}{re.escape(surface)}{plural}(?![A-Za-z])", flags)
    return re.compile(re.escape(surface), re.I)  # 한글·한자·키릴 — 포함 여부(키릴은 대소문자 무시)


def _find(text: str, pairs: list[tuple[str, dict]], zh_first: bool = False) -> list[tuple[str, dict]]:
    """긴 말부터, 찾은 자리는 가리면서. zh_first: 같은 길이면 병음 약자 먼저(중국어 글의 SM = 萨满)."""
    masked, hits, seen = text, [], set()
    for surface, entry in sorted(pairs, key=lambda p: (-len(p[0]), zh_first and not p[1].get("zh_only"))):
        m = _pattern(surface, bool(entry.get("zh_only"))).search(masked)
        if not m:
            continue
        masked = masked[:m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
        key = (text[m.start():m.end()], id(entry))
        if key not in seen:
            seen.add(key)
            hits.append((text[m.start():m.end()], entry))
    return hits


_OUT_PAIRS = [(s, t) for t in TERMS if not t.get("zh_only") for s in [t["ko"]] + t.get("ko_alias", [])]
_IN_PAIRS = [(s, t) for t in TERMS for s in [t["en"]] + t.get("en_abbr", []) + t.get("zh", []) + t.get("ru", [])
             if s and not s.endswith(")")]


def target_name(entry: dict, lang: str) -> str:
    if lang == "en":
        abbr = (entry.get("en_abbr") or [None])[0]
        return f"{abbr} ({entry['en']})" if abbr and abbr.lower() != entry["en"].lower() else entry["en"]
    if lang in ("zh", "ru") and entry.get(lang):
        return entry[lang][0]
    return entry["en"]


def outgoing(text: str, lang: str) -> dict[str, str]:
    return {s: target_name(t, lang) for s, t in _find(text, _OUT_PAIRS)[:MAX_HINTS]}


def incoming(text: str, chinese: bool = False) -> dict[str, str]:
    """chinese: 중국 사용자 글(한자가 없어도 병음 약자 먼저)."""
    has_cjk = chinese or bool(CJK.search(text))
    pairs = [(s, t) for s, t in _IN_PAIRS if has_cjk or not t.get("zh_only")]
    return {s: t["ko"] for s, t in _find(text, pairs, zh_first=has_cjk)[:MAX_HINTS]}
