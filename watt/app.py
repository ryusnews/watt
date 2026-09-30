"""런처 — pywebview(WebView2) 창 + 화면(ui/)이 부르는 API.

화면 → Python: window.pywebview.api.<메서드>(...)  (Promise)
Python → 화면: WATT.onEvent({type, ...})             (오래 걸리는 일의 진행률·끝)
"""
import base64
import ctypes
import functools
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque

import webview

from . import APP_FULL, APP_NAME, VERSION, chat_region, ocr, paths, screen, settings, system

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
log = logging.getLogger("watt")


def _logged(fn):
    """화면이 부르는 API — 예외와 1초 넘는 호출을 logs/app.log 에."""
    @functools.wraps(fn)
    def wrap(*a, **kw):
        t0 = time.monotonic()
        try:
            return fn(*a, **kw)
        except Exception:
            log.exception("api %s 실패", fn.__name__)
            raise
        finally:
            dt = time.monotonic() - t0
            if dt > 1:
                log.info("api %s %.1fs", fn.__name__, dt)
    return wrap
LINKS = {"https://ai.google.dev/gemma/terms",  # Gemma 이용 약관
         "https://github.com/ryusnews/watt"}
MUTEX_NAME = "Local\\WATT_launcher_single_instance"


class Api:
    def __init__(self):
        self._window: webview.Window | None = None
        self._procs: dict[str, subprocess.Popen] = {}
        self._busy: dict[str, threading.Event] = {}  # 진행 중인 일 → 취소 신호
        self._sys = None
        self._games = (0.0, [])
        self._today = (None, None, 0, [])  # (파일 크기, 날짜, 오늘 건수, 최근 소요 초)
        self._cmd_n = 0

    # ---- 화면에 알리기
    def _emit(self, **ev) -> None:
        if self._window:
            try:
                self._window.evaluate_js(f"window.WATT && WATT.onEvent({json.dumps(ev, ensure_ascii=False)})")
            except Exception:  # 창이 닫히는 중
                pass

    def _task(self, name: str, fn) -> dict:
        """오래 걸리는 일을 뒤에서. 끝나면 {type:'done', task, ok, result|error}."""
        if name in self._busy:
            return {"started": False, "reason": "이미 진행 중"}
        cancel = threading.Event()
        self._busy[name] = cancel

        def run():
            try:
                result = fn(cancel)
                self._emit(type="done", task=name, ok=True, result=result)
            except Exception as e:
                self._emit(type="done", task=name, ok=False, error=str(e) or type(e).__name__)
            finally:
                self._busy.pop(name, None)

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    @_logged
    def cancel(self, name: str) -> bool:
        ev = self._busy.get(name)
        if ev:
            ev.set()
        return bool(ev)

    # ---- 상태
    @_logged
    def get_state(self, refresh: bool = False) -> dict:
        if self._sys is None or refresh:
            self._sys = system.system_info()
        cfg = settings.load()
        ol = system.ollama_status()
        oc = system.ocr_status()
        region = None
        try:
            r = json.loads(paths.REGION_GOOD.read_text(encoding="utf-8"))
            region = {"w": r["chat"]["w"], "h": r["chat"]["h"], "line_h": r["line_height"], "lines": r.get("chat_lines_found"),
                      "window": f"{r['window']['w']}×{r['window']['h']}"}
        except (OSError, ValueError, KeyError):
            pass
        game = screen.find_game_window()
        if game:
            game["flavor"] = system.FLAVORS.get(os.path.basename(os.path.dirname(game["path"])), game["exe"])
        vram = (self._sys.get("gpu") or {}).get("vram_gb", 0)
        model_ok = any(m["name"] == cfg["model"] for m in ol["models"])
        live = self._live_status()
        steps = {
            "system": "done",
            "ocr": "done" if not oc["missing"] else "todo",
            "ollama": "done" if ol["running"] else "todo",
            "model": "running" if "pull" in self._busy else ("done" if model_ok else "todo"),
            "addon": "done" if any(g["addon"] for g in self._game_list() if g["supported"]) else "todo",
            "region": "done" if region else "todo",
            "test": "done" if cfg.get("setup_done") else "todo",
        }
        return {
            "app": {"name": APP_NAME, "full": APP_FULL, "version": VERSION, "data": str(paths.DATA)},
            "settings": cfg, "system": self._sys, "ocr": oc, "ollama": ol,
            "models": [{**m, "installed": any(x["name"] == m["name"] for x in ol["models"]),
                        "recommended": m["name"] == system.recommend_model(vram)} for m in system.MODELS],
            "game": game and {"exe": game["exe"], "flavor": game["flavor"], "w": game["w"], "h": game["h"]},
            "region": region, "steps": steps,
            "running": {"live": self._alive("live"), "input": self._alive("input")},
            "live": live, "busy": list(self._busy),
        }

    @_logged
    def log(self, msg: str) -> None:
        log.warning("ui %s", str(msg)[:2000])

    def get_games(self) -> list[dict]:
        return self._game_list(force=True)

    def _game_list(self, force: bool = False) -> list[dict]:
        """게임 폴더 찾기는 드라이브를 훑으니 30초 동안 재사용."""
        if force or time.monotonic() - self._games[0] > 30:
            self._games = (time.monotonic(), system.game_installs(settings.load()["wow_roots"]))
        return self._games[1]

    @_logged
    def get_live(self) -> dict:
        """홈 화면이 1.5초마다 부르는 가벼운 상태."""
        feed = self.get_feed(30)
        today, secs = self._today_stats()
        return {"running": {"live": self._alive("live"), "input": self._alive("input")}, "live": self._live_status(),
                "feed": feed, "today": today, "avg_sec": round(sorted(secs)[len(secs) // 2], 1) if secs else None}  # 중앙값 — 모델 올리는 첫 번역 몇 초에 끌려가지 않게

    def _today_stats(self) -> tuple[int, list[float]]:
        """오늘 번역 건수와 최근 20건 번역 시간 — live.jsonl 이 바뀌었을 때만 다시 센다."""
        path = paths.LOGS / "live.jsonl"
        try:
            size = path.stat().st_size
        except OSError:
            return 0, []
        day = time.strftime("%Y-%m-%d")
        if self._today[0] == size and self._today[1] == day:
            return self._today[2], self._today[3]
        count, secs = 0, []
        with path.open("rb") as f:  # 뒤에서부터 오늘 날짜가 끝날 때까지
            f.seek(0, 2)
            pos, buf = f.tell(), b""
            while pos > 0:
                step = min(65536, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                lines = buf.split(b"\n")
                buf = lines[0]
                stop = False
                for line in reversed(lines[1:]):
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if not d.get("t", "").startswith(day):
                        stop = True
                        break
                    count += 1
                    if not d.get("cached") and len(secs) < 20:
                        secs.append(float(d.get("sec") or 0))
                if stop:
                    break
        self._today = (size, day, count, secs)
        return count, secs

    def _live_status(self) -> dict | None:
        try:
            st = json.loads(paths.LIVE_STATUS.read_text(encoding="utf-8"))
            return st if time.time() - st.get("t", 0) < 10 else None
        except (OSError, ValueError):
            return None

    @_logged
    def get_feed(self, n: int = 30) -> list[dict]:
        """최근 번역(새것이 앞)."""
        path = paths.LOGS / "live.jsonl"
        try:
            with path.open("rb") as f:  # 파일이 커져도 끝 64KB 만
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 65536))
                tail = deque(f.read().decode("utf-8", errors="replace").splitlines()[1:] or [], maxlen=n)
        except OSError:
            return []
        out = []
        for line in reversed(tail):
            try:
                d = json.loads(line)
                out.append({k: d.get(k) for k in ("t", "ch", "name", "lang", "body", "ko", "sec")})
            except ValueError:
                pass
        return out

    @_logged
    def get_terms(self) -> list[dict]:
        return json.loads(paths.TERMS.read_text(encoding="utf-8"))["terms"]

    # ---- 설정
    @_logged
    def save_settings(self, changes: dict) -> dict:
        return settings.save(changes)

    # ---- 환경 설정 단계
    @_logged
    def install_ocr(self, codes: list[str] | None = None) -> dict:
        codes = codes or system.ocr_status()["missing"]
        return self._task("ocr", lambda c: system.install_ocr(codes))

    @_logged
    def install_ollama(self) -> dict:
        def work(cancel):
            def prog(done, total):
                self._emit(type="progress", task="ollama", done=done, total=total, status="설치 파일 받는 중")
            return system.ollama_install(prog, cancel)
        return self._task("ollama", work)

    @_logged
    def start_ollama(self) -> dict:
        return self._task("ollama", lambda c: system.ollama_start())

    @_logged
    def pull_model(self, name: str) -> dict:
        def work(cancel):
            last = [0.0, 0, time.monotonic()]

            def prog(status, done, total):
                now = time.monotonic()
                if now - last[0] < 0.25 and done != total:
                    return
                speed = (done - last[1]) / max(1e-3, now - last[2]) if done >= last[1] else 0
                last[:] = [now, done, now]
                self._emit(type="progress", task="pull", model=name, status=status, done=done, total=total,
                           speed=speed)
            st = system.model_pull(name, prog, cancel)
            settings.save({"model": name})
            return st
        return self._task("pull", work)

    @_logged
    def use_model(self, name: str) -> dict:
        return settings.save({"model": name})

    @_logged
    def pick_game_folder(self) -> dict:
        """폴더 고르기 창 → WoW 폴더인지 확인 → 저장(wow_roots · game_dir)."""
        if not self._window:
            return {"ok": False, "error": "창이 없습니다"}
        cfg = settings.load()
        start = cfg.get("game_dir") or next(iter(cfg.get("wow_roots") or []), "")
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        if not res:
            return {"ok": False, "cancel": True}
        folder = res[0] if isinstance(res, (list, tuple)) else res
        try:
            r = system.resolve_folder(folder)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        roots = [x for x in cfg.get("wow_roots", []) if x.lower() != r["root"].lower()] + [r["root"]]
        settings.save({"wow_roots": roots, "game_dir": r["dir"]})
        return {"ok": True, "dir": r["dir"], "games": self._game_list(force=True)}

    @_logged
    def select_game(self, flavor_dir: str) -> dict:
        return settings.save({"game_dir": flavor_dir})

    @_logged
    def install_addon(self, flavor_dir: str) -> dict:
        res = system.install_addon(flavor_dir)
        settings.save({"game_dir": flavor_dir})
        return res

    @_logged
    def find_region(self) -> dict:
        def work(cancel):
            r = chat_region.find_and_save()
            rect = chat_region.screen_rect(r)
            img = screen.capture(rect["x"], rect["y"], rect["w"], rect["h"])
            return {"region": r, "preview": "data:image/png;base64," + base64.b64encode(screen.png_bytes(img)).decode()}
        return self._task("region", work)

    @_logged
    def test_translate(self, ko_text: str = "") -> dict:
        """번역 시험 — 지금 게임 채팅창에 보이는 외국어 최근 3줄 + 보낼 말(입력한 한국어, 없으면 예시)."""
        def work(cancel):
            from translator import incoming, outgoing
            model = settings.load()["model"]
            incoming.MODEL = model
            system.model_warm(model)
            rows, note = [], None
            chat = self._chat_now()
            if chat is None:
                note = "게임이 켜져 있지 않아 예시 문장으로 시험했습니다"
            elif not chat:
                note = "채팅창에 외국어가 없어 예시 문장으로 시험했습니다"
            for m in (chat or [{"ch": "", "name": "", "body": "哀嚎4=1 来ms", "lang": "zh"}]):
                ko, sec = incoming.translate(m["body"])
                rows.append({"dir": "in", "lang": m["lang"], "who": m["name"], "src": m["body"], "dst": ko, "sec": round(sec, 1)})
            src = (ko_text or "").strip() or "성불 탱 구해요 귓 주세요"
            lang = settings.load()["out_lang"]
            out, sec = outgoing.translate(src, lang, model, log=False)
            rows.append({"dir": "out", "lang": lang, "who": "", "src": src, "dst": out, "sec": round(sec, 1)})
            settings.save({"setup_done": True})
            return {"rows": rows, "note": note}
        return self._task("test", work)

    def _chat_now(self, n: int = 3) -> list[dict] | None:
        """게임 채팅창에서 지금 보이는 외국어 메시지(아래쪽 = 최근) n개. 게임이 없으면 None."""
        from translator import live
        if not screen.find_game_window():
            return None
        rect = chat_region.current()
        if not rect:
            return []
        r = ocr.Reader(2).read(rect, force=True)
        msgs = live.build_messages(live.pick_lines(r.get("lines", {}), rect["line_h"]), rect["line_h"])
        foreign = [m for m in msgs if m["lang"] != "ko" and len(re.sub(r"\W", "", m["body"])) >= 2
                   and not live.is_junk(m["body"])]
        return foreign[-n:]

    # ---- 통역 창 · 입력창(따로 뜨는 프로세스)
    def _alive(self, role: str) -> bool:
        p = self._procs.get(role)
        return bool(p and p.poll() is None)

    @_logged
    def start(self, role: str) -> bool:
        if role not in ("live", "input") or self._alive(role):
            return self._alive(role)
        self._procs[role] = subprocess.Popen(paths.child_command(role), creationflags=NO_WINDOW, close_fds=True)
        return True

    @_logged
    def stop(self, role: str) -> bool:
        p = self._procs.pop(role, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
        return False

    @_logged
    def stop_all(self) -> None:
        for role in list(self._procs):
            self.stop(role)

    def _live_cmd(self, cmd: str) -> None:
        """실행 중인 통역 창에 명령(파일로) — 통역 창이 2초 안에 읽는다."""
        self._cmd_n += 1
        paths.LIVE_CMD.write_text(json.dumps({"n": f"{time.time():.3f}", "cmd": cmd}), encoding="utf-8")

    @_logged
    def refind(self) -> dict:
        """채팅 영역 다시 찾기 — 통역 중이면 통역 창이 직접, 아니면 여기서."""
        if self._alive("live"):
            self._live_cmd("refind")
            return {"started": True, "by": "live"}
        return self.find_region()

    @_logged
    def reset_overlay(self) -> bool:
        try:
            paths.LIVE_UI.unlink()
        except OSError:
            pass
        if self._alive("live"):
            self._live_cmd("reset_pos")
        return True

    # ---- 창 · 폴더 · 링크
    @_logged
    def open_url(self, url: str) -> bool:
        """정해 둔 주소만 기본 브라우저로 — 앱 창 안에서 링크를 열면 화면이 그 페이지로 바뀐다."""
        if url not in LINKS:
            log.warning("blocked url %s", url)
            return False
        os.startfile(url)
        return True

    @_logged
    def open_folder(self, which: str = "data") -> bool:
        target = {"data": paths.DATA, "logs": paths.LOGS}.get(which, paths.DATA)
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target))
        return True

    @_logged
    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    @_logged
    def close(self) -> None:
        log.info("close button")
        self.stop_all()
        if self._window:
            self._window.destroy()


def _single_instance() -> bool:
    """이미 켜져 있으면 그 창을 앞으로 가져오고 False."""
    ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        hwnd = ctypes.windll.user32.FindWindowW(None, APP_NAME)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        return False
    return True


def main() -> int:
    if not _single_instance():
        return 0
    paths.ensure()
    logging.basicConfig(filename=paths.LOGS / "app.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("start %s frozen=%s data=%s", VERSION, paths.FROZEN, paths.DATA)
    # 조용히 꺼지는 일이 없게 — 처리 안 된 예외(메인·스레드)는 모두 app.log 에
    sys.excepthook = lambda et, ev, tb: log.critical("unhandled", exc_info=(et, ev, tb))
    threading.excepthook = lambda a: log.critical("thread %s", a.thread and a.thread.name,
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    screen.dpi_aware()
    api = Api()
    win = webview.create_window(APP_NAME, url=str(paths.UI / "index.html"), js_api=api, width=1120, height=740,
                                min_size=(960, 640), frameless=True, easy_drag=False, background_color="#0A0D12")
    api._window = win
    win.events.closed += lambda: log.info("window closed")
    win.events.closed += api.stop_all
    try:
        webview.start(gui="edgechromium", debug=bool(os.environ.get("WATT_DEBUG")))
    except Exception:
        log.critical("webview crashed", exc_info=True)
        raise
    finally:
        api.stop_all()
        log.info("exit")
    return 0
