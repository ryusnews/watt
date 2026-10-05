"""광고 가려내기 — 링크·주소 하나로 판단하지 않는다.

한국 서버는 디스코드 주소로 파티·공대를 모으고, 중국 길드는 QQ·위챗 방으로 모은다. 그래서 연락처·디스코드는
'광고'의 근거가 아니라 점수 하나일 뿐이고, 파티·거래·길드 모집 신호가 있으면 점수를 깎는다.
광고 = 현금 거래·외부 사이트·외부 메신저 홍보가 같은 사람에게서 되풀이되는 것(2026-10-01 기록 937건 분석).

점수(AD_AT 이상이면 광고):
  현금(달러·위안·루블·대리) +3 · 디스코드 아닌 사이트 주소 +3 · 외부 메신저 연락처 +2 · 15분 안에 같은 글 3번↑ +2
  홍보 말투(교류방·최고의 커뮤니티·경품…) +2 · 140자↑(한자는 2자) +1
  파티 모집(LFM·来T·ищу…) −4 · 길드 모집 −2 · 게임 안 거래(WTS·WTB) −2, 현금이 붙은 거래는 +1
"""
import re
import time
from collections import deque
from difflib import SequenceMatcher

AD_AT = 4
CJK = re.compile(r"[一-鿿]")
REPEAT_WINDOW = 15 * 60
REPEAT_AT = 3

MONEY = re.compile(r"\$\s?\d|\d\s?\$|\d+\s*(?:usd|rmb|元|руб|eur|€)|paypal|支付宝|代练|代打|代刷|卖金|出金|金币出售|"
                   r"cheap gold|gold (?:for|4) (?:sale|cash)|power ?leveling service|за реал", re.I)
SITE = re.compile(r"\b[\w-]{2,}\s?\.\s?(?:com|net|org|cn|ru|io|gg|top|xyz|shop|vip|cc|me)\b", re.I)
DISCORD = re.compile(r"discord|disc\.gg|디코|디스코드", re.I)
CONTACT = re.compile(r"(?:\b(?:v|vx|wx|q|qq|tg|wechat|telegram|whatsapp|line|kakao|vk)|微信|威信|扣扣)\s*群?\s*号?\s*[:：]\s*"
                     r"([A-Za-z0-9_\-]{5,})", re.I)
PROMO = re.compile(r"交流群|老友群|进裙|加群|进群|入群|加微信|加威信|公众号|直播间|抖音|best community|лучшее сообщество|розыгрыш|giveaway|"
                   r"free (?:gold|items)|join (?:our|my) (?:group|channel)", re.I)
PARTY = re.compile(r"\bLF\d?M\b|\bLFG\b|\bLF\b|\bLF\s?\d|looking for (?:more|tank|heal|dps|group)|need (?:tank|heal|dps)|"
                   r"来\s?[个TN奶]|求组|进组|缺\s?[TN奶]|任务队|速刷|车头|\b\d\s?=\s?\d\b|"
                   r"ищу|ищем|нужен (?:танк|хил)|в пати|го в|"
                   r"buscamos|busco|suche|cherche", re.I)
GUILD = re.compile(r"recruit|guild|roster|raid team|招募|招收|公会|入会|工会|набор в гильди|ищем в гильди|рекрут|recluta|rekrut|"
                   r"길드|공대", re.I)
TRADE = re.compile(r"\bWT[SBT]\b|\bselling\b|\bbuying\b|\bprice\b|продам|куплю", re.I)


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s).lower()


class AdFilter:
    """보낸 사람별 최근 글을 기억해 되풀이를 센다."""

    def __init__(self):
        self.recent: deque[tuple[float, str, str]] = deque(maxlen=300)  # (시각, 이름, 본문 키)
        self.known: deque[str] = deque(maxlen=60)  # 광고로 본 본문 — 다음부터는 처음 나와도 바로 광고
        self.marks: deque[str] = deque(maxlen=60)  # 광고에 있던 연락처 ID·사이트 — 잘려 읽힌 같은 광고도 알아본다

    def repeats(self, name: str, key: str, now: float) -> int:
        n = 0
        for t, who, k in self.recent:
            if now - t <= REPEAT_WINDOW and (who == name or k == key) and \
                    (k == key or SequenceMatcher(None, k[:80], key[:80]).ratio() >= 0.8):
                n += 1
        return n

    def check(self, name: str, body: str, now: float | None = None) -> dict:
        """{'kind': ad·guild·trade·party·chat, 'score', 'why'}"""
        now = time.time() if now is None else now
        key = norm(body)
        rep = self.repeats(norm(name), key, now) + 1
        self.recent.append((now, norm(name), key))
        why, score = [], 0

        def add(cond, pts, label):
            nonlocal score
            if cond:
                score += pts
                why.append(label)

        add(MONEY.search(body), 3, "현금")
        add(any(not DISCORD.match(s) for s in SITE.findall(body)), 3, "사이트")  # discord.gg 는 빼고
        add(CONTACT.search(body), 2, "연락처")
        add(rep >= REPEAT_AT, 2, f"반복 {rep}")
        add(PROMO.search(body), 2, "홍보")
        add(len(body) + len(CJK.findall(body)) > 140, 1, "긴 글")  # 한자는 두 글자로 센다
        party, guild, trade = PARTY.search(body), GUILD.search(body), TRADE.search(body)
        add(party, -4, "파티")
        add(guild, -2, "길드")
        add(trade and not MONEY.search(body), -2, "거래")
        add(trade and MONEY.search(body), 1, "현금 거래")  # WTS gold … paypal
        marks = [norm(m) for m in CONTACT.findall(body) + [s for s in SITE.findall(body) if not DISCORD.match(s)]]
        known = any(m in self.marks for m in marks) or             any(key[:40] and SequenceMatcher(None, k[:80], key[:80]).ratio() >= 0.8 for k in self.known)
        if score >= AD_AT or (known and not party):
            kind = "ad"
            if known:
                why.append("본 광고")
            else:
                self.known.append(key)
            self.marks.extend(m for m in marks if m not in self.marks)
        else:
            kind = "party" if party else "guild" if guild else "trade" if trade else "chat"
        return {"kind": kind, "score": score, "why": why}
