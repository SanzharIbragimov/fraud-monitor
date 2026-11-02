"""Демонстрационные сценарии раздела 4 ТЗ: последовательности событий
на вымышленных клиентах. Каждый вызов создаёт новых клиентов, чтобы
сценарии не влияли друг на друга.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta

from .engine import Engine
from .models import Channel, Event, EventType, Payment, Session

_seq = itertools.count(1)
SCENARIOS = {
    "legit": "Обычный перевод знакомому получателю",
    "social_engineering": "Социальная инженерия: звонок, новый получатель, сумма в 10 раз выше обычной",
    "account_takeover": "Захват учётной записи: эмулятор, IP другой страны, подбор пароля, смена пароля, перевод",
    "compromised_cards": "Скомпрометированные карты: 5 карт на одном устройстве и пробные платежи",
    "droppers": "Дроппер: деньги от 4 человек и быстрый вывод на новую карту",
    "multiaccount": "Мультиаккаунты: три учётные записи на одном устройстве получают бонус",
    "stoplist": "Перевод получателю из стоп-листа (вымышленная запись национального списка)",
}


class Script:
    def __init__(self, eng: Engine, jur: str = "KZ") -> None:
        self.eng, self.jur = eng, jur
        self.n = next(_seq)
        self.t = datetime.now().replace(microsecond=0) - timedelta(days=20)
        self.steps: list[dict] = []
        self.cur = eng.jur[jur]["currency"]

    def uid(self, name: str) -> str:
        return f"demo{self.n}_{name}"

    def ev(self, user: str, et: EventType, ts: datetime, device: str, record: bool = True, **kw) -> dict:
        sess = kw.pop("session", {})
        e = Event(event_id=f"DEMO{self.n}-{len(self.steps) + 1}-{user}", event_type=et, ts=ts, jurisdiction=self.jur,
                  user_id=user, account_created=kw.pop("created", ts - timedelta(days=400)),
                  session=Session(device_id=device, ip_country=sess.pop("ip_country", self.jur),
                                  lat=sess.pop("lat", 51.13), lon=sess.pop("lon", 71.43), **sess), **kw)
        d = self.eng.score(e)
        if record:
            self.steps.append({"event": e.model_dump(mode="json"), "decision": d.model_dump(mode="json")})
        return d.model_dump(mode="json")

    def pay(self, user: str, ts: datetime, device: str, amount: float, rcp: str, channel: Channel = Channel.p2p,
            rtype: str = "wallet", card: str | None = None, record: bool = True, rcp_user: str | None = None,
            **kw) -> dict:
        return self.ev(user, EventType.payment, ts, device, record=record,
                       payment=Payment(channel=channel, amount=amount, currency=self.cur, recipient_id=rcp,
                                       recipient_type=rtype, recipient_user_id=rcp_user,
                                       card_hash=card or f"card_{user}", card_bin="440043",
                                       card_last4="1234"), **kw)

    def history(self, user: str, device: str, contacts: list[str], usual: float = 8000) -> None:
        """20 дней обычной истории клиента (не попадает в вывод сценария)."""
        self.ev(user, EventType.login, self.t, device, record=False, login_success=True)
        for i in range(12):
            self.pay(user, self.t + timedelta(days=i * 1.5, hours=2), device, usual * (0.7 + 0.05 * i),
                     contacts[i % len(contacts)], record=False)


def run(eng: Engine, name: str) -> dict:
    if name not in SCENARIOS:
        raise KeyError(name)
    s = Script(eng)
    now = datetime.now().replace(microsecond=0)
    u, dev = s.uid("client"), s.uid("phone")
    friends = [f"w_{s.uid('friend')}{i}" for i in range(3)]

    if name == "legit":
        s.history(u, dev, friends)
        s.pay(u, now, dev, 9000, friends[0])
    elif name == "social_engineering":
        s.history(u, dev, friends)
        s.pay(u, now, dev, 95000, f"w_{s.uid('unknown')}", session={"on_call": True})
        s.pay(u, now + timedelta(minutes=6), dev, 120000, f"w_{s.uid('unknown2')}", session={"on_call": True})
    elif name == "account_takeover":
        s.history(u, dev, friends)
        fdev = s.uid("emulator")
        foreign = {"emulator": True, "ip_country": "NL", "lat": 52.37, "lon": 4.9}
        for i in range(3):
            s.ev(u, EventType.login, now + timedelta(seconds=15 * i), fdev, login_success=False, session=dict(foreign))
        s.ev(u, EventType.login, now + timedelta(minutes=1), fdev, login_success=True, session=dict(foreign))
        s.ev(u, EventType.profile_change, now + timedelta(minutes=3), fdev, changed_field="password", session=dict(foreign))
        s.pay(u, now + timedelta(minutes=8), fdev, 60000, f"w_{s.uid('mule')}", session=dict(foreign))
    elif name == "compromised_cards":
        fdev = s.uid("farm_phone")
        created = now - timedelta(hours=3)
        for i in range(5):
            card = f"stolen_{s.n}_{i}"
            s.ev(u, EventType.card_bind, now + timedelta(minutes=2 * i), fdev, card_hash=card, card_bin="404000",
                 created=created)
            s.pay(u, now + timedelta(minutes=2 * i, seconds=30), fdev, 300, "svc_001", channel=Channel.service,
                  rtype="merchant", card=card, created=created)
    elif name == "droppers":
        drop, ddev = s.uid("drop"), s.uid("drop_phone")
        created = now - timedelta(days=5)
        s.ev(drop, EventType.login, now - timedelta(hours=1), ddev, login_success=True, created=created, record=False)
        for i in range(4):
            v = s.uid(f"victim{i}")
            s.history(v, s.uid(f"victim{i}_phone"), friends)
            s.pay(v, now + timedelta(minutes=5 * i), s.uid(f"victim{i}_phone"), 50000, f"w_{drop}", rcp_user=drop)
        s.pay(drop, now + timedelta(minutes=30), ddev, 190000, f"c_out_{s.n}", channel=Channel.withdrawal,
              rtype="card", created=created)
    elif name == "multiaccount":
        fdev = s.uid("farm_phone")
        for i in range(3):
            acc = s.uid(f"acc{i}")
            t = now + timedelta(minutes=10 * i)
            s.ev(acc, EventType.login, t, fdev, login_success=True, created=t)
            s.ev(acc, EventType.bonus_claim, t + timedelta(minutes=2), fdev, promo_id="WELCOME", created=t)
    elif name == "stoplist":
        s.history(u, dev, friends)
        s.pay(u, now, dev, 15000, "w_blk_KZ_0")
    last = s.steps[-1]["decision"]
    return {"scenario": name, "title": SCENARIOS[name], "final": last, "steps": s.steps}
