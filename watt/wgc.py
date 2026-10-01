"""Windows 그래픽 캡처(Windows.Graphics.Capture) — 게임 창 출력을 GPU 에서 바로 받는다(#45).

GDI 로 화면을 읽으면 GPU 가 그린 화면을 거꾸로 읽어 와 채팅창 크기에도 ~190ms, 가려졌을 때 쓰는 PrintWindow 는
~780ms 가 걸렸다(2026-10-01, RTX 5080). 이 방식은 게임 창이 그려질 때마다 GPU 텍스처로 받아 두고, 필요할 때
채팅 영역만 작은 텍스처로 복사해 읽는다. 다른 창이 가려도 게임 화면이다. 게임에 끼어들지 않는다(OBS '창 캡처'와 같은 방식).

추가 구성 요소 없이 ctypes 로 COM 을 직접 부른다(pywinrt 에는 HWND · D3D 장치를 넘기는 연결이 없다).
Windows 10 1903+ . Windows 11 은 캡처 중 노란 테두리를 끈다(IsBorderRequired = false).
"""
import ctypes
import ctypes.wintypes as wt
import threading

import numpy as np

ole32 = ctypes.windll.ole32
combase = ctypes.windll.combase
d3d11 = ctypes.windll.d3d11
user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
HRESULT = ctypes.c_long
PTR = ctypes.c_void_p


class GUID(ctypes.Structure):
    _fields_ = [("a", wt.DWORD), ("b", wt.WORD), ("c", wt.WORD), ("d", ctypes.c_ubyte * 8)]

    def __init__(self, s: str):
        super().__init__()
        ole32.CLSIDFromString(ctypes.c_wchar_p("{" + s + "}"), ctypes.byref(self))


IID_IDXGIDevice = GUID("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
IID_IInspectable = GUID("AF86E2E0-B12D-4C6A-9C5A-D7AA65101E90")
IID_IGraphicsCaptureItemInterop = GUID("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356")
IID_IGraphicsCaptureItem = GUID("79C3F95B-31F7-4EC2-A464-632EF5D30760")
IID_IDirect3D11CaptureFramePoolStatics2 = GUID("589B103F-6BBC-5DF5-A991-02E28B3B66D5")
IID_IGraphicsCaptureSession2 = GUID("2C39AE40-7D2E-5044-804E-8B6799D4CF9E")
IID_IGraphicsCaptureSession3 = GUID("F2CDD966-22AE-5EA1-9596-3A289344C3BE")
IID_IClosable = GUID("30D5A829-7FA4-4026-83BB-D75BAE4EA99E")
IID_IDirect3DDxgiInterfaceAccess = GUID("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1")
IID_ID3D11Texture2D = GUID("6f15aaf2-d208-4e89-9ab4-489535d34f9c")


def _call(obj, index: int, restype, *args):
    """COM 메서드 호출 — vtable[index](this, *args)."""
    vtbl = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(PTR))[0], ctypes.POINTER(PTR))
    types = [PTR if type(a).__name__ == "CArgObject" or a is None else type(a) for a in args]  # byref(...) · None → 포인터
    fn = ctypes.WINFUNCTYPE(restype, PTR, *types)(vtbl[index])
    return fn(obj, *args)


def _hr(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} 실패 0x{hr & 0xFFFFFFFF:08X}")


def _release(obj) -> None:
    if obj:
        _call(obj, 2, wt.ULONG)


def _qi(obj, iid: GUID) -> PTR:
    out = PTR()
    _hr(_call(obj, 0, HRESULT, ctypes.byref(iid), ctypes.byref(out)), "QueryInterface")
    return out


def _factory(name: str, iid: GUID) -> PTR:
    hs = PTR()
    _hr(combase.WindowsCreateString(ctypes.c_wchar_p(name), len(name), ctypes.byref(hs)), "WindowsCreateString")
    out = PTR()
    try:
        _hr(combase.RoGetActivationFactory(hs, ctypes.byref(iid), ctypes.byref(out)), f"RoGetActivationFactory({name})")
    finally:
        combase.WindowsDeleteString(hs)
    return out


