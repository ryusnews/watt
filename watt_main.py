"""PyInstaller 진입 스크립트 — WATT.exe 는 여기서 시작한다(python -m watt 와 같다)."""
import sys

from watt.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
