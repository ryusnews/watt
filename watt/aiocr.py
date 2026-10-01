"""AI 글자 인식(PaddleOCR 모델, ONNX) — Windows OCR 을 언어별로 보강한다.

Windows OCR 은 OS 에 깔린 엔진을 부르므로 언어마다 켤 일이 없지만, AI 모델은 언어마다 따로라 켠 언어만 받아 돌린다.
글자 상자는 한 번 찾고(공통 모델) 켠 언어 모델이 같은 조각을 읽는다 → 그 엔진 자리(ko · zh-Hans-CN · ru-RU)의 줄을 바꿔 낀다.
영어는 Windows OCR 이 잘 읽어 그대로 둔다. opencv 없이 numpy + onnxruntime(DirectML: GPU, 없으면 CPU)만 쓴다.

실행 엔진(onnxruntime)과 모델은 설치판에 넣지 않고, 사용자가 켤 때 받는다(aipack).
"""
import hashlib
import math
import re
import sys
from collections import OrderedDict

import numpy as np

from . import paths

# 받을 것 — 출처와 SHA256 을 고정한다(바뀌면 받지 않는다)
RUNTIME = {
    "name": "onnxruntime-directml 1.24.4",
    "url": "https://files.pythonhosted.org/packages/88/ea/33814eb0ec96775eda4c1d30b0d86e91d7d2cd0d84c66d3915aef0e06fa3/"
           "onnxruntime_directml-1.24.4-cp312-cp312-win_amd64.whl",
    "sha256": "f2ecb68b7b7b259d2ef3112ae760149f9b5a1e7c0fbb73d539da6250a648a614",
    "size": 25111930,
}
_MS = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/"
MODELS = {  # 이름: (파일, 주소, SHA256, 크기)
    "det": ("PP-OCRv6_det_small.onnx", _MS + "PP-OCRv6/det/PP-OCRv6_det_small.onnx",
            "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f", 9929594),
    "ko": ("korean_PP-OCRv5_rec_mobile.onnx", _MS + "PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile.onnx",
           "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b", 13488748),
    "zh": ("PP-OCRv6_rec_small.onnx", _MS + "PP-OCRv6/rec/PP-OCRv6_rec_small.onnx",
           "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884", 21234383),
    "ru": ("cyrillic_PP-OCRv5_rec_mobile.onnx", _MS + "PP-OCRv5/rec/cyrillic_PP-OCRv5_rec_mobile.onnx",
           "90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711", 8074092),
}
LANGS = ("ko", "zh", "ru")  # 켤 수 있는 언어 — 영어는 Windows OCR
ENGINE_KEY = {"ko": "ko", "zh": "zh-Hans-CN", "ru": "ru-RU"}  # 바꿔 낄 Windows 엔진 자리

AI_DIR = paths.DATA / "ai"
RUNTIME_DIR = AI_DIR / "runtime"
MODEL_DIR = AI_DIR / "models"

CJK = re.compile(r"[぀-ヿ㐀-鿿가-힣＀-￯]")
_HAN = re.compile(r"[一-鿿]")
_HANGUL = re.compile(r"[가-힣]")
_CYR = re.compile(r"[Ѐ-ӿ]")
# 이 줄에 이 언어 모델이 필요한가 — Windows 엔진이 읽은 같은 줄로 본다(모델 3개를 모든 줄에 돌리면 게임 중 띠 하나에 ~0.8초).
# 한국어 엔진은 한자를 한자로, 러시아어 엔진은 키릴을 키릴로 읽는다. 못 알아본 줄은 Windows 것이 그대로 남는다(ocr.read_lines)
NEED = {"ko": lambda h: len(_HANGUL.findall(h.get("ko", ""))) >= 2,
        "zh": lambda h: bool(_HAN.search(h.get("ko", ""))),
        "ru": lambda h: len(_CYR.findall(h.get("ru-RU", ""))) >= 3}


