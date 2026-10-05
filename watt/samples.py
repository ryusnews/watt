"""번역 품질 개선 참여(#7) — 동의한 설치만, 번역 표본(이름 없이)을 조금씩 WATT 서버로. 우리가 로컬 AI 로 검증해
사전 · 규칙 · 정답 표본을 키운다(tools/fetch_samples.py).

사용자는 환경 설정에서 한 번 고르고(기본값 없음) 번역 설정에서 언제든 끈다. 끄면 그때부터 보내지 않고, 모아 둔 것도 지운다.
- 새로 번역한 줄만(캐시 · 한국어 그대로 · 실패는 빼고), 하루 최대 200개
- 50개가 모이거나 통역을 다시 켤 때 남은 것을 보낸다. 보낼 게 없으면 아무 요청도 하지 않는다. 실패하면 남겨 두었다가 다음에
- 보낸 것은 data\\sent\\samples_<날짜>.jsonl 에 그대로 남는다(무엇을 보냈는지 볼 수 있게)
보내는 것: 원문 본문 · 번역 · 언어 · 원문 언어 표시 · 종류 · 모델 · 번역 시간 · 엔진마다 읽은 글 · 버전 · 날짜.
빼는 것: 보낸 사람 이름 · 채널 · 본문 속 '[이름]:' · 연락처 · 주소(짧은 해시로 바꿈) · 화면 이미지 · 시각.
"""
import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

from . import VERSION, paths, report, settings

log = logging.getLogger("watt")
API = report.API + "/v1/samples"
DIR = paths.DATA / "samples"
PENDING = DIR / "pending.jsonl"
COUNT = DIR / "count.json"
SENT = paths.DATA / "sent"
DAY_MAX = 200
BATCH = 50
_lock = threading.Lock()

# 연락처 · 주소 — 광고의 위챗 · QQ · 디스코드, 사이트, 메일, 긴 숫자(서버도 한 번 더 거른다)
CONTACT = re.compile(r"(?i)https?://\S+|www\.\S+|discord(?:app)?\.(?:gg|com)/\S+|"
                     r"\b[\w.-]+\.(?:com|net|org|gg|cn|ru|io|me|tv|cc|xyz|shop|top)\b(?:/\S*)?|"
                     r"[\w.%+-]+@[\w-]+\.[\w.]+|"
                     r"(?:微信|威信|扣扣|群|加\s*[VvＶ])\s*[:：=]?\s*[A-Za-z0-9_-]{5,}|"
                     # 라틴 머리말은 띄우거나 : 를 두고 숫자가 든 ID 만 — 'Vietnam' 의 v 를 위챗 ID 로 가렸다
                     r"(?<![A-Za-z])(?:qq|vx|wx|v|wechat|tg|telegram|line|kakao)(?:\s*[:：=]\s*|\s+)(?=[A-Za-z_-]*\d)[A-Za-z0-9_-]{5,}|"
                     r"\d{5,}")
HEAD = re.compile(r"\[[^\[\]]{1,32}\]\s*[:：]\s*")       # '[이름]:' — 본문에 섞인 다른 메시지의 머리
LEAD = re.compile(r"^.{0,60}?\]\s*[:：]\s*")             # 엔진이 읽은 줄 앞의 '11:06 [1] [이름]:'


def enabled() -> bool:
    return settings.load().get("share_samples") is True


def _mask(s: str, names: tuple[str, ...] = ()) -> str:
    s = CONTACT.sub(lambda m: "<id:" + hashlib.sha1(m[0].lower().encode()).hexdigest()[:8] + ">", s)
    s = HEAD.sub(" ", s)
    for n in names:
        if n and len(n) >= 2:
            s = s.replace(n, "<name>")
    return re.sub(r"\s{2,}", " ", s).strip()


