"""보내기 번역 — 한국어로 쓰면 영어(또는 중국어·러시아어·일본어)로 바꿔 클립보드에 넣는다. 게임에 자동 입력하지 않는다.

python -m watt --role input                       # 상주: Ctrl+Shift+K → 입력창 → Enter → 클립보드 → 게임에서 Ctrl+V
python -m translator.outgoing --test "문장" --to en   # 번역만 시험
"""
import argparse
import ctypes
import json
import queue
import re
import threading
import time
import tkinter as tk
from ctypes import wintypes

from watt import paths, settings
from watt import tkstyle as tks

from . import terms
from .llm import chat_json, preload

MODEL = "gemma4:12b"
MAX_LEN = 255  # WoW 채팅 한 줄 한도
LANGS = [("en", "영어", "English"), ("zh", "중국어", "Simplified Chinese"), ("ru", "러시아어", "Russian"),
         ("ja", "일본어", "Japanese")]
HISTORY = paths.LOGS / "outgoing.jsonl"
SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


def terms_for(text: str, lang: str) -> dict:
    """용어 사전(wow_terms.json): 한국어 채팅 말(통곡·성불·화심·흑마 …) → 그 언어로 쓸 말(영어는 약어 우선)."""
    return terms.outgoing(text, lang)


def system_prompt(lang: str, lang_name: str, terms: dict) -> str:
    s = (f"You translate a Korean World of Warcraft Classic chat message into {lang_name} for other players. "
         "Write it the way players actually type in game chat: short, casual, natural. "
         "Keep numbers, item links in [brackets] and player names unchanged; add [brackets] only where the message has them "
         "(in game chat brackets mean an item link). Translate every item and material name; "
         "do not drop words and do not add anything that is not in the message. ")
    if lang == "en":
        s += ("Recruiting others (구해요/구함/구인 + a role or dungeon) is 'LF <role>' or 'LFM'; 'LFG' only when the writer "
              "wants to join someone else's group. ")
    else:
        s += (f"Write the whole message in {lang_name}. Do not switch to English words or English chat abbreviations "
              f"(LF, LFG, SFK …) — write it the way {lang_name}-speaking players do. ")
    s += f"At most {MAX_LEN} characters. Put only the translated message in 'text'."
    if terms:
        s += (f" Game terms in this message (Korean chat word = what {lang_name} players write"
              + ("; use the abbreviation before the brackets, as players type it" if lang == "en" else "") + "): "
              + "; ".join(f"{k} = {v}" for k, v in terms.items()) + ".")
    return s


def translate(text: str, lang: str = "en", model: str = MODEL, log: bool = True) -> tuple[str, float]:
    lang_name = next(n for c, _, n in LANGS if c == lang)
    out, secs = chat_json(model, system_prompt(lang, lang_name, terms_for(text, lang)), text, SCHEMA)
    result = out.get("text", "").strip().strip('"').replace("\n", " ")[:MAX_LEN]
    if not log:  # 평가 등 — 사용자 기록에 섞지 않는다
        return result, secs
    HISTORY.parent.mkdir(exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "ko": text, "to": lang, "out": result,
                            "sec": round(secs, 2), "model": model}, ensure_ascii=False) + "\n")
    return result, secs


# ---- 설정 — 언어·방식은 런처 설정(settings.json)과 같이 쓰고, 입력창에서 Tab/F2 로 바꾸면 거기에 저장. 창 위치는 따로
MODES = {"clipboard": "클립보드에 복사", "paste": "채팅창에 붙여넣기"}


def load_config() -> dict:
    s = settings.load()
    cfg = {"lang": s["out_lang"], "mode": s["out_mode"]}
    try:
        cfg.update({k: v for k, v in json.loads(paths.INPUT_UI.read_text(encoding="utf-8")).items() if k == "pos"})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    settings.save({"out_lang": cfg["lang"], "out_mode": cfg["mode"]})
    if cfg.get("pos"):
        paths.ensure()
        paths.INPUT_UI.write_text(json.dumps({"pos": cfg["pos"]}), encoding="utf-8")


# ---- Windows: 단축키 · 창 전환 · 붙여넣기
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT, WM_HOTKEY = 0x0002, 0x0004, 0x4000, 0x0312
VK_K, VK_RETURN, VK_CONTROL, VK_V = 0x4B, 0x0D, 0x11, 0x56
GAME_EXES = {"wow.exe", "wowb.exe", "wowclassic.exe"}
HK_ANY, HK_GAME = 1, 2  # Ctrl+Shift+K(항상) · Ctrl+Enter(WoW 가 앞에 있을 때만)