def import_runtime():
    """받아 둔 onnxruntime 을 불러온다(없으면 None)."""
    try:
        import onnxruntime  # noqa: F401 — 개발 환경에 깔려 있으면 그것
        return onnxruntime
    except ImportError:
        pass
    if (RUNTIME_DIR / "onnxruntime").is_dir():
        if str(RUNTIME_DIR) not in sys.path:
            sys.path.insert(0, str(RUNTIME_DIR))
        try:
            import onnxruntime
            return onnxruntime
        except ImportError:
            return None
    return None


def resize(img: np.ndarray, h: int, w: int) -> np.ndarray:
    """쌍선형 크기 바꾸기(H×W×C, uint8 · float)."""
    H, W = img.shape[:2]
    if (H, W) == (h, w):
        return img.astype(np.float32)
    ys = np.clip((np.arange(h) + 0.5) * H / h - 0.5, 0, H - 1)
    xs = np.clip((np.arange(w) + 0.5) * W / w - 0.5, 0, W - 1)
    y0, x0 = ys.astype(int), xs.astype(int)
    y1, x1 = np.minimum(y0 + 1, H - 1), np.minimum(x0 + 1, W - 1)
    fy, fx = (ys - y0)[:, None, None], (xs - x0)[None, :, None]
    f = img.astype(np.float32)
    top = f[y0][:, x0] * (1 - fx) + f[y0][:, x1] * fx
    bot = f[y1][:, x0] * (1 - fx) + f[y1][:, x1] * fx
    return top * (1 - fy) + bot * fy


