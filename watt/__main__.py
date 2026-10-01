"""WATT 진입점.

WATT.exe                  런처(설정 마법사·대시보드)
WATT.exe --role live      통역 창(채팅 읽기 → 번역 → 게임 위 창)
WATT.exe --role input     보내기 입력창(Ctrl+Shift+K)
WATT.exe --role selftest  묶음(exe)이 제대로 됐는지 점검 → logs/selftest.json (창을 띄우지 않는다)
WATT.exe --role apply-delta --target <폴더> --stage <받아 둔 곳> --pid <n>   바뀐 파일 바꿔 끼우기(update\runner 에서)
WATT.exe --role apply-update --target <폴더> --pid <n>   옛 포터블 업데이트(0.1.3–0.1.7 이 부른다)
개발 중: python -m watt [--role ...]
"""
import argparse
import sys


def selftest() -> int:
    """exe 안에 필요한 것이 다 들어갔는지 — 모듈·데이터 파일·OCR·글꼴·tkinter·WebView2."""
    import json
    import time
    import traceback

    from watt import paths
    report, ok = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "frozen": paths.FROZEN, "portable": paths.PORTABLE,
                  "res": str(paths.RES), "data_dir": str(paths.DATA)}, True

    def check(name, fn):
        nonlocal ok
        try:
            report[name] = fn()
        except Exception:
            ok = False
            report[name] = "ERROR " + traceback.format_exc(limit=3)

    def modules():
        import numpy  # noqa: F401
        import webview  # noqa: F401
        from translator import incoming, live, outgoing, terms  # noqa: F401
        from watt import app, chat_region, system  # noqa: F401
        return {"terms": len(terms.TERMS), "model": incoming.MODEL}

    def data_files():
        need = [paths.UI / "index.html", paths.UI / "app.js", paths.UI / "style.css", paths.UI / "fonts" / "IBMPlexSansKR-Regular.ttf",
                paths.TERMS, paths.ADDON / "ChatFontCJK.toc"]
        missing = [str(p) for p in need if not p.exists()]
        if missing:
            raise FileNotFoundError(", ".join(missing))
        return "ok"

    def ocr():
        import numpy as np
        from watt import ocr as o
        eng = o.Ocr()
        res = eng.recognize(np.full((40, 120, 4), 255, np.uint8))
        return {"installed": o.installed(), "engines": list(res)}

    def fonts():
        import tkinter as tk
        from watt import tkstyle
        root = tk.Tk()
        root.withdraw()
        tkstyle.load_fonts(root)
        root.destroy()
        return {"sans": tkstyle.SANS, "mono": tkstyle.MONO}

    def ai():  # 받아 둔 AI 글자 인식 팩이 있으면 exe 에서 불러지는지(DLL 충돌 · 빠진 표준 모듈)
        import numpy as np
        from watt import aipack, ocr as o
        st = aipack.status()
        langs = [lg for lg in ("ko", "zh", "ru", "latin") if st["models"][lg]]
        if not (st["runtime"] and st["models"]["det"] and langs):
            return "없음"
        eng = o.Ocr()
        eng.set_ai(langs)
        if not eng.ai:
            raise RuntimeError(eng.ai_error)
        eng.ai.lines(np.zeros((64, 256, 4), np.uint8), 2)
        return {"langs": langs, "gpu": eng.ai.gpu}

    def webview2():
        import winreg
        key = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        return winreg.QueryValueEx(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key), "pv")[0]

    def api():
        from watt.app import Api
        a, out = Api(), {}
        for name in ("get_state", "get_live", "get_games", "get_terms"):
            t0 = time.perf_counter()
            r = getattr(a, name)()
            out[name] = f"{(time.perf_counter() - t0) * 1000:.0f}ms {type(r).__name__}"
        return out

    for name, fn in (("modules", modules), ("data", data_files), ("ocr", ocr), ("ai", ai), ("fonts", fonts), ("webview2", webview2),
                     ("api", api)):
        check(name, fn)
    report["ok"] = ok
    paths.ensure()
    (paths.LOGS / "selftest.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="WATT")
    ap.add_argument("--role", choices=["app", "live", "input", "selftest", "apply-update", "apply-delta"], default="app")
    ap.add_argument("--target")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--stage")
    ap.add_argument("--log")
    args, _ = ap.parse_known_args()
    if args.role == "apply-delta":
        from watt import update
        return update.apply_delta(args.target, args.stage, args.pid, args.log)
    if args.role == "apply-update":
        from watt import update
        return update.apply_portable(args.target, args.pid)
    if args.role == "selftest":
        return selftest()
    if args.role == "live":
        from translator import live
        return live.main()
    if args.role == "input":
        from translator import outgoing
        from watt import settings
        outgoing.App(settings.load()["model"]).run()
        return 0
    from watt import app
    return app.main()


if __name__ == "__main__":
    sys.exit(main())
