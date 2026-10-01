"""OCR 헷갈리는 글자 바로잡기 시험 — python -m eval.quick_ocrfix"""
import json
import sys
from pathlib import Path

from translator.ocrfix import fix

CASES = json.loads((Path(__file__).parent / "ocrfix_cases.json").read_text(encoding="utf-8"))["cases"]


def main() -> int:
    bad = 0
    for src, want in CASES:
        got = fix(src)
        ok = got == want
        bad += not ok
        if not ok:
            print(f"BAD {src!r} → {got!r} (원함 {want!r})")
    print(f"{len(CASES) - bad}/{len(CASES)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
