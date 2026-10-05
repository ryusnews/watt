"""HTTPS — 보안 프로그램이 HTTPS 를 가로채 약한 키(RSA 1024)로 다시 서명하는 PC 에서도 받기 · 업데이트가 되게.

PC방(2026-10-05): 업데이트 확인이 'CERTIFICATE_VERIFY_FAILED — EE certificate key too weak'. 그 PC 의 보안 프로그램이
자기 인증서(Windows 신뢰 저장소에 들어 있다 — 브라우저 · Ollama 는 된다)로 다시 서명하는데, 키가 짧아 Python(OpenSSL
보안 수준 2)만 거절했다(#124). 그 오류일 때만 보안 수준 1(RSA 1024 허용)로 한 번 더 — 인증서 사슬 · 호스트 이름 검사는
그대로 한다(검사를 끄지 않는다). 한 번 그런 호스트는 기억해 다음부터 바로.
watt 를 불러오면 urllib 의 기본 opener 로 깔린다(context 없이 부르는 urlopen 모두).
"""
import http.client
import logging
import ssl
import urllib.error
import urllib.request

log = logging.getLogger("watt")
_weak_hosts: set[str] = set()


def _weak_key(e: BaseException) -> bool:
    reason = getattr(e, "reason", e)
    return isinstance(reason, ssl.SSLCertVerificationError) and "too weak" in str(reason)


class _Https(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=ssl.create_default_context())
        self._weak = ssl.create_default_context()
        self._weak.set_ciphers("DEFAULT@SECLEVEL=1")

    def https_open(self, req):
        host = req.host
        if host in _weak_hosts:
            return self.do_open(http.client.HTTPSConnection, req, context=self._weak)
        try:
            return self.do_open(http.client.HTTPSConnection, req, context=self._context)
        except urllib.error.URLError as e:
            if not _weak_key(e):
                raise
            log.warning("weak TLS key from %s (HTTPS inspection?) — retry at security level 1", host)
            _weak_hosts.add(host)
            return self.do_open(http.client.HTTPSConnection, req, context=self._weak)


def install() -> None:
    urllib.request.install_opener(urllib.request.build_opener(_Https()))
