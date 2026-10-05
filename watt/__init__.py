"""WATT — 게임 채팅 실시간 통역(비공식). 런처(설정 마법사·대시보드) + 통역 창 + 보내기 입력창."""
APP_NAME = "WATT"
APP_FULL = "WoW AI Translation Tool"
VERSION = "0.2.33"

from . import net as _net  # noqa: E402 — 보안 프로그램이 약한 키로 HTTPS 를 가로채는 PC(PC방)에서도 받기 · 업데이트(#124)

_net.install()
