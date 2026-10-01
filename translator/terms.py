"""와우 클래식(포에버) 용어 사전(wow_terms.json) — 보내기·받기가 같이 쓴다.

사용자가 고친 것(user_terms.json: edit · add · hide)을 기본 사전 위에 덧씌운다. 업데이트로 기본 사전이 바뀌어도 고친 것은 남고,
파일이 바뀌면 다음 찾기에서 다시 읽는다(런처에서 저장하면 통역 창 · 입력창에 바로).

outgoing(text, lang): 한국어 글에 나온 용어 → 그 언어로 쓸 말(영어는 채팅 약어 우선: 통곡 → WC)
incoming(text):       외국어 글에 나온 용어 → 한국 클라이언트 이름(RFC·怒焰·Огненная пропасть → 성난불길 협곡)
긴 말부터 찾고 찾은 자리는 가려서, 흑마법사 안의 '마법사'·SM Lib 안의 'SM' 이 따로 잡히지 않게 한다.
"""
import json
import os
import re
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CJK = re.compile(r"[一-鿿]")
# 대문자 약어는 채팅에서 소문자로도 쓴다(any uc sums) — 흔한 낱말·다른 뜻과 겹치는 것만 대소문자를 가린다
CASE_SENSITIVE = {"IF", "ST", "SW", "OG", "DM", "SM", "XR", "ARM", "GY", "WS", "BB", "T", "Q", "ML", "SR", "HR",
                  "MS", "SS", "LR", "DZ", "XD", "QS", "ZS", "FS"}
MAX_HINTS = 14


FIELDS = ("type", "ko", "ko_alias", "en", "en_abbr", "zh", "ru", "zh_only", "verify")
TYPES = ("dungeon", "wing", "raid", "zone", "city", "class", "spec", "role", "chat", "item")


def _user_path() -> Path | None:
    try:
        from watt import paths
        return paths.USER_TERMS
    except Exception:  # 사전만 따로 쓸 때(eval)
        return None


def base_terms() -> list[dict]:
    return json.loads((HERE / "wow_terms.json").read_text(encoding="utf-8"))["terms"]


def user_terms() -> dict:
    p = _user_path()
    try:
        u = json.loads(p.read_text(encoding="utf-8")) if p else {}
    except (OSError, ValueError):
        u = {}
    return {"edit": u.get("edit", {}), "add": u.get("add", []), "hide": u.get("hide", [])}


def all_terms() -> list[dict]:
    """기본 + 고친 것. 각 항목에 origin: base · edited · added · hidden(숨긴 것도 — 화면에서 되살리기용)."""
    u = user_terms()
    out = []
    for t in base_terms():
        if t["id"] in u["hide"]:
            out.append({**t, **u["edit"].get(t["id"], {}), "origin": "hidden"})
        elif t["id"] in u["edit"]:
            out.append({**t, **u["edit"][t["id"]], "id": t["id"], "origin": "edited"})
        else:
            out.append({**t, "origin": "base"})
    out += [{**t, "origin": "added"} for t in u["add"]]
    return out


def _clean(entry: dict) -> dict:
    """화면에서 온 항목 — 알려진 칸만, 목록은 쉼표로 나눠 빈 것 빼기."""
    def lst(v):
        v = v.split(",") if isinstance(v, str) else (v or [])
        return [s.strip() for s in v if str(s).strip()][:20]
    e = {"type": entry.get("type") if entry.get("type") in TYPES else "chat", "ko": str(entry.get("ko", "")).strip()[:40],
         "ko_alias": lst(entry.get("ko_alias")), "en": str(entry.get("en", "")).strip()[:60], "en_abbr": lst(entry.get("en_abbr")),
         "zh": lst(entry.get("zh")), "ru": lst(entry.get("ru")), "zh_only": bool(entry.get("zh_only"))}
    # 확인이 필요한 한국 줄임말(실제로 쓰이는지 모름) — 사용자가 확인하면 빠진다. 줄임말에서 지운 것은 함께 빠진다
    e["verify"] = [s for s in lst(entry.get("verify")) if s in e["ko_alias"]]
    if not e["ko"]:
        raise ValueError("한국 이름이 필요합니다")
    if not (e["en"] or e["en_abbr"] or e["zh"] or e["ru"] or e["ko_alias"]):
        raise ValueError("다른 말이 하나는 있어야 합니다")
    return e