class AiOcr:
    REC_H = 48
    BATCH = 32

    def __init__(self, langs: list[str], gpu: bool = True):
        ort = import_runtime()
        if ort is None:
            raise RuntimeError("AI 실행 엔진이 없습니다")
        prov = (["DmlExecutionProvider"] if gpu and "DmlExecutionProvider" in ort.get_available_providers() else []) \
            + ["CPUExecutionProvider"]
        opt = ort.SessionOptions()
        opt.log_severity_level = 3
        # 게임과 CPU 를 나눠 쓴다: 스레드 4개, 일 없을 때 돌며 기다리지 않기(기본은 16스레드가 돌아 띠 하나에 CPU ~1초),
        # 크기마다 잡아 두는 메모리 풀 끄기(RAM 630 → 230MB). 띠 읽기 ~85 → ~115ms(2026-10-01)
        opt.intra_op_num_threads, opt.inter_op_num_threads = 4, 1
        opt.enable_cpu_mem_arena = False
        opt.add_session_config_entry("session.intra_op.allow_spinning", "0")

        def load(name):
            return ort.InferenceSession(str(MODEL_DIR / MODELS[name][0]), opt, providers=prov)
        self.det = load("det")
        self.rec, self.chars = {}, {}
        for lg in langs:
            s = load(lg)
            self.rec[lg] = s
            chars = s.get_modelmeta().custom_metadata_map["character"].splitlines()
            self.chars[lg] = ["\0"] + chars + [" "]  # 0 = 빈칸(CTC), 끝 = 띄어쓰기
        self.cache: OrderedDict = OrderedDict()  # (언어, 글자 모양) → 읽은 결과 — 채팅이 밀려 다시 읽어도 같은 줄은 다시 돌리지 않는다
        self.gpu = prov[0] == "DmlExecutionProvider"
        self.want_gpu = gpu

    # ---- 글자 상자(DB) — 채팅 줄은 가로라 줄 띠 · 가로 토막으로 나눈다
    def boxes(self, bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
        h, w = bgr.shape[:2]
        H, W = max(32, math.ceil(h / 32) * 32), max(32, math.ceil(w / 32) * 32)
        x = np.zeros((H, W, 3), np.float32)
        x[:h, :w] = bgr
        x = ((x / 255 - 0.5) / 0.5).transpose(2, 0, 1)[None]
        prob = self.det.run(None, {self.det.get_inputs()[0].name: x})[0][0, 0, :h, :w]
        bm = prob > 0.3
        bm[1:] |= bm[:-1].copy()  # 2×2 넓히기
        bm[:, 1:] |= bm[:, :-1].copy()
        rows = bm.any(1)
        bands, y = [], 0
        while y < h:
            if rows[y]:
                y0 = y
                while y < h and rows[y]:
                    y += 1
                bands.append((y0, y))
            y += 1
        if not bands:
            return []
        med = float(np.median([b - a for a, b in bands]))
        split = []
        for a, b in bands:  # 붙은 두 줄 — 가운데쯤 가장 옅은 가로줄에서 나눈다
            while b - a > med * 1.7:
                prof = prob[a:b].sum(1)
                lo, hi = int(med * 0.6), max(int(med * 0.6) + 1, (b - a) - int(med * 0.6))
                cut = a + lo + int(np.argmin(prof[lo:hi]))
                split.append((a, cut))
                a = cut + 1
            split.append((a, b))
        out = []
        for a, b in split:
            cols = bm[a:b].any(0)
            xs = np.flatnonzero(cols)
            if not len(xs):
                continue
            gap = max(4, int((b - a) * 1.5))  # 낱말 사이는 잇고, 멀리 떨어진 토막(아이콘)은 나눈다
            runs, s0, prev = [], xs[0], xs[0]
            for v in xs[1:]:
                if v - prev > gap:
                    runs.append((s0, prev + 1))
                    s0 = v
                prev = v
            runs.append((s0, prev + 1))
            for x0, x1 in runs:
                if prob[a:b, x0:x1][bm[a:b, x0:x1]].mean() < 0.5 if bm[a:b, x0:x1].any() else True:
                    continue
                bw, bh = x1 - x0, b - a
                d = bw * bh * 1.6 / (2 * (bw + bh))  # 줄인 글자 영역을 다시 넓힌다(unclip)
                out.append((max(0, int(x0 - d)), max(0, int(a - d)), min(w, int(x1 + d)), min(h, int(b + d))))
        return [o for o in out if o[2] - o[0] >= 4 and o[3] - o[1] >= 4]

    # ---- 글자 읽기(CTC) — 글자마다 가로 위치도
    def prepare(self, crops: list[np.ndarray]) -> list[tuple]:
        """조각을 모델 입력으로(언어 모델들이 같이 쓴다). 폭을 320 단위로 묶는다 — GPU(DirectML)는 처음 보는 크기마다
        새로 준비해 느리다."""
        ratios = [c.shape[1] / c.shape[0] for c in crops]
        buckets: dict[int, list[int]] = {}
        for j, r in enumerate(ratios):
            buckets.setdefault(min(8, math.ceil(self.REC_H * r / 320)) * 320, []).append(j)
        out = []
        for W, ids in sorted(buckets.items()):
            for i in range(0, len(ids), self.BATCH):
                idx = ids[i:i + self.BATCH]
                batch = np.zeros((len(idx), 3, self.REC_H, W), np.float32)
                rw = []
                for n, j in enumerate(idx):
                    w = min(W, math.ceil(self.REC_H * ratios[j]))
                    batch[n, :, :, :w] = ((resize(crops[j], self.REC_H, w) / 255 - 0.5) / 0.5).transpose(2, 0, 1)
                    rw.append(w)
                out.append((W, idx, batch, rw))
        return out

    @staticmethod
    def _key(crop: np.ndarray) -> bytes:
        """글자 픽셀(밝은 점)만 잘라 낸 모양 — 상자 여백이 1px 달라도 같은 줄이면 같은 키."""
        ink = crop[..., :3].max(2) > 110
        ys, xs = np.nonzero(ink)
        if not len(ys):
            return b""
        part = np.ascontiguousarray(crop[ys.min():ys.max() + 1, xs.min():xs.max() + 1, :3])
        return hashlib.blake2b(part.tobytes(), digest_size=12).digest() + bytes(str(part.shape), "ascii")

    def recognize(self, lg: str, crops: list[np.ndarray], prepared: list | None = None) -> list[tuple[str, list]]:
        if prepared is None and crops:
            keys = [self._key(c) for c in crops]
            todo = [i for i, k in enumerate(keys) if not k or (lg, k) not in self.cache]
            got = dict(zip(todo, self._run(lg, [crops[i] for i in todo]))) if todo else {}
            out = []
            for i, k in enumerate(keys):
                if i in got:
                    if k:
                        self.cache[(lg, k)] = got[i]
                        if len(self.cache) > 3000:
                            self.cache.popitem(last=False)
                    out.append(got[i])
                else:
                    self.cache.move_to_end((lg, k))
                    out.append(self.cache[(lg, k)])
            return out
        return self._run(lg, crops, prepared)

    def _run(self, lg: str, crops: list[np.ndarray], prepared: list | None = None) -> list[tuple[str, list]]:
        sess, chars = self.rec[lg], self.chars[lg]
        res: list = [("", [])] * len(crops)
        for W, idx, batch, rw in prepared if prepared is not None else self.prepare(crops):
            preds = sess.run(None, {sess.get_inputs()[0].name: batch})[0]
            ids_all = preds.argmax(2)
            step = W / preds.shape[1]  # 한 칸(입력 픽셀)
            for n, j in enumerate(idx):
                out, prev = [], 0
                for t, k in enumerate(ids_all[n]):
                    if k and k != prev and k < len(chars):
                        out.append((chars[k], (t + 0.5) * step * crops[j].shape[1] / rw[n]))
                    prev = k
                res[j] = ("".join(c for c, _ in out), out)
        return res

    @staticmethod
    def words(chars: list[tuple[str, float]], x0: int, k: float, half: float) -> list[list]:
        """글자 → 낱말 [글, 왼, 오른](원래 좌표). 띄어쓰기 · ':' 뒤에서 끊고, 한자 · 한글 · 가나는 글자마다(Windows 와 같게)."""
        out, cur = [], []

        def flush():
            if cur:
                out.append(["".join(c for c, _ in cur), int((x0 + cur[0][1] - half) / k), int((x0 + cur[-1][1] + half) / k)])
                cur.clear()
        for c, x in chars:
            if c == " ":
                flush()
                continue
            if CJK.match(c) and c not in "：（）［］，。！？":
                flush()
                cur.append((c, x))
                flush()
                continue
            cur.append((c, x))
            if c in ":：":
                flush()
        flush()
        return out

    def lines(self, bgra: np.ndarray, scale: float, hints: dict | None = None) -> dict[str, list[dict]]:
        """확대한 화면(bgra) → 켠 언어의 Windows 엔진 자리별 줄 [{t, x, y, h, w}](원래 좌표).
        hints: Windows 엔진이 읽은 줄(엔진 자리별, 원래 좌표) — 주면 그 언어가 보이는 줄만 AI 로 읽는다."""
        bgr = np.ascontiguousarray(bgra[..., :3])
        boxes = self.boxes(bgr)
        texts = []  # 상자마다 Windows 엔진들이 읽은 글
        for x0, y0, x1, y1 in boxes:
            a, b = y0 / scale, y1 / scale
            texts.append({k: " ".join(l["t"] for l in ls if min(b, l["y"] + l["h"]) - max(a, l["y"]) > 0.4 * (b - a))
                          for k, ls in (hints or {}).items()})
        out = {}
        for lg in self.rec:
            # 읽을 화면(새 줄 띠)에 그 언어가 한 줄이라도 보이면 전부, 없으면 그 모델은 건너뛴다. 줄마다 고르면 같은 메시지의
            # 머리 줄(Windows 가 Гільдія 를 TinbAia 로 읽음)을 건너뛰어 이름이 사라졌다(정답 표본 이름 98 → 95%)
            idx = list(range(len(boxes))) if hints is None or any(NEED[lg](x) for x in texts) else []
            crops = [bgr[boxes[i][1]:boxes[i][3], boxes[i][0]:boxes[i][2]] for i in idx]
            got = self.recognize(lg, crops) if crops else []
            ls = []
            for (x0, y0, x1, y1), (text, chars) in zip([boxes[i] for i in idx], got):
                text = text.strip()
                if not text:
                    continue
                half = (y1 - y0) * 0.25
                ws = self.words(chars, x0, scale, half) or [[text, int(x0 / scale), int(x1 / scale)]]
                ls.append({"t": text, "x": int(x0 / scale), "y": int(y0 / scale), "h": int((y1 - y0) / scale), "w": ws})
            out[ENGINE_KEY[lg]] = ls
        return out
