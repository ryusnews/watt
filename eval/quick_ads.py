"""광고 가려내기 시험 — python -m eval.quick_ads"""
import json
import sys
from pathlib import Path

from translator.adfilter import AdFilter

CASES = json.loads((Path(__file__).parent / "ad_cases.json").read_text(encoding="utf-8"))["cases"]


def main() -> int:
    bad, f = 0, AdFilter()
    for n, c in enumerate(CASES):
        if not c.get("after_previous"):
            f = AdFilter()
        now = n * 1000.0
        for i in range(c.get("repeat", 1) - 1):
            f.check(c["name"], c["body"], now + i * 60)
        r = f.check(c["name"], c["body"], now + 600)
        ok = r["kind"] == c["kind"]
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {c['kind']:>5} → {r['kind']:<5} {r['score']:>3} {','.join(r['why']):<24} {c['body'][:50]}")
    print(f"{len(CASES) - bad}/{len(CASES)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