def foreground_exe() -> tuple[int, str]:
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    name = ""
    if h:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            name = buf.value.rsplit("\\", 1)[-1].lower()
        kernel32.CloseHandle(h)
    return hwnd, name


def hotkey_thread(events: queue.Queue) -> None:
    """Ctrl+Enter 는 WoW 가 앞에 있을 때만 등록 — 다른 프로그램의 Ctrl+Enter 를 빼앗지 않는다."""
    if not user32.RegisterHotKey(None, HK_ANY, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_K):
        events.put(("error", "Ctrl+Shift+K 등록 실패"))
    game_hk = False
    msg = wintypes.MSG()
    while True:
        _, exe = foreground_exe()
        want = exe in GAME_EXES
        if want and not game_hk:
            game_hk = bool(user32.RegisterHotKey(None, HK_GAME, MOD_CONTROL | MOD_NOREPEAT, VK_RETURN))
        elif not want and game_hk:
            user32.UnregisterHotKey(None, HK_GAME)
            game_hk = False
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            if msg.message == WM_HOTKEY:
                user32.AllowSetForegroundWindow(-1)
                events.put(("show", user32.GetForegroundWindow()))
        time.sleep(0.05)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def send_ctrl_v() -> None:
    """열려 있는 WoW 채팅 입력칸에 붙여넣기만 한다(보내기 Enter 는 사람이)."""
    seq = [(VK_CONTROL, 0), (VK_V, 0), (VK_V, 2), (VK_CONTROL, 2)]  # 2 = KEYEVENTF_KEYUP
    arr = (INPUT * len(seq))(*[INPUT(type=1, ki=KEYBDINPUT(wVk=vk, dwFlags=fl)) for vk, fl in seq])
    user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))


def default_position() -> tuple[int, int]:
    """채팅 영역(live 가 찾아 둔 chat_region.json) 바로 위, 없으면 화면 왼쪽 아래 쪽."""
    try:
        r = json.loads(paths.REGION.read_text(encoding="utf-8"))
        return r["window"]["screen_x"] + r["chat"]["x"], r["window"]["screen_y"] + r["chat"]["y"] - 120
    except (OSError, ValueError, KeyError):
        return 60, 690


