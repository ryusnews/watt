"""번역 모델 비교 — 보내기(한국어 → en/zh/ru/ja)와 받기(→ 한국어)를 필수 용어·금지어·문자 체계로 채점하고 지연을 잰다.

python -m eval.compare_translate --models gemma4:12b qwen3:14b
게임 중이면 끝날 때까지 기다린다(측정이 게임과 GPU 를 나눠 쓰지 않게). 모델마다 첫 호출(올리기)은 지연에서 뺀다.
"""
import argparse
import json
import re
import statistics
import subprocess
import time
from pathlib import Path

from translator import incoming, outgoing
from translator.llm import URL, preload

HERE = Path(__file__).resolve().parent
GAMES = {"wow.exe", "wowclassic.exe", "wowb.exe", "vam.exe"}
SCRIPTS = {
    "en": lambda t: not re.search(r"[가-힣]", t),
    "zh": lambda t: re.search(r"[一-鿿]", t) and not re.search(r"[가-힣぀-ヿ]", t),
    "ru": lambda t: re.search(r"[Ѐ-ӿ]", t) and not re.search(r"[가-힣]", t),
    "ja": lambda t: re.search(r"[぀-ヿ]", t) and not re.search(r"[가-힣]", t),
    "ko": lambda t: re.search(r"[가-힣]", t),
}
for _lg in ("es", "de", "fr", "pt"):  # 라틴 문자 — 한글 · 한자가 섞이지 않으면
    SCRIPTS[_lg] = lambda t: not re.search(r"[가-힣一-鿿぀-ヿ]", t)


def wait_for_games() -> None:
    announced = False
    while True:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout.lower()
        running = [g for g in GAMES if f'"{g}"' in out]
        if not running:
            return
        if not announced:
            print(f"게임 실행 중({', '.join(running)}) — 끝날 때까지 대기", flush=True)
            announced = True
        time.sleep(60)


def unload_all() -> None:
    import urllib.request
    try:
        models = json.load(urllib.request.urlopen(URL + "/api/ps", timeout=5)).get("models", [])
        for m in models:
            req = urllib.request.Request(URL + "/api/generate", data=json.dumps({"model": m["name"], "keep_alive": 0}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
    except OSError:
        pass


def check(text: str, case: dict, script: str) -> list[str]:
    low = text.lower()
    problems = []
    for group in case.get("must", []):
        if not any(alt.lower() in low for alt in group):
            problems.append("없음:" + "/".join(group))
    for bad in case.get("must_not", []):
        if re.search(rf"(?<![A-Za-z]){re.escape(bad.lower())}(?![A-Za-z])", low):
            problems.append("금지:" + bad)
    if not SCRIPTS[script](text):
        problems.append(f"문자:{script} 아님")
    if len(text) > outgoing.MAX_LEN or not text:
        problems.append("길이")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=[outgoing.MODEL])
    args = ap.parse_args()
    cases = json.loads((HERE / "translate_cases.json").read_text(encoding="utf-8"))
    report, lines = {}, []
    for model in args.models:
        wait_for_games()
        unload_all()
        t0 = time.time()
        preload(model)  # 올리기만 — 지연에서 뺀다(번역 호출로 올리면 형식 오류로 평가 전체가 멈출 수 있다)
        load_s = time.time() - t0
        rows, lat = [], []
        for c in cases["outgoing"]:
            try:
                out, secs = outgoing.translate(c["ko"], c["to"], model, log=False)
                problems = check(out, c, c.get("script", c["to"]))
                lat.append(secs)
            except Exception as e:  # 한 문장 실패로 전체를 멈추지 않는다
                out, secs, problems = "", 0.0, [f"오류:{type(e).__name__}"]
            rows.append({"dir": "out", "src": c["ko"], "to": c["to"], "out": out, "sec": round(secs, 2), "problems": problems})
        for c in cases["incoming"]:
            try:
                ko, secs = incoming.translate(c["src"], model)
                problems = check(ko, c, "ko")
                lat.append(secs)
            except Exception as e:
                ko, secs, problems = "", 0.0, [f"오류:{type(e).__name__}"]
            rows.append({"dir": "in", "src": c["src"], "out": ko, "sec": round(secs, 2), "problems": problems})
        ok_out = sum(1 for r in rows if r["dir"] == "out" and not r["problems"])
        ok_in = sum(1 for r in rows if r["dir"] == "in" and not r["problems"])
        n_out, n_in = len(cases["outgoing"]), len(cases["incoming"])
        p90 = sorted(lat)[int(0.9 * (len(lat) - 1))]
        line = (f"{model:<24} 보내기 {ok_out}/{n_out} · 받기 {ok_in}/{n_in} · 지연 중앙 {statistics.median(lat):.2f}s "
                f"p90 {p90:.2f}s · 올리기 {load_s:.1f}s")
        print(line, flush=True)
        for r in rows:
            if r["problems"]:
                print(f"    ✗ [{r['dir']}{'→' + r['to'] if r['dir'] == 'out' else ''}] {r['src']} → {r['out']}  ({', '.join(r['problems'])})", flush=True)
        lines.append(line)
        report[model] = {"ok_out": ok_out, "n_out": n_out, "ok_in": ok_in, "n_in": n_in,
                         "median_s": statistics.median(lat), "p90_s": p90, "load_s": load_s, "rows": rows}
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"translate_{ts}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / f"translate_{ts}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n저장: {out_dir / f'translate_{ts}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
