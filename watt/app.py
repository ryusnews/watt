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
import shutil
from collections import deque

import webview

from . import APP_FULL, APP_NAME, VERSION, chat_region, housekeeping, ocr, paths, report, runner, screen, settings, system, update

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
        self._update: dict | None = None
        self._shot = None  # (게임 창, 화면) — 직접 지정 · 신고용
        self._update_t = 0.0

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
        plan = system.vram_plan(bool(cfg.get("ai_gpu")))  # 지금 남은 VRAM · 와우 몫으로 추천
        model_ok = any(m["name"] == cfg["model"] for m in ol["models"])
        mine = system.installed_by_watt()
        ol["watt"] = mine["ollama"]
        live = self._live_status()
        steps = {
            "system": "done",
            "ocr": "done" if not self._ocr_gap(oc, cfg) else "todo",
            # AI 글자 인식(권장, #83) — 켠 언어가 있고 그 모델을 받아 두었으면 done. 필수 단계는 아니다
            "ai": "done" if cfg.get("ai_langs") and self._ai_ready(cfg["ai_langs"]) else "todo",
            "ollama": "done" if ol["running"] else "todo",
            "model": "running" if "pull" in self._busy else ("done" if model_ok else "todo"),
            "addon": "done" if any(g["addon"] for g in self._game_list() if g["supported"]) else "todo",
            "region": "done" if region else "todo",
            "test": "done" if cfg.get("setup_done") else "todo",
        }
        return {
            "app": {"name": APP_NAME, "full": APP_FULL, "version": VERSION, "data": str(paths.DATA),
                    "portable": paths.PORTABLE},
            "settings": cfg, "system": self._sys, "ocr": oc, "ollama": ol,
            "removable_ocr": mine["ocr"], "watt_installed": mine,
            "models": [{**m, "installed": any(x["name"] == m["name"] for x in ol["models"]), "watt": True,  # 전용 실행기 — 모두 WATT 것
                        "recommended": m["name"] == plan["pick"]} for m in system.MODELS],
            "vram": plan,
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
        st = self._live_status()
        return {"running": {"live": self._alive("live"), "input": self._alive("input")}, "live": st,
                "feed": feed, "today": today, "avg_sec": round(sorted(secs)[len(secs) // 2], 1) if secs else None,
                "runtime": self._runtime(),
                "lighter": self._lighter_model(st["model"]) if st and st.get("slow") else None}

    @staticmethod
    def _lighter_model(current: str) -> dict | None:
        """지금 모델보다 가벼운 모델(system.MODELS 순서) 중 받아 둔 것 하나 — 없으면 바로 다음 것(받아야 함)."""
        names = [m["name"] for m in system.MODELS]
        if current not in names:
            return None
        lighter = system.MODELS[names.index(current) + 1:]
        if not lighter:
            return None
        have = {m["name"] for m in system.ollama_status().get("models", [])}
        pick = next((m for m in lighter if m["name"] in have), lighter[0])
        return {"name": pick["name"], "label": pick["label"], "installed": pick["name"] in have}  # 중앙값 — 모델 올리는 첫 번역 몇 초에 끌려가지 않게

    @staticmethod
    def _runtime() -> float:
        """오늘 통역이 돈 시간(초) — 통역 창이 2초마다 runtime.json 에 더한다."""
        try:
            rt = json.loads(paths.RUNTIME.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return rt.get("sec", 0) if rt.get("day") == time.strftime("%Y-%m-%d") else 0

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
                out.append({k: d.get(k) for k in ("t", "ch", "name", "lang", "tag", "body", "ko", "sec", "kind")})
            except ValueError:
                pass
        return out

    @_logged
    def get_terms(self) -> list[dict]:
        """기본 사전 + 사용자가 고친 것(origin: base · edited · added · hidden)."""
        from translator import terms
        return terms.all_terms()

    @_logged
    def save_term(self, entry: dict) -> dict:
        from translator import terms
        try:
            return terms.save_term(entry)
        except ValueError as e:
            return {"error": str(e)}

    @_logged
    def delete_term(self, tid: str) -> dict:
        from translator import terms
        return terms.delete_term(tid)

    @_logged
    def reset_term(self, tid: str) -> dict:
        from translator import terms
        return terms.reset_term(tid)

    # ---- 설정
    @_logged
    def save_settings(self, changes: dict) -> dict:
        old = settings.load()["model"]
        cfg = settings.save(changes)
        self._switch_model(old, cfg["model"])
        return cfg

    def _switch_model(self, old: str, new: str) -> None:
        """번역 모델을 바꾸면 WATT 가 쓰던 모델을 내리고(VRAM 비우기) 새 모델을 올린다(미리 올리기 켜짐일 때).
        다른 프로그램이 올린 다른 모델은 건드리지 않는다."""
        if not old or old == new:
            return

        def work():
            from translator import llm
            gone = llm.unload(old)
            log.info("model switch %s -> %s (unloaded=%s)", old, new, gone)
            if settings.load()["preload"]:
                llm.preload(new)
        self._switching = threading.Thread(target=work, daemon=True)
        self._switching.start()

    def start_runner(self) -> None:
        if runner.EXE.exists():
            runner.start()
            self.tidy_models()

    def tidy_models(self) -> None:
        """WATT 모델(system.MODELS) 중 지금 설정이 아닌 것이 올라가 있으면 내린다 — 모델을 바꾸고 곧바로 업데이트로 다시
        켜면 내리기가 끝나기 전에 런처가 꺼져 E4B 와 12b 가 같이 남았다(2026-10-02). 다른 프로그램의 모델은 건드리지 않는다."""
        from translator import llm
        want = settings.load()["model"]
        mine = {m["name"] for m in system.MODELS}
        for name in llm.loaded():
            if name in mine and name != want and llm.unload(name):
                log.info("unloaded leftover model %s (using %s)", name, want)

    # ---- AI 글자 인식(언어별로 켜고, 켤 때 그 모델만 받는다)
    def get_ai(self) -> dict:
        from . import aipack
        cfg = settings.load()
        try:
            live = json.loads(paths.LIVE_STATUS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            live = {}
        return {"langs": cfg["ai_langs"], "gpu": cfg["ai_gpu"], **aipack.status(),
                "active": live.get("ai") if time.time() - live.get("t", 0) < 10 else None, "active_gpu": live.get("ai_gpu"),
                "error": live.get("ai_error")}

    def _ocr_gap(self, oc: dict, cfg: dict) -> list[str]:
        """빠진 Windows OCR 언어 팩 중 AI 글자 인식이 대신하지 못하는 것 — 언어 팩을 못 까는 PC 는 AI 로 채운다."""
        from . import aiocr
        ai = set(cfg.get("ai_langs") or [])
        return [c for c in oc["missing"] if not (aiocr.COVERS.get(c) in ai and self._ai_ready([aiocr.COVERS[c]]))]

    @_logged
    def cover_ocr_with_ai(self) -> dict:
        """빠진 언어 팩 대신 AI 글자 인식 — 그 언어 모델을 받아 켠다(관리자 권한 · DISM 없이, WATT 데이터 폴더에만)."""
        from . import aiocr
        need = sorted({aiocr.COVERS[c] for c in system.ocr_status()["missing"] if c in aiocr.COVERS})
        return self.enable_ai(need) if need else {"langs": []}

    @staticmethod
    def _ai_ready(langs: list[str]) -> bool:
        from . import aipack
        return not aipack.need(langs)

    @_logged
    def enable_ai(self, langs: list[str] | None = None) -> dict:
        """환경 설정의 'AI 글자 인식' 단계 — 권장 언어(한국어 · 중국어 · 러시아어)를 한 번에 받아 켠다."""
        from . import aiocr, aipack
        langs = [lg for lg in aiocr.LANGS if lg in (langs or ["ko", "zh", "ru"])]

        def work(cancel):
            aipack.ensure(langs, lambda d, t: self._emit(type="progress", task="ai", done=d, total=t), cancel)
            have = settings.load()["ai_langs"]
            settings.save({"ai_langs": [lg for lg in aiocr.LANGS if lg in set(have) | set(langs)]})
            return {"langs": langs}
        if not aipack.need(langs):
            have = settings.load()["ai_langs"]
            settings.save({"ai_langs": [lg for lg in aiocr.LANGS if lg in set(have) | set(langs)]})
            return {"langs": langs}
        return {**self._task("ai", work), "bytes": sum(s for _, s in aipack.need(langs))}

    @_logged
    def set_ai_lang(self, lang: str, on: bool) -> dict:
        from . import aiocr, aipack
        if lang not in aiocr.LANGS:
            return {"error": "모르는 언어"}
        langs = [lg for lg in settings.load()["ai_langs"] if lg != lang] + ([lang] if on else [])
        langs = [lg for lg in aiocr.LANGS if lg in langs]
        if on and aipack.need(langs):
            def work(cancel):
                aipack.ensure(langs, lambda d, t: self._emit(type="progress", task="ai", done=d, total=t), cancel)
                settings.save({"ai_langs": langs})
                return {"langs": langs}
            return {**self._task("ai", work), "bytes": sum(s for _, s in aipack.need(langs))}
        settings.save({"ai_langs": langs})
        return {"langs": langs}

    @_logged
    def remove_ai(self) -> dict:
        from . import aipack
        settings.save({"ai_langs": []})
        try:
            live = json.loads(paths.LIVE_STATUS.read_text(encoding="utf-8"))
            if time.time() - live.get("t", 0) < 10 and live.get("ai") is not None:
                return {"error": "통역 창을 끈 뒤 지워 주세요"}
        except (OSError, ValueError):
            pass
        aipack.remove()
        return aipack.status()

    # ---- 환경 설정 단계
    @_logged
    def install_ocr(self, codes: list[str] | None = None) -> dict:
        codes = codes or system.ocr_status()["missing"]
        return self._task("ocr", lambda c: system.install_ocr(codes))

    @_logged
    def install_ollama(self) -> dict:
        def work(cancel):
            last = [0.0]

            def prog(done, total):  # 1MB 마다 불린다 — 화면에는 0.25초에 한 번
                if time.monotonic() - last[0] >= 0.25 or done >= total:
                    last[0] = time.monotonic()
                    self._emit(type="progress", task="ollama", done=done, total=total, status="AI 실행기 받는 중")
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
            runner.start()
            had = any(m["name"] == name for m in system.ollama_status()["models"])
            st = system.model_pull(name, prog, cancel)  # 전용이면 그 모델 폴더에(지울 때 폴더째)
            if runner.mode() == "system" and not had:  # PC 의 Ollama 에 받은 것 — 원래 있던 모델은 지울 때 건드리지 않는다
                system.remember(paths.INSTALLED_MODELS, name)
            self.save_settings({"model": name})
            return st
        return self._task("pull", work)

    # ---- 업데이트
    @_logged
    def check_update(self, force: bool = False) -> dict | None:
        """새 버전 — 설정이 켜져 있을 때만, 30분에 한 번(켜 둔 동안 화면이 30분마다 묻는다). force 면 지금."""
        if not force:
            if not settings.load()["update_check"]:
                return None
            if self._update and time.time() - self._update_t < 1800:  # 30분 — 6시간이면 켜 둔 동안 새 버전을 못 봤다
                return self._update
        try:
            self._update = update.check()
            self._update_t = time.time()
        except Exception as e:  # 인터넷이 없거나 GitHub 이 답하지 않음 — 조용히
            log.info("update check failed: %s", e)
            return {"error": "업데이트를 확인하지 못했습니다"} if force else None
        if self._update.get("newer") and not self._update.get("ready") and paths.FROZEN:
            self.apply_update()  # 뒤에서 바뀐 파일만 받아 둔다 — 끝나면 다시 시작할지 묻는다
        return self._update

    @_logged
    def apply_update(self) -> dict:
        """새 버전의 바뀐 파일만 받아 둔다(#15). 끝나면 {ready: 버전} — 화면이 지금 다시 시작할지 묻는다."""
        def work(cancel):
            info = self._update or update.check()
            if info.get("ready"):
                return {"ready": info["ready"]}
            if not info.get("newer"):
                return {"latest": True}
            if not paths.FROZEN:  # 개발 실행 — 페이지만
                os.startfile(info["page"])
                return {"opened": info["page"]}
            plan = update.stage(info, lambda d, t: self._emit(type="progress", task="update", done=d, total=t), cancel)
            info["ready"] = plan["version"]
            return {"ready": plan["version"], "mb": round(plan["bytes"] / 1e6, 1)}
        return self._task("update", work)

    @_logged
    def restart_update(self) -> dict:
        """받아 둔 새 버전으로 지금 바꾸고 다시 켠다."""
        ver = update.pending()
        if not ver:
            return {"ready": False}
        try:  # 통역이 돌고 있었으면 새 버전이 켜질 때 바로 이어서(업데이트 단추만 누르면 끊김 없이)
            paths.RESUME.write_text(json.dumps({"live": self._alive("live") or self._live_status() is not None,
                                                      "t": time.time(), "to": ver}), encoding="utf-8")
        except OSError:
            pass
        sw = getattr(self, "_switching", None)
        if sw and sw.is_alive():  # 모델 바꾸기(옛 모델 내리기)가 끝난 뒤에 다시 켠다
            sw.join(15)
        self.stop_all()
        release_lock()  # 바꿔 끼우기가 끝나고 다시 켜질 새 WATT 가 잠금을 잡을 수 있게
        update.launch_apply(ver)
        if self._window:
            self._window.destroy()
        return {"restarting": ver}

    @_logged
    def resume(self) -> dict:
        """업데이트 직전에 통역이 돌고 있었으면 이어서 켠다 — 10분 안에 다시 켜진 경우만(업데이트가 실패해 나중에 켠 것은 아님)."""
        try:
            r = json.loads(paths.RESUME.read_text(encoding="utf-8"))
            paths.RESUME.unlink(missing_ok=True)
        except (OSError, ValueError):
            return {}
        st = self._live_status()
        # 옛 통역 창이 꺼지기 직전에 쓴 상태 파일(몇 초 전)을 '이미 돌고 있음'으로 보고 건너뛰었다(0.1.53 → 0.1.56, 2026-10-01)
        # — 업데이트 뒤에 쓴 상태만 본다
        running = self._alive("live") or (st is not None and st.get("t", 0) > r.get("t", 0) + 1)
        if r.get("live") and time.time() - r.get("t", 0) < 600 and not running:
            log.info("resume live after update to %s", r.get("to"))
            self.start("live")
            return {"live": True}
        return {}

    @_logged
    def delete_model(self, name: str) -> dict:
        if name in self._busy:
            raise RuntimeError("받는 중인 모델은 지울 수 없습니다")
        if name not in system.read_list(paths.INSTALLED_MODELS):
            raise RuntimeError("WATT 가 받지 않은 모델은 지우지 않습니다")
        return system.model_delete(name)

    @_logged
    def remove_addon(self, flavor_dir: str) -> dict:
        res = system.remove_addon(flavor_dir)
        self._game_list(force=True)
        return res

    @_logged
    def remove_ocr(self, code: str) -> dict:
        return self._task("ocr", lambda c: system.remove_ocr([code]))

    @_logged
    def uninstall_ollama(self) -> dict:
        if not system.installed_by_watt()["ollama"]:
            raise RuntimeError("WATT 가 설치하지 않은 Ollama 는 제거하지 않습니다")
        self.stop_all()  # 통역 창·입력창이 Ollama 를 쓰고 있다
        return self._task("ollama", lambda c: system.ollama_uninstall())

    @_logged
    def cleanup_installed(self) -> dict:
        """WATT 가 설치한 것 모두 되돌리기(포터블은 삭제 프로그램이 없으므로) — 모델 → OCR 팩 → 애드온 → Ollama."""
        def work(cancel):
            mine, done = system.installed_by_watt(), []
            self.stop_all()
            for m in mine["models"]:
                try:
                    system.model_delete(m)
                    done.append(m)
                except Exception as e:
                    log.warning("model delete %s: %s", m, e)
            if mine["ocr"]:
                system.remove_ocr(mine["ocr"])
                done += [c for c in mine["ocr"] if c not in system.read_list(paths.INSTALLED_OCR)]
            for d in mine["addons"]:
                system.remove_addon(d)
                done.append(d)
            if mine["ollama"]:
                system.ollama_uninstall()
                done.append("ollama")
            from . import aiocr, aipack
            if aiocr.AI_DIR.exists():  # AI 글자 인식 파일(통역 창을 껐으니 지울 수 있다)
                aipack.remove()
                settings.save({"ai_langs": []})
                done.append("ai")
            self._game_list(force=True)
            return {"done": done, "left": system.installed_by_watt()}
        return self._task("cleanup", work)

    @_logged
    def get_storage(self) -> dict:
        return housekeeping.usage()

    @_logged
    def clear_logs(self) -> dict:
        return housekeeping.clear()

    @_logged
    def use_model(self, name: str) -> dict:
        return self.save_settings({"model": name})

    @_logged
    def set_runner_mode(self, mode: str) -> dict:
        """AI 실행기 방식 — system(PC 의 Ollama) · watt(WATT 전용, 없으면 받기부터)."""
        def work(cancel):
            self.stop_all()  # 통역 창 · 입력창이 실행기를 쓰고 있다
            runner.set_mode(mode)
            return system.ollama_status()
        return self._task("ollama", work)

    @_logged
    def models_info(self) -> dict:
        """모델 폴더 · 쓰는 용량 · 빈 공간 · PC 의 Ollama 에 이미 있는 WATT 모델(다시 받지 않고 가져오기)."""
        store = runner.models_dir()
        try:
            free = shutil.disk_usage(store.anchor or str(store)).free
        except OSError:
            free = 0
        mine = {m["name"] for m in system.MODELS}
        have = {m["name"] for m in system.ollama_status().get("models", [])}
        watt = runner.mode() == "watt"
        return {"mode": runner.mode(), "dir": str(store), "custom": settings.load().get("models_dir", ""),
                "used": runner.store_size(store) if watt else 0, "free": free,
                "importable": [n for n in runner.system_models() if n in mine and n not in have] if watt else []}

    @_logged
    def pick_models_dir(self) -> dict:
        """모델 폴더 고르기 → 옮기기(받아 둔 모델이 있으면 함께). 같은 드라이브면 바로, 다른 드라이브면 복사라 오래 걸린다."""
        if not self._window:
            return {"ok": False, "error": "창이 없습니다"}
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=settings.load().get("models_dir") or "")
        if not res:
            return {"ok": False, "cancel": True}
        folder = res[0] if isinstance(res, (list, tuple)) else res
        return self.set_models_dir(folder)

    @_logged
    def set_models_dir(self, folder: str) -> dict:
        """'' 이면 기본(데이터 폴더)으로."""
        def work(cancel):
            self.stop_all()  # 통역 창 · 입력창이 실행기를 쓰고 있다
            dst = runner.move_models(folder or "", lambda d, t: self._emit(type="progress", task="move", done=d, total=t),
                                     cancel) if folder else self._models_default(cancel)
            return {"dir": dst}
        return self._task("move", work)

    def _models_default(self, cancel) -> str:
        """고른 폴더 → 기본 폴더로 되돌리기."""
        cur = settings.load().get("models_dir") or ""
        if not cur:
            return str(runner.models_dir())
        src = runner.models_dir()
        dst = runner.models_dir("")
        was = runner.running()
        runner.stop()
        if src.exists():
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                if cancel.is_set():
                    raise RuntimeError("취소됨")
                shutil.move(str(item), str(dst / item.name))
            shutil.rmtree(src, ignore_errors=True)
        settings.save({"models_dir": ""})
        if was:
            runner.start()
        return str(dst)

    @_logged
    def import_model(self, name: str) -> dict:
        """PC 의 Ollama 에 이미 있는 모델을 WATT 모델 폴더로(같은 드라이브면 공간을 더 쓰지 않음)."""
        def work(cancel):
            runner.import_model(name, lambda d, t: self._emit(type="progress", task="pull", model=name, status="가져오는 중",
                                                              done=d, total=t, speed=0), cancel)
            runner.stop()  # 새 모델 목록을 읽게 다시 켠다
            runner.start()
            self.save_settings({"model": name})
            return system.ollama_status()
        return self._task("pull", work)

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
            img = screen.capture_game(rect["x"], rect["y"], rect["w"], rect["h"])
            return {"region": r, "preview": "data:image/png;base64," + base64.b64encode(screen.png_bytes(img)).decode()}
        return self._task("region", work)

    @_logged
    def list_windows(self) -> dict:
        """게임 창 고르기 — WoW 창 먼저, 그다음 다른 창(실행 파일 이름이 다를 때). WATT 창은 빼고."""
        screen.dpi_aware()
        games = screen.list_windows(True)
        others = [w for w in screen.list_windows(False) if w["exe"].lower() not in screen.GAME_EXES
                  and w["exe"].lower() != "watt.exe"]
        cur = screen.find_game_window()
        row = lambda w: {"exe": w["exe"], "title": w["title"], "w": w["w"], "h": w["h"]}  # noqa: E731
        return {"games": [row(w) for w in games], "others": [row(w) for w in others],
                "picked": settings.load().get("game_exe", ""), "current": cur["exe"] if cur else None}

    @_logged
    def pick_window(self, exe: str) -> dict:
        """고른 게임 창(실행 파일) 저장 — 빈 값이면 자동(WoW 창 중 가장 큰 것)."""
        settings.save({"game_exe": exe or ""})
        return self.list_windows()

    # ---- 채팅 영역 직접 지정 · 인식 오류 신고(#14)
    @_logged
    def get_shot(self) -> dict:
        """게임 창 화면(미리 보기용으로 줄여서) + 지금 찾은 영역(창 기준). 다른 창이 가려도 게임 화면."""
        win = screen.find_game_window()
        if not win:
            return {"error": "게임이 꺼져 있습니다"}
        img = screen.capture_window(win)
        if img is None:
            img = screen.capture(win["x"], win["y"], win["w"], win["h"])
        self._shot = (win, img)
        found = None
        good = chat_region.last_good()
        if good and (good["window"]["w"], good["window"]["h"]) == (win["w"], win["h"]):
            found = good["chat"]
        small = report.shrink(img, 1280)
        return {"img": "data:image/jpeg;base64," + base64.b64encode(report.jpeg(small)).decode(),
                "w": win["w"], "h": win["h"], "found": found, "manual": bool(good and good.get("manual")),
                "report": report.status()}

    @_logged
    def set_region(self, rect: dict) -> dict:
        """직접 지정한 채팅 영역 저장 — 통역 중이면 2초 안에 바뀐다."""
        if not self._shot:
            return {"error": "화면을 다시 불러 주세요"}
        win, img = self._shot
        if rect["w"] < 120 or rect["h"] < 40:
            return {"error": "영역이 너무 작습니다"}
        r = chat_region.from_rect(win, rect, img)
        chat_region.save(r)
        c = r["chat"]
        crop = img[c["y"]:c["y"] + c["h"], c["x"]:c["x"] + c["w"]]
        return {"region": r, "preview": "data:image/png;base64," + base64.b64encode(screen.png_bytes(crop)).decode()}

    @_logged
    def send_report(self, found: dict | None = None, picked: dict | None = None, reason: str = "region") -> dict:
        if not self._shot:
            return {"error": "화면을 다시 불러 주세요"}
        win, img = self._shot
        rect = lambda r: {k: int(r[k]) for k in ("x", "y", "w", "h")} if r else None  # noqa: E731
        meta = {"found": rect(found), "picked": rect(picked), "window": {"x": 0, "y": 0, "w": win["w"], "h": win["h"]},
                "image": {"x": 0, "y": 0, "w": img.shape[1], "h": img.shape[0]}, "reason": reason,
                "lines": (chat_region.last_good() or {}).get("chat_lines_found", 0),
                "engines": [k for k, ok in ocr.installed().items() if ok],
                "flavor": system.FLAVORS.get(os.path.basename(os.path.dirname(win.get("path", ""))), win.get("exe"))}
        res = report.send(img, meta)
        log.info("report %s", res)
        return res

    @_logged
    def test_translate(self, ko_text: str = "") -> dict:
        """번역 시험 — 지금 게임 채팅창에 보이는 외국어 최근 3줄 + 보낼 말(입력한 한국어, 없으면 예시)을 모든 보내기 언어로."""
        def work(cancel):
            from translator import incoming, outgoing
            model = settings.load()["model"]
            incoming.MODEL = model
            system.model_warm(model)
            rows, note = [], None
            chat = self._chat_now()
            if chat is None:
                note = "게임이 꺼져 있음"
            elif not chat:
                note = "지금 외국어 없음"
            for m in chat or []:  # 예시 문장은 쓰지 않는다 — 입력한 말의 번역으로 오해했다
                ko, sec = incoming.translate(m["body"])
                rows.append({"dir": "in", "lang": m["lang"], "who": m["name"], "src": m["body"], "dst": ko, "sec": round(sec, 1)})
            src = (ko_text or "").strip() or "성불 탱 구해요 귓 주세요"
            first = settings.load()["out_lang"]
            for lang in [first] + [c for c, _, _ in outgoing.LANGS if c != first]:  # 기본 보내기 언어부터
                if cancel.is_set():
                    break
                out, sec = outgoing.translate(src, lang, model, log=False)
                rows.append({"dir": "out", "lang": lang, "who": "", "src": src, "dst": out, "sec": round(sec, 1)})
            settings.save({"setup_done": True})
            return {"rows": rows, "note": note}
        return self._task("test", work)

    def _chat_now(self, n: int = 3) -> list[dict] | None:
        """게임 채팅창에서 지금 보이는 외국어 메시지(아래쪽 = 최근) n개. 게임이 없으면 None."""
        from translator import live
        from translator.adfilter import AdFilter
        if not screen.find_game_window():
            return None
        rect = chat_region.current()
        if not rect:
            return []
        r = ocr.Reader(2).read(rect, force=True)
        rows = live.pick_lines(r.get("lines", {}), rect["line_h"])
        msgs = live.build_messages(rows, live.line_pitch(rows, rect["line_h"]))
        foreign = [m for m in msgs if m["lang"] != "ko" and m["name"] and len(re.sub(r"\W", "", m["body"])) >= 2
                   and not live.is_junk(m["body"])]
        ads = AdFilter()
        foreign = [m for m in foreign if ads.check(m["name"], m["body"])["kind"] != "ad"]  # 광고는 시험에서 빼기
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

    @_logged
    def set_hotkey(self, combo: str) -> dict:
        """보내기 입력창 단축키 — 쓸 수 있는지(다른 프로그램이 쓰는지) 보고 저장. 켜 둔 입력창은 1초 안에 바꾼다."""
        from . import hotkey
        combo = combo or hotkey.DEFAULT
        if combo != settings.load().get("hotkey_input"):
            why = hotkey.check(combo)
            if why:
                return {"ok": False, "why": why}
        settings.save({"hotkey_input": combo})
        return {"ok": True, "combo": combo}

    @_logged
    def reset_input(self) -> bool:
        """한국어 입력창 위치 되돌리기 — 다음에 열 때(Ctrl+Shift+K) 채팅창 위로."""
        try:
            paths.INPUT_UI.unlink()
        except OSError:
            pass
        return True

    # ---- 창 · 폴더 · 링크
    @_logged
    def open_url(self, url: str) -> bool:
        """정해 둔 주소만 기본 브라우저로 — 앱 창 안에서 링크를 열면 화면이 그 페이지로 바뀐다."""
        if url not in LINKS and not url.startswith(f"https://github.com/{update.REPO}/"):
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

    # ---- 창 크기 · 위치 — 테두리 없는 창이라 화면(가장자리 손잡이 · 최대화 단추)이 부른다. 좌표는 물리 픽셀
    _maxed_from: dict | None = None

    def _hwnd(self) -> int:
        """이 프로세스의 런처 창(WinForms) — 다른 스레드에서 Form.Handle 을 읽지 않으려고 창 목록에서 찾는다."""
        if getattr(self, "_hwnd_cache", 0) and ctypes.windll.user32.IsWindow(self._hwnd_cache):
            return self._hwnd_cache
        found = []
        pid = os.getpid()
        PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def each(hwnd, _):
            p = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(p))
            if p.value == pid and ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
                buf = ctypes.create_unicode_buffer(64)
                ctypes.windll.user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 64)
                if buf.value == APP_NAME:
                    found.append(hwnd)
                    return False
            return True
        ctypes.windll.user32.EnumWindows(PROC(each), None)
        self._hwnd_cache = found[0] if found else 0
        return self._hwnd_cache

    def get_window_rect(self) -> dict:
        r = (ctypes.c_long * 4)()
        ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(self._hwnd()), r)
        return {"x": r[0], "y": r[1], "w": r[2] - r[0], "h": r[3] - r[1], "maxed": self._maxed_from is not None}

    def set_window_rect(self, x: int, y: int, w: int, h: int) -> None:
        # SWP_NOZORDER | SWP_NOACTIVATE — 최소 크기는 WinForms 가 지킨다
        ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(self._hwnd()), None, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)

    def _work_area(self) -> tuple[int, int, int, int]:
        """창이 있는 모니터의 작업 영역(작업 표시줄 뺀 곳)."""
        mon = ctypes.windll.user32.MonitorFromWindow(ctypes.c_void_p(self._hwnd()), 2)  # MONITOR_DEFAULTTONEAREST

        class MI(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("rc", ctypes.c_long * 4), ("work", ctypes.c_long * 4), ("flags", ctypes.c_ulong)]
        mi = MI()
        mi.cb = ctypes.sizeof(MI)
        ctypes.windll.user32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
        return mi.work[0], mi.work[1], mi.work[2] - mi.work[0], mi.work[3] - mi.work[1]

    @_logged
    def toggle_maximize(self) -> bool:
        """최대화 ↔ 원래 크기. 창 테두리가 없으면 Windows 최대화가 작업 표시줄까지 덮어서 작업 영역에 직접 맞춘다."""
        if self._maxed_from:
            r, self._maxed_from = self._maxed_from, None
            self.set_window_rect(r["x"], r["y"], r["w"], r["h"])
            return False
        self._maxed_from = self.get_window_rect()
        self.set_window_rect(*self._work_area())
        return True

    def save_window_rect(self) -> None:
        if self._maxed_from:
            return
        try:
            r = self.get_window_rect()
            paths.LAUNCHER_UI.write_text(json.dumps({k: r[k] for k in ("x", "y", "w", "h")}), encoding="utf-8")
        except OSError:
            pass

    def restore_window_rect(self) -> None:
        """지난번 위치 · 크기로 — 그 자리에 모니터가 없으면(모니터를 뺐음) 그대로 둔다."""
        try:
            r = json.loads(paths.LAUNCHER_UI.read_text(encoding="utf-8"))
            rc = (ctypes.c_long * 4)(r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"])
            if ctypes.windll.user32.MonitorFromRect(rc, 0):  # MONITOR_DEFAULTTONULL
                self.set_window_rect(r["x"], r["y"], r["w"], r["h"])
        except (OSError, ValueError, KeyError):
            pass

    @_logged
    def close(self) -> None:
        log.info("close button")
        self.stop_all()
        runner.stop()
        if self._window:
            self._window.destroy()


_lock = None


def release_lock() -> None:
    """한 번만 켜기 잠금 풀기 — 업데이트 설치 직전."""
    global _lock
    if _lock:
        ctypes.windll.kernel32.CloseHandle(_lock)
        _lock = None


def _single_instance() -> bool:
    """이미 켜져 있으면 그 창을 앞으로 가져오고 False."""
    global _lock
    _lock = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
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
    logging.basicConfig(level=logging.INFO, handlers=[housekeeping.file_handler("app.log")])
    ready = update.pending() if paths.FROZEN else None
    if ready:  # 지난번에 받아 둔 새 버전 — 켤 때 바꿔 끼우고 새 버전으로 다시 켜진다(#15)
        log.info("apply staged update %s", ready)
        try:
            release_lock()
            update.launch_apply(ready)
            return 0
        except Exception:
            log.exception("apply staged update failed")
            _single_instance()
    threading.Thread(target=housekeeping.prune, daemon=True).start()  # 오래된 기록·다 쓴 설치 파일
    log.info("start %s frozen=%s portable=%s data=%s", VERSION, paths.FROZEN, paths.PORTABLE, paths.DATA)
    update.cleanup_stage()  # 지난 업데이트에 쓴 것(받아 두고 아직 안 쓴 새 버전은 남긴다)
    # 조용히 꺼지는 일이 없게 — 처리 안 된 예외(메인·스레드)는 모두 app.log 에
    sys.excepthook = lambda et, ev, tb: log.critical("unhandled", exc_info=(et, ev, tb))
    threading.excepthook = lambda a: log.critical("thread %s", a.thread and a.thread.name,
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    screen.dpi_aware()
    api = Api()
    win = webview.create_window(APP_NAME, url=str(paths.UI / "index.html"), js_api=api, width=1120, height=740,
                                min_size=(760, 560), frameless=True, easy_drag=False, background_color="#0A0D12")
    api._window = win
    win.events.shown += api.restore_window_rect
    threading.Thread(target=api.start_runner, daemon=True).start()  # WATT 전용 AI 실행기 켜기 + 지난번에 남은 모델 정리
    win.events.closed += lambda: log.info("window closed")
    win.events.closed += api.stop_all
    win.events.closed += runner.stop  # WATT 전용 실행기도 — VRAM 을 비운다
    try:
        # 캐시를 데이터 폴더 안에 — 기본값은 켤 때마다 %TEMP% 에 새 폴더를 남긴다(정리되지 않음)
        webview.start(gui="edgechromium", debug=bool(os.environ.get("WATT_DEBUG")), storage_path=str(paths.WEBVIEW))
    except Exception:
        log.critical("webview crashed", exc_info=True)
        raise
    finally:
        api.stop_all()
        log.info("exit")
    return 0