class App:
    def __init__(self, model: str):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (OSError, AttributeError):
            pass
        self.model, self.game_hwnd = model, None
        self.cfg = load_config()
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        # 런처(WATT)와 같은 색·글꼴 — 입력을 받는 창이라 테두리는 청록
        tks.load_fonts(self.root)
        self.win.configure(bg=tks.TEAL)
        body = tk.Frame(self.win, bg=tks.BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        top = tk.Frame(body, bg=tks.BAR, height=24)
        top.pack(fill="x")
        top.pack_propagate(False)
        logo = tks.logo(top, 13, tks.BAR)
        logo.pack(side="left", padx=(9, 8))
        self.label = tk.Label(top, text="KO →", fg=tks.FG2, bg=tks.BAR, font=(tks.MONO, 8))
        self.label.pack(side="left")
        self.lang_label = tk.Label(top, fg=tks.LANG["en"], bg=tks.BAR, font=(tks.MONO, 8, "bold"))
        self.lang_label.pack(side="left", padx=(4, 0))
        help_btn = tk.Label(top, text="?", fg=tks.FAINT, bg=tks.BAR, font=(tks.MONO, 9), cursor="hand2")
        help_btn.pack(side="right", padx=(4, 9))
        help_btn.bind("<Button-1>", self.toggle_help)
        self.mode_label = tk.Label(top, fg=tks.MUTED, bg=tks.BAR, font=(tks.SANS, 8))
        self.mode_label.pack(side="right", padx=4)
        self.entry = tk.Entry(body, width=48, font=(tks.SANS, 13), bg=tks.CTRL, fg=tks.FG, insertbackground=tks.TEAL,
                              relief="flat", bd=0, highlightthickness=0)
        self.entry.pack(fill="x", padx=8, pady=8, ipady=8, ipadx=10)
        row = tk.Frame(body, bg=tks.BG)
        row.pack(fill="x", padx=10, pady=(0, 9))
        self.result = tk.Label(row, fg=tks.OK, bg=tks.BG, font=(tks.SANS, 11), anchor="w", wraplength=520, justify="left")
        self.result.pack(side="left", fill="x", expand=True)
        self.note = tk.Label(row, fg=tks.FAINT, bg=tks.BG, font=(tks.MONO, 8))
        self.note.pack(side="right", anchor="n")
        self.hint = tk.Label(body, text="Enter 번역 · Tab 언어 · F2 넣는 곳 · Esc 닫기", fg=tks.FAINT, bg=tks.BG,
                             font=(tks.SANS, 8), anchor="w")
        self.hint_on = False
        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<Escape>", lambda e: self.hide())
        self.entry.bind("<Tab>", self.on_tab)
        self.entry.bind("<F2>", self.on_mode)
        for w in (top, logo, self.label, self.lang_label, self.mode_label, self.result, self.win):  # 윗줄·결과줄을 끌어 옮기기 — 위치는 저장
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._drag_end)
        self.label.config(cursor="fleur")
        self.events: queue.Queue = queue.Queue()
        threading.Thread(target=hotkey_thread, args=(self.events,), daemon=True).start()
        threading.Thread(target=preload, args=(model,), daemon=True).start()
        self.root.after(100, self.poll)

    def header(self) -> None:
        lang = self.cfg["lang"]
        self.lang_label.config(text=lang.upper(), fg=tks.LANG.get(lang, tks.FG))
        self.mode_label.config(text="클립보드" if self.cfg["mode"] == "clipboard" else "입력칸")

    def toggle_help(self, _e=None) -> None:
        self.hint_on = not self.hint_on
        if self.hint_on:
            self.hint.pack(fill="x", padx=10, pady=(0, 8))
        else:
            self.hint.pack_forget()
        self.entry.focus_set()

    def poll(self) -> None:
        while not self.events.empty():
            kind, arg = self.events.get()
            if kind == "show":
                self.game_hwnd = arg
                self.show()
            elif kind == "done":
                self.finish(*arg)
        self.root.after(50, self.poll)

    def _drag_start(self, e) -> None:
        self._dx, self._dy = e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y()

    def _drag(self, e) -> None:
        self.win.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, _e) -> None:
        self.cfg["pos"] = [self.win.winfo_x(), self.win.winfo_y()]
        save_config(self.cfg)
        self.entry.focus_set()

    def show(self) -> None:
        self.cfg = {**self.cfg, **{k: v for k, v in load_config().items() if k != "pos"}}  # 런처에서 바꾼 언어·방식
        self.model = settings.load()["model"]
        x, y = self.cfg.get("pos") or default_position()
        self.win.geometry(f"+{x}+{y}")
        self.header()
        self.result.config(text="")
        self.note.config(text="")
        self.entry.config(state="normal")
        self.entry.delete(0, "end")
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.entry.focus_set()
        user32.SetForegroundWindow(self.win.winfo_id())

    def hide(self) -> None:
        self.win.withdraw()
        if self.game_hwnd:
            user32.SetForegroundWindow(self.game_hwnd)

    def on_tab(self, _event) -> str:
        codes = [c for c, _, _ in LANGS]
        self.cfg["lang"] = codes[(codes.index(self.cfg["lang"]) + 1) % len(codes)]
        save_config(self.cfg)
        self.header()
        return "break"

    def on_mode(self, _event) -> str:
        self.cfg["mode"] = "paste" if self.cfg["mode"] == "clipboard" else "clipboard"
        save_config(self.cfg)
        self.header()
        return "break"

    def on_enter(self, _event) -> None:
        text = self.entry.get().strip()
        if not text:
            return self.hide()
        self.entry.config(state="disabled")
        self.result.config(text="…", fg=tks.MUTED)
        self.note.config(text="")
        lang = self.cfg["lang"]

        def work():
            try:
                out, secs = translate(text, lang, self.model)
                self.events.put(("done", (out, f"{secs:.1f}s")))
            except Exception as e:  # 번역 실패는 창에 보여 주고 끝낸다
                self.events.put(("done", ("", f"실패: {e}")))

        threading.Thread(target=work, daemon=True).start()

    def finish(self, out: str, note: str) -> None:
        if not out:
            self.result.config(text=note, fg="#E97272")
            self.entry.config(state="normal")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(out)
        self.root.update()
        if self.cfg["mode"] == "paste":
            self.hide()
            self.root.after(150, send_ctrl_v)  # WoW 가 다시 앞에 온 뒤 붙여넣기 — 보내기(Enter)는 사람이
            return
        self.result.config(text=out, fg=tks.OK)
        self.note.config(text=f"복사됨 {note}")
        self.root.after(1200, self.hide)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", help="번역만 시험할 한국어 문장")
    ap.add_argument("--to", default="en", choices=[c for c, _, _ in LANGS])
    ap.add_argument("--model", default=settings.load()["model"])
    args = ap.parse_args()
    if args.test:
        out, secs = translate(args.test, args.to, args.model)
        print(f"{out}  ({secs:.1f}s)")
        return 0
    App(args.model).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
