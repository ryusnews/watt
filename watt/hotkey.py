"""단축키 글('Ctrl+Shift+K') ↔ RegisterHotKey 값. 입력창(보내기) 단축키를 사용자가 고른다."""
import ctypes
import re

MODS = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
DEFAULT = "Ctrl+Shift+K"
NAMED = {"space": 0x20, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
         "pause": 0x13, "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA, "'": 0xDE,
         ",": 0xBC, ".": 0xBE, "/": 0xBF}
# 게임 · 시스템이 쓰는 조합 — 빼앗으면 곤란하다(Ctrl+Enter 는 WoW 가 앞에 있을 때 입력창이 따로 쓴다)
RESERVED = {"Ctrl+C", "Ctrl+V", "Ctrl+X", "Ctrl+Z", "Ctrl+A", "Ctrl+Enter", "Alt+F4", "Alt+Tab", "Ctrl+Alt+Delete"}


def parse(text: str) -> tuple[int, int] | None:
    """'Ctrl+Shift+K' → (mods, vk). 조합키(Ctrl · Alt · Win) 없이 쓸 수 있는 건 F1–F24 뿐."""
    parts = [p.strip() for p in (text or "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        if p.lower() not in MODS:
            return None
        mods |= MODS[p.lower()]
    key = parts[-1].lower()
    if re.fullmatch(r"[a-z0-9]", key):
        vk = ord(key.upper())
    elif re.fullmatch(r"f([1-9]|1\d|2[0-4])", key):
        vk = 0x70 + int(key[1:]) - 1
    elif key in NAMED:
        vk = NAMED[key]
    else:
        return None
    if not mods & (MODS["ctrl"] | MODS["alt"] | MODS["win"]) and not 0x70 <= vk <= 0x87:
        return None  # 글자만(또는 Shift+글자)이면 타자를 칠 수 없다
    return mods, vk


def check(text: str) -> str | None:
    """쓸 수 없으면 까닭, 쓸 수 있으면 None — 잠깐 등록해 보고 바로 푼다(다른 프로그램이 쓰고 있으면 실패)."""
    if text in RESERVED:
        return "reserved"
    p = parse(text)
    if not p:
        return "invalid"
    user32 = ctypes.windll.user32
    if not user32.RegisterHotKey(None, 0xBF00, p[0] | MOD_NOREPEAT, p[1]):
        return "taken"
    user32.UnregisterHotKey(None, 0xBF00)
    return None