def _save_user(u: dict) -> None:
    p = _user_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(u, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def save_term(entry: dict) -> dict:
    """기본 항목이면 수정으로, 아니면 추가(새 id)."""
    e = _clean(entry)
    u = user_terms()
    base_ids = {t["id"] for t in base_terms()}
    tid = entry.get("id") or ""
    if tid in base_ids:
        u["edit"][tid] = e
        if tid in u["hide"]:
            u["hide"].remove(tid)
    else:
        tid = tid if any(a["id"] == tid for a in u["add"]) else f"u-{uuid.uuid4().hex[:8]}"
        u["add"] = [a for a in u["add"] if a["id"] != tid] + [{"id": tid, **e}]
    _save_user(u)
    reload()
    return {"id": tid}


def delete_term(tid: str) -> dict:
    """기본 항목은 숨기고(되살릴 수 있음), 추가한 것은 지운다."""
    u = user_terms()
    if tid.startswith("u-"):
        u["add"] = [a for a in u["add"] if a["id"] != tid]
    elif tid not in u["hide"]:
        u["hide"].append(tid)
    _save_user(u)
    reload()
    return {"ok": True}


def reset_term(tid: str) -> dict:
    """원래대로 — 수정 · 숨김을 지운다."""
    u = user_terms()
    u["edit"].pop(tid, None)
    u["hide"] = [h for h in u["hide"] if h != tid]
    _save_user(u)
    reload()
    return {"ok": True}


def _active() -> list[dict]:
    return [t for t in all_terms() if t["origin"] != "hidden"]


TERMS = _active()
VERSION = 0
_MTIME = None


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


def _pairs():
    out_p = [(s, t) for t in TERMS if not t.get("zh_only") for s in [t["ko"]] + t.get("ko_alias", []) if s]
    in_p = [(s, t) for t in TERMS for s in [t.get("en", "")] + t.get("en_abbr", []) + t.get("zh", []) + t.get("ru", [])
            if s and not s.endswith(")")]
    return out_p, in_p


_OUT_PAIRS, _IN_PAIRS = _pairs()


def reload() -> None:
    global TERMS, _OUT_PAIRS, _IN_PAIRS, VERSION
    TERMS = _active()
    _OUT_PAIRS, _IN_PAIRS = _pairs()
    VERSION += 1


def maybe_reload() -> None:
    """user_terms.json 이 바뀌었으면 다시 읽는다(stat 한 번 — 매 번역마다 불러도 가볍다)."""
    global _MTIME
    p = _user_path()
    try:
        m = p.stat().st_mtime if p else None
    except OSError:
        m = None
    if m != _MTIME:
        first = _MTIME is None and m is None
        _MTIME = m
        if not first:
            reload()


def target_name(entry: dict, lang: str) -> str:
    if lang == "en":
        abbr = (entry.get("en_abbr") or [None])[0]
        return f"{abbr} ({entry['en']})" if abbr and abbr.lower() != entry["en"].lower() else entry["en"]
    if lang in ("zh", "ru") and entry.get(lang):
        return entry[lang][0]
    return entry.get("en") or (entry.get("en_abbr") or [entry["ko"]])[0]


def outgoing(text: str, lang: str) -> dict[str, str]:
    maybe_reload()
    return {s: target_name(t, lang) for s, t in _find(text, _OUT_PAIRS)[:MAX_HINTS]}


def incoming(text: str, chinese: bool = False) -> dict[str, str]:
    """chinese: 중국 사용자 글(한자가 없어도 병음 약자 먼저)."""
    maybe_reload()
    has_cjk = chinese or bool(CJK.search(text))
    pairs = [(s, t) for s, t in _IN_PAIRS if has_cjk or not t.get("zh_only")]
    return {s: t["ko"] for s, t in _find(text, pairs, zh_first=has_cjk)[:MAX_HINTS]}
