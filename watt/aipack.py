"""AI 글자 인식 팩 — 켠 언어에 필요한 것만 받는다(실행 엔진 + 공통 글자 상자 모델 + 언어 모델).

설치판에는 넣지 않는다(실행 엔진만 ~65MB). 출처 · SHA256 은 aiocr 에 고정 — 다르면 쓰지 않는다.
"""
import hashlib
import shutil
import threading
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from . import aiocr, paths, system

TRUSTED = {"files.pythonhosted.org", "www.modelscope.cn", "modelscope.cn"}
SKIP = ("onnxruntime/tools/", "onnxruntime/transformers/", "onnxruntime/quantization/", "onnxruntime/datasets/",
        "onnxruntime/backend/")  # 실행에 필요 없는 도구들


def runtime_ready() -> bool:
    return (aiocr.RUNTIME_DIR / "onnxruntime" / "capi" / "onnxruntime.dll").exists() or _dev_runtime()


def _dev_runtime() -> bool:
    if paths.FROZEN:
        return False
    import importlib.util
    return importlib.util.find_spec("onnxruntime") is not None


def model_ready(name: str) -> bool:
    f = aiocr.MODEL_DIR / aiocr.MODELS[name][0]
    return f.exists() and f.stat().st_size == aiocr.MODELS[name][3]


def need(langs: list[str]) -> list[tuple[str, int]]:
    """이 언어들을 쓰려면 더 받을 것 [(이름, 바이트)]."""
    out = []
    if not runtime_ready():
        out.append(("runtime", aiocr.RUNTIME["size"]))
    for name in ["det", *langs]:
        if not model_ready(name):
            out.append((name, aiocr.MODELS[name][3]))
    return out


def status() -> dict:
    disk = sum(f.stat().st_size for f in aiocr.AI_DIR.rglob("*") if f.is_file()) if aiocr.AI_DIR.exists() else 0
    return {"runtime": runtime_ready(), "models": {n: model_ready(n) for n in aiocr.MODELS}, "disk": disk,
            "sizes": {"runtime": aiocr.RUNTIME["size"], **{n: m[3] for n, m in aiocr.MODELS.items()}}}


def _get(url: str, sha: str, dest: Path, progress, cancel) -> Path:
    if urlparse(url).hostname not in TRUSTED:
        raise RuntimeError("믿을 수 없는 주소입니다")
    system.download(url, dest, progress, cancel)
    h = hashlib.sha256()
    with dest.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != sha:
        dest.unlink(missing_ok=True)
        raise RuntimeError("받은 파일의 확인값이 맞지 않아 쓰지 않았습니다")
    return dest


def ensure(langs: list[str], progress=None, cancel: threading.Event | None = None) -> dict:
    """필요한 것을 받아 둔다. progress(받은, 전체)."""
    todo = need(langs)
    total = sum(s for _, s in todo) or 1
    base = 0

    def prog(done, _):
        if progress:
            progress(base + done, total)
    for name, size in todo:
        if name == "runtime":
            whl = _get(aiocr.RUNTIME["url"], aiocr.RUNTIME["sha256"], paths.DOWNLOADS / "onnxruntime_directml.whl", prog, cancel)
            tmp = aiocr.RUNTIME_DIR.with_name("runtime.part")
            shutil.rmtree(tmp, ignore_errors=True)
            with zipfile.ZipFile(whl) as z:
                for m in z.namelist():
                    if m.startswith("onnxruntime/") and not m.startswith(SKIP):
                        z.extract(m, tmp)
            shutil.rmtree(aiocr.RUNTIME_DIR, ignore_errors=True)
            tmp.replace(aiocr.RUNTIME_DIR)
            whl.unlink(missing_ok=True)
        else:
            fn, url, sha, _ = aiocr.MODELS[name]
            aiocr.MODEL_DIR.mkdir(parents=True, exist_ok=True)
            _get(url, sha, aiocr.MODEL_DIR / fn, prog, cancel)
        base += size
    return status()


def remove() -> None:
    """받은 AI 팩을 모두 지운다."""
    shutil.rmtree(aiocr.AI_DIR, ignore_errors=True)