def _reads(m: dict, names: tuple[str, ...]) -> dict:
    """엔진마다 읽은 그 메시지(머리 · 이름 뺌) — 인식 오류를 찾는 데."""
    out: dict[str, list[str]] = {}
    for row in m.get("rows") or []:
        for eng, txt in (row.get("cand") or {}).items():
            if txt:
                out.setdefault(eng, []).append(LEAD.sub("", str(txt), count=1))
    return {k: _mask(" ".join(v), names)[:300] for k, v in out.items()}


def _score(body: str, ko: str, reads: dict) -> int:
    """우리가 먼저 볼 것 — 엔진끼리 다르게 읽음 · 번역에 원문 라틴 낱말이 남음."""
    vals = {re.sub(r"\W", "", v) for v in reads.values() if v}
    left = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", ko)} & {w.lower() for w in re.findall(r"[A-Za-z]{4,}", body)}
    return (len(vals) > 1) + bool(left)


def sample(m: dict, sec: float, model: str) -> dict:
    names = tuple(x for x in (m.get("name"), m.get("ch")) if x)
    body, ko = _mask(m.get("body") or "", names)[:400], _mask(m.get("ko") or "", names)[:600]
    reads = _reads(m, names)
    return {"day": time.strftime("%Y-%m-%d"), "v": VERSION, "lang": m.get("lang") or "", "src": m.get("tag") or "",
            "kind": m.get("kind") or "", "model": model, "sec": round(float(sec or 0), 2), "body": body, "ko": ko,
            "reads": reads, "score": _score(body, ko, reads)}


def _today_count() -> int:
    try:
        c = json.loads(COUNT.read_text(encoding="utf-8"))
        return c["n"] if c.get("day") == time.strftime("%Y-%m-%d") else 0
    except (OSError, ValueError, KeyError):
        return 0


def _pending() -> list[str]:
    try:
        return [l for l in PENDING.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


def add(m: dict, sec: float, model: str) -> None:
    """통역 창이 새로 번역할 때마다 부른다. 동의하지 않았으면 아무것도 남기지 않는다."""
    if not enabled():
        return
    with _lock:
        n = _today_count()
        if n >= DAY_MAX:
            return
        s = sample(m, sec, model)
        if not s["body"] or not s["ko"]:
            return
        DIR.mkdir(parents=True, exist_ok=True)
        with PENDING.open("a", encoding="utf-8") as f:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
        COUNT.write_text(json.dumps({"day": s["day"], "n": n + 1}), encoding="utf-8")
        due = len(_pending()) >= BATCH
    if due:
        threading.Thread(target=flush, name="samples", daemon=True).start()


def flush() -> dict:
    """모인 것을 보낸다 — {sent} · {error}. 보낼 게 없거나 끔이면 요청하지 않는다."""
    if not enabled():
        return {"sent": 0}
    with _lock:
        lines = _pending()
        if not lines:
            return {"sent": 0}
        items = [json.loads(l) for l in lines[:100]]
        body = json.dumps({"install": report.install_id(), "ver": VERSION, "items": items}, ensure_ascii=False).encode()
        req = urllib.request.Request(API, data=body, method="POST", headers={**report.UA, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:  # 오늘 몫을 다 씀 — 서버가 받지 않는 것은 버린다(내일로 미루면 하루 몫이 계속 밀린다)
                PENDING.unlink(missing_ok=True)
            log.info("samples send failed: HTTP %s", e.code)
            return {"error": e.code}
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.info("samples send failed: %s", e)
            return {"error": str(e)}
        SENT.mkdir(parents=True, exist_ok=True)
        with (SENT / f"samples_{time.strftime('%Y%m%d')}.jsonl").open("a", encoding="utf-8") as f:
            f.write("\n".join(lines[:100]) + "\n")
        rest = lines[100:]
        if rest:
            PENDING.write_text("\n".join(rest) + "\n", encoding="utf-8")
        else:
            PENDING.unlink(missing_ok=True)
        log.info("samples sent %s (accepted %s)", len(items), res.get("accepted"))
        return {"sent": len(items)}


def clear() -> None:
    """참여를 끄면 — 아직 보내지 않은 것을 지운다."""
    with _lock:
        PENDING.unlink(missing_ok=True)