class SizeInt32(ctypes.Structure):
    _fields_ = [("w", ctypes.c_int32), ("h", ctypes.c_int32)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [("Width", wt.UINT), ("Height", wt.UINT), ("MipLevels", wt.UINT), ("ArraySize", wt.UINT),
                ("Format", wt.UINT), ("SampleCount", wt.UINT), ("SampleQuality", wt.UINT), ("Usage", wt.UINT),
                ("BindFlags", wt.UINT), ("CPUAccessFlags", wt.UINT), ("MiscFlags", wt.UINT)]


class D3D11_BOX(ctypes.Structure):
    _fields_ = [("left", wt.UINT), ("top", wt.UINT), ("front", wt.UINT), ("right", wt.UINT), ("bottom", wt.UINT), ("back", wt.UINT)]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", PTR), ("RowPitch", wt.UINT), ("DepthPitch", wt.UINT)]


def supported() -> bool:
    try:
        _release(_factory("Windows.Graphics.Capture.GraphicsCaptureItem", IID_IGraphicsCaptureItemInterop))
        return True
    except OSError:
        return False


class WindowCapture:
    """게임 창 하나를 계속 받아 두고, grab(x, y, w, h — 창 클라이언트 좌표)으로 그 부분만 BGRA 로."""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.lock = threading.Lock()
        combase.RoInitialize(1)  # MTA(이미 되어 있으면 그대로)
        self.device, self.context = PTR(), PTR()
        _hr(d3d11.D3D11CreateDevice(None, 1, None, 0x20, None, 0, 7, ctypes.byref(self.device), None,
                                    ctypes.byref(self.context)), "D3D11CreateDevice")  # HARDWARE, BGRA_SUPPORT
        dxgi = _qi(self.device, IID_IDXGIDevice)
        self.winrt_device = PTR()
        _hr(d3d11.CreateDirect3D11DeviceFromDXGIDevice(dxgi, ctypes.byref(self.winrt_device)), "CreateDirect3D11DeviceFromDXGIDevice")
        _release(dxgi)
        interop = _factory("Windows.Graphics.Capture.GraphicsCaptureItem", IID_IGraphicsCaptureItemInterop)
        self.item = PTR()
        _hr(_call(interop, 3, HRESULT, wt.HWND(hwnd), ctypes.byref(IID_IGraphicsCaptureItem), ctypes.byref(self.item)),
            "CreateForWindow")
        _release(interop)
        size = SizeInt32()
        _hr(_call(self.item, 7, HRESULT, ctypes.byref(size)), "Size")  # IGraphicsCaptureItem: DisplayName 6, Size 7
        self.size = (size.w, size.h)
        statics = _factory("Windows.Graphics.Capture.Direct3D11CaptureFramePool", IID_IDirect3D11CaptureFramePoolStatics2)
        self.pool = PTR()
        fn = ctypes.WINFUNCTYPE(HRESULT, PTR, PTR, ctypes.c_int32, ctypes.c_int32, SizeInt32, ctypes.POINTER(PTR))(
            ctypes.cast(ctypes.cast(statics, ctypes.POINTER(PTR))[0], ctypes.POINTER(PTR))[6])
        _hr(fn(statics, self.winrt_device, 87, 2, size, ctypes.byref(self.pool)), "CreateFreeThreaded")  # B8G8R8A8UIntNormalized
        _release(statics)
        self.session = PTR()
        _hr(_call(self.pool, 10, HRESULT, self.item, ctypes.byref(self.session)), "CreateCaptureSession")
        for iid, idx, what in ((IID_IGraphicsCaptureSession2, 7, "IsCursorCaptureEnabled"),
                               (IID_IGraphicsCaptureSession3, 7, "IsBorderRequired")):
            try:  # 커서 · 노란 테두리 끄기(없는 Windows 면 그대로)
                s = _qi(self.session, iid)
                _call(s, idx, HRESULT, ctypes.c_bool(False))
                _release(s)
            except OSError:
                pass
        _hr(_call(self.session, 6, HRESULT), "StartCapture")
        self.staging, self.staging_size, self.last = PTR(), (0, 0), None

    def _latest_frame(self):
        """쌓인 프레임 중 가장 새것(나머지는 돌려준다)."""
        frame = None
        while True:
            f = PTR()
            _hr(_call(self.pool, 7, HRESULT, ctypes.byref(f)), "TryGetNextFrame")
            if not f:
                return frame
            if frame:
                self._close(frame)
            frame = f

    @staticmethod
    def _close(frame) -> None:
        try:
            c = _qi(frame, IID_IClosable)
            _call(c, 6, HRESULT)
            _release(c)
        finally:
            _release(frame)

    def _frame_offset(self) -> tuple[int, int]:
        """캡처는 창 바깥 테두리(DWM 범위) 기준 — 클라이언트 좌표로 맞춘다."""
        rect = wt.RECT()
        dwmapi.DwmGetWindowAttribute(wt.HWND(self.hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect))  # EXTENDED_FRAME_BOUNDS
        pt = wt.POINT(0, 0)
        user32.ClientToScreen(wt.HWND(self.hwnd), ctypes.byref(pt))
        return pt.x - rect.left, pt.y - rect.top

    def grab(self, x: int, y: int, w: int, h: int) -> np.ndarray | None:
        """창 클라이언트 (x, y, w, h) → BGRA. 아직 받은 프레임이 없거나 창 크기가 바뀌었으면 None."""
        with self.lock:
            frame = self._latest_frame()
            if frame:
                if self.last:
                    self._close(self.last)
                self.last = frame
            frame = self.last
            if not frame:
                return None
            surface = PTR()
            _hr(_call(frame, 6, HRESULT, ctypes.byref(surface)), "Surface")
            access = _qi(surface, IID_IDirect3DDxgiInterfaceAccess)
            tex = PTR()
            _hr(_call(access, 3, HRESULT, ctypes.byref(IID_ID3D11Texture2D), ctypes.byref(tex)), "GetInterface")
            _release(access)
            _release(surface)
            try:
                desc = D3D11_TEXTURE2D_DESC()
                _call(tex, 10, None, ctypes.byref(desc))
                ox, oy = self._frame_offset()
                left, top = x + ox, y + oy
                if left < 0 or top < 0 or left + w > desc.Width or top + h > desc.Height:
                    return None
                if self.staging_size != (w, h):
                    _release(self.staging)
                    sd = D3D11_TEXTURE2D_DESC(w, h, 1, 1, desc.Format, 1, 0, 3, 0, 0x20000, 0)  # STAGING, CPU_READ
                    self.staging = PTR()
                    _hr(_call(self.device, 5, HRESULT, ctypes.byref(sd), None, ctypes.byref(self.staging)), "CreateTexture2D")
                    self.staging_size = (w, h)
                box = D3D11_BOX(left, top, 0, left + w, top + h, 1)
                _call(self.context, 46, None, self.staging, wt.UINT(0), wt.UINT(0), wt.UINT(0), wt.UINT(0), tex, wt.UINT(0),
                      ctypes.byref(box))  # CopySubresourceRegion — 채팅 영역만
                m = D3D11_MAPPED_SUBRESOURCE()
                _hr(_call(self.context, 14, HRESULT, self.staging, wt.UINT(0), wt.UINT(1), wt.UINT(0), ctypes.byref(m)), "Map")
                try:
                    buf = (ctypes.c_ubyte * (m.RowPitch * h)).from_address(m.pData)
                    img = np.frombuffer(buf, np.uint8).reshape(h, m.RowPitch)[:, :w * 4].reshape(h, w, 4).copy()
                finally:
                    _call(self.context, 15, None, self.staging, wt.UINT(0))  # Unmap
                img[..., 3] = 255
                return img
            finally:
                _release(tex)

    def close(self) -> None:
        with self.lock:
            if self.last:
                self._close(self.last)
                self.last = None
            for obj in ("session", "pool"):
                p = getattr(self, obj)
                if p:
                    try:
                        c = _qi(p, IID_IClosable)
                        _call(c, 6, HRESULT)
                        _release(c)
                    except OSError:
                        pass
                    _release(p)
                    setattr(self, obj, PTR())
            for obj in ("staging", "item", "winrt_device", "context", "device"):
                _release(getattr(self, obj))
                setattr(self, obj, PTR())
