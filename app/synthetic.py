"""Генератор вымышленного потока событий платёжного приложения.

ВСЕ ДАННЫЕ ВЫМЫШЛЕННЫЕ. Это не данные Monexsol и не статистика рынка.
В поток заложены шесть сценариев мошенничества из раздела 4 ТЗ (с разметкой)
и «шум» — легитимное поведение, похожее на мошенническое:
крупные переводы новым получателям, смена телефона, поездки за границу,
одно устройство на семью, сбор денег «на подарок», ночные платежи,
а также ошибки данных: нет устройства, неверная валюта.

Доля мошенничества в синтетике намеренно завышена (~1–2% событий), чтобы
каждый сценарий был представлен; в реальном потоке она на порядки ниже.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import Channel, Event, EventType, Payment, Session

START = datetime(2026, 10, 1)
COUNTRIES = {  # код: (доля клиентов, валюта, курс к USD, город (lat, lon))
    "KZ": (0.6, "KZT", 500, (51.13, 71.43)),
    "UZ": (0.25, "UZS", 12700, (41.31, 69.28)),
    "KG": (0.15, "KGS", 87, (42.87, 74.59)),
}
FOREIGN = [("TR", 41.0, 28.9), ("AE", 25.2, 55.3), ("DE", 52.5, 13.4), ("NL", 52.4, 4.9)]


@dataclass
class Row:
    event: Event
    label: int = 0
    scenario: str = "legit"
    incident: str | None = None
    note: str = ""


@dataclass
class User:
    uid: str
    country: str
    device: str
    created: datetime
    usual_usd: float
    rate_per_day: float
    contacts: list = field(default_factory=list)
    cards: list = field(default_factory=list)
    lat: float = 0.0
    lon: float = 0.0


class Gen:
    def __init__(self, seed: int = 7, users: int = 2500, days: int = 14) -> None:
        self.r = random.Random(seed)
        self.days = days
        self.rows: list[Row] = []
        self.n = 0
        self.users: list[User] = []
        self.merchants = [f"m_{i:04d}" for i in range(300)]
        self.services = [f"svc_{i:03d}" for i in range(60)]
        self.stoplists: list[dict] = []
        self.seed = seed
        for i in range(users):
            self.users.append(self._new_user(f"U{seed}{i:05d}", START - timedelta(days=self.r.randint(20, 700))))
        for u in self.users:
            same = [v for v in self.users if v.country == u.country]
            u.contacts = [v.uid for v in self.r.sample(same, k=min(len(same), self.r.randint(3, 8))) if v.uid != u.uid]

    # ---------- вспомогательное ----------
    def _new_user(self, uid: str, created: datetime, country: str | None = None) -> User:
        r = self.r
        if country is None:
            x, acc = r.random(), 0.0
            for c, (share, *_rest) in COUNTRIES.items():
                acc += share
                if x <= acc:
                    country = c
                    break
            country = country or "KZ"
        lat, lon = COUNTRIES[country][3]
        return User(uid=uid, country=country, device=f"d_{uid}", created=created,
                    usual_usd=round(r.lognormvariate(3.2, 0.7), 2), rate_per_day=r.uniform(0.6, 3.0),
                    cards=[f"card_{uid}_1"], lat=lat + r.uniform(-0.15, 0.15), lon=lon + r.uniform(-0.15, 0.15))

    def _id(self) -> str:
        self.n += 1
        return f"E{self.seed}{self.n:07d}"

    def _ts(self, day: int, night_ok: bool = False) -> datetime:
        h = self.r.choice(range(0, 24)) if night_ok else self.r.choices(range(7, 24), k=1)[0]
        return START + timedelta(days=day, hours=h, minutes=self.r.randint(0, 59), seconds=self.r.randint(0, 59))

    def _session(self, u: User, device: str | None = None, **kw) -> Session:
        return Session(device_id=device or u.device, os=self.r.choice(["android", "ios"]),
                       ip=f"10.{self.r.randint(0, 255)}.{self.r.randint(0, 255)}.{self.r.randint(1, 254)}",
                       ip_country=u.country, lat=u.lat, lon=u.lon, **kw)

    def _amount(self, u: User, mult: float = 1.0) -> float:
        usd = max(0.5, self.r.lognormvariate(0, 0.45) * u.usual_usd * mult)
        rate = COUNTRIES[u.country][2]
        return float(round(usd * rate, -1 if rate > 100 else 0) or 1)

    def _pay(self, u: User, ts: datetime, channel: Channel, rcp: str | None, rtype: str, amount: float,
             session: Session | None = None, rcp_user: str | None = None, card: str | None = None,
             label: int = 0, scenario: str = "legit", incident: str | None = None, note: str = "") -> Row:
        cur = COUNTRIES[u.country][1]
        ev = Event(event_id=self._id(), event_type=EventType.payment, ts=ts, jurisdiction=u.country, user_id=u.uid,
                   account_created=u.created, session=session or self._session(u),
                   payment=Payment(channel=channel, amount=amount, currency=cur, recipient_id=rcp,
                                   recipient_type=rtype, recipient_user_id=rcp_user,
                                   card_hash=card or u.cards[0], card_bin="440043", card_last4="1234"))
        row = Row(ev, label, scenario, incident, note)
        self.rows.append(row)
        return row

    def _event(self, u: User, ts: datetime, et: EventType, session: Session | None = None, label: int = 0,
               scenario: str = "legit", incident: str | None = None, note: str = "", **kw) -> Row:
        ev = Event(event_id=self._id(), event_type=et, ts=ts, jurisdiction=u.country, user_id=u.uid,
                   account_created=u.created, session=session or self._session(u), **kw)
        row = Row(ev, label, scenario, incident, note)
        self.rows.append(row)
        return row

    def _wallet(self, uid: str) -> str:
        return f"w_{uid}"

    # ---------- легитимное поведение ----------
    def legit(self) -> None:
        r = self.r
        by_id = {u.uid: u for u in self.users}
        shared = r.sample(self.users, k=int(len(self.users) * 0.03))
        for u in shared:  # одно устройство на семью (2 учётные записи)
            same = [v for v in self.users if v.country == u.country and v.uid != u.uid]
            r.choice(same).device = u.device
        for day in range(self.days):
            for u in self.users:
                for _ in range(self._poisson(u.rate_per_day)):
                    ts = self._ts(day, night_ok=r.random() < 0.05)
                    if r.random() < 0.3:
                        self._event(u, ts - timedelta(minutes=1), EventType.login, login_success=True)
                    x = r.random()
                    if x < 0.4 and u.contacts:
                        c = r.choice(u.contacts)
                        self._pay(u, ts, Channel.p2p, self._wallet(c), "wallet", self._amount(u), rcp_user=c)
                    elif x < 0.65:
                        self._pay(u, ts, Channel.service, r.choice(self.services), "merchant", self._amount(u, 0.6))
                    elif x < 0.85:
                        self._pay(u, ts, Channel.qr, r.choice(self.merchants), "merchant", self._amount(u, 0.8))
                    else:
                        self._pay(u, ts, Channel.merchant, r.choice(self.merchants), "merchant", self._amount(u))
                if r.random() < 0.004:  # редкий неуспешный вход (опечатка в пароле)
                    ts = self._ts(day)
                    self._event(u, ts, EventType.login, login_success=False, note="опечатка в пароле")
                    self._event(u, ts + timedelta(seconds=30), EventType.login, login_success=True)

        n = len(self.users)
        # крупный перевод новому получателю (аренда, покупка) — похоже на соц. инженерию
        for u in r.sample(self.users, k=int(n * 0.03)):
            ts = self._ts(r.randrange(self.days))
            other = r.choice(self.users)
            self._pay(u, ts, Channel.p2p, self._wallet(other.uid), "wallet", self._amount(u, r.uniform(5, 15)),
                      rcp_user=other.uid, session=self._session(u, on_call=r.random() < 0.15),
                      note="легитимный крупный перевод новому получателю")
        # смена телефона: новое устройство, смена номера/устройства, затем обычные платежи
        for u in r.sample(self.users, k=int(n * 0.03)):
            day = r.randrange(1, self.days)
            ts = self._ts(day)
            u.device = f"d_{u.uid}_new"
            self._event(u, ts, EventType.login, login_success=True, note="новый телефон")
            self._event(u, ts + timedelta(minutes=3), EventType.profile_change, changed_field="device")
            if r.random() < 0.3:
                self._event(u, ts + timedelta(minutes=5), EventType.profile_change, changed_field="phone",
                            note="сменил номер вместе с телефоном")
            if u.contacts:
                c = r.choice(u.contacts)
                self._pay(u, ts + timedelta(hours=r.uniform(0.5, 30)), Channel.p2p, self._wallet(c), "wallet",
                          self._amount(u), rcp_user=c, note="платёж после смены телефона")
        # поездки за границу
        for u in r.sample(self.users, k=int(n * 0.02)):
            code, lat, lon = r.choice(FOREIGN)
            day = r.randrange(self.days - 2)
            for k in range(3):
                ts = self._ts(day + k)
                s = self._session(u)
                s.ip_country, s.lat, s.lon = code, lat, lon
                self._pay(u, ts + timedelta(days=0), Channel.merchant, r.choice(self.merchants), "merchant",
                          self._amount(u), session=s, note="поездка за границу")
        # сбор денег «на подарок»: много входящих, затем перевод дальше
        for u in r.sample(self.users, k=max(3, int(n * 0.004))):
            day = r.randrange(self.days)
            ts = self._ts(day)
            friends = [v for v in self.users if v.country == u.country and v.uid != u.uid]
            for i, v in enumerate(r.sample(friends, k=r.randint(5, 9))):
                self._pay(v, ts + timedelta(minutes=5 * i), Channel.p2p, self._wallet(u.uid), "wallet",
                          self._amount(v), rcp_user=u.uid, note="скидываемся на подарок")
            other = r.choice(friends)
            self._pay(u, ts + timedelta(minutes=70), Channel.p2p, self._wallet(other.uid), "wallet",
                      self._amount(u, 6), rcp_user=other.uid, note="перевод собранных денег")
        # новые клиенты: регистрация, привязка карты, бонус
        for i in range(int(n * 0.04)):
            day = r.randrange(self.days)
            ts = self._ts(day)
            u = self._new_user(f"N{self.seed}{i:05d}", ts)
            self.users.append(u)
            self._event(u, ts, EventType.login, login_success=True)
            self._event(u, ts + timedelta(minutes=2), EventType.card_bind, card_hash=u.cards[0], card_bin="440043")
            self._event(u, ts + timedelta(minutes=4), EventType.bonus_claim, promo_id="WELCOME")
            self._pay(u, ts + timedelta(minutes=20), Channel.service, r.choice(self.services), "merchant",
                      self._amount(u, 0.6))
        # вторая карта у существующих клиентов
        for u in r.sample(self.users[:n], k=int(n * 0.02)):
            ts = self._ts(r.randrange(self.days))
            u.cards.append(f"card_{u.uid}_2")
            self._event(u, ts, EventType.card_bind, card_hash=u.cards[-1], card_bin="510000")
        # ошибки данных
        for u in r.sample(self.users[:n], k=int(n * 0.01)):
            ts = self._ts(r.randrange(self.days))
            s = self._session(u)
            s.device_id = None
            self._pay(u, ts, Channel.service, r.choice(self.services), "merchant", self._amount(u, 0.6), session=s,
                      note="ошибка данных: нет устройства")
        _ = by_id

    @staticmethod
    def _poisson_knuth(r: random.Random, lam: float) -> int:
        import math
        L, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= r.random()
            if p <= L:
                return k
            k += 1

    def _poisson(self, lam: float) -> int:
        return self._poisson_knuth(self.r, lam)

    # ---------- мошенничество ----------
    def fraud(self, incidents_per_scenario: int = 25) -> None:
        r = self.r
        base = self.users[: len(self.users)]
        k = incidents_per_scenario

        # дропперы: сеть подставных кошельков по странам
        drops: dict[str, list[User]] = {}
        for c in COUNTRIES:
            drops[c] = []
            for i in range(max(4, k // 3)):
                d = self._new_user(f"D{self.seed}{c}{i:03d}", START - timedelta(days=r.randint(2, 20)), c)
                d.rate_per_day = 0
                drops[c].append(d)
                self.users.append(d)

        def victims(n: int) -> list[User]:
            pool = [u for u in base if u.uid.startswith("U")]
            return r.sample(pool, k=n)

        # 1. социальная инженерия
        for i, v in enumerate(victims(k)):
            inc = f"SE-{i:03d}"
            day = r.randrange(1, self.days)
            ts = self._ts(day)
            drop = r.choice(drops[v.country])
            on_call = r.random() < 0.6
            remote = r.random() < 0.15
            for j in range(r.choice([1, 1, 2, 3])):
                s = self._session(v, on_call=on_call, remote_access_app=remote)
                self._pay(v, ts + timedelta(minutes=7 * j), Channel.p2p, self._wallet(drop.uid), "wallet",
                          self._amount(v, r.uniform(4, 25)), session=s, rcp_user=drop.uid,
                          label=1, scenario="social_engineering", incident=inc)

        # 2. захват учётной записи
        for i, v in enumerate(victims(k)):
            inc = f"ATO-{i:03d}"
            ts = self._ts(r.randrange(1, self.days), night_ok=True)
            fdev = f"d_fraud_ato_{self.seed}_{i}"
            s = self._session(v, device=fdev, emulator=r.random() < 0.4, rooted=r.random() < 0.3)
            if r.random() < 0.35:
                code, lat, lon = r.choice(FOREIGN)
                s.ip_country, s.lat, s.lon = code, lat, lon
            elif r.random() < 0.5:
                s.lat, s.lon = None, None
            for j in range(r.randint(0, 4)):
                self._event(v, ts + timedelta(seconds=20 * j), EventType.login, session=s, login_success=False,
                            label=1, scenario="account_takeover", incident=inc)
            t = ts + timedelta(minutes=2)
            self._event(v, t, EventType.login, session=s, login_success=True, label=1,
                        scenario="account_takeover", incident=inc)
            if r.random() < 0.6:
                self._event(v, t + timedelta(minutes=3), EventType.profile_change, session=s,
                            changed_field=r.choice(["password", "phone"]), label=1,
                            scenario="account_takeover", incident=inc)
            drop = r.choice(drops[v.country])
            for j in range(r.randint(1, 3)):
                self._pay(v, t + timedelta(minutes=10 + 6 * j), Channel.p2p, self._wallet(drop.uid), "wallet",
                          self._amount(v, r.uniform(3, 12)), session=s, rcp_user=drop.uid,
                          label=1, scenario="account_takeover", incident=inc)

        # 3. скомпрометированные карты
        for i in range(k):
            inc = f"CARD-{i:03d}"
            c = r.choice(list(COUNTRIES))
            ts = self._ts(r.randrange(self.days), night_ok=True)
            fdev = f"d_fraud_card_{self.seed}_{i}"
            accs = [self._new_user(f"F{self.seed}C{i:03d}{a}", ts - timedelta(hours=r.randint(1, 48)), c)
                    for a in range(r.choice([1, 1, 2]))]
            cards = [f"stolen_{self.seed}_{i}_{n}" for n in range(r.randint(3, 7))]
            for n, card in enumerate(cards):
                a = accs[n % len(accs)]
                s = self._session(a, device=fdev, emulator=r.random() < 0.3)
                t = ts + timedelta(minutes=3 * n)
                self._event(a, t, EventType.card_bind, session=s, card_hash=card, card_bin="404000",
                            label=1, scenario="compromised_cards", incident=inc)
                small = COUNTRIES[c][2] * r.uniform(0.1, 0.9)
                self._pay(a, t + timedelta(minutes=1), Channel.service, r.choice(self.services), "merchant",
                          float(round(small) or 1), session=s, card=card, label=1,
                          scenario="compromised_cards", incident=inc)
            a = accs[0]
            s = self._session(a, device=fdev)
            self._pay(a, ts + timedelta(minutes=3 * len(cards) + 5), Channel.merchant, r.choice(self.merchants),
                      "merchant", float(round(COUNTRIES[c][2] * r.uniform(150, 600))), session=s, card=cards[-1],
                      label=1, scenario="compromised_cards", incident=inc)

        # 4. дропперы: транзит поступлений от жертв (часть жертв — из сценариев 1–2, часть — внешние)
        for c, lst in drops.items():
            for i, d in enumerate(lst):
                inc = f"DROP-{c}-{i:03d}"
                day = r.randrange(1, self.days)
                ts = self._ts(day)
                senders = r.sample([u for u in base if u.country == c and u.uid.startswith("U")], k=r.randint(3, 6))
                total_usd = 0.0
                for j, v in enumerate(senders):
                    amt = self._amount(v, r.uniform(3, 10))
                    total_usd += amt / COUNTRIES[c][2]
                    s = self._session(v, on_call=r.random() < 0.5)
                    self._pay(v, ts + timedelta(minutes=6 * j), Channel.p2p, self._wallet(d.uid), "wallet", amt,
                              session=s, rcp_user=d.uid, label=1, scenario="social_engineering",
                              incident=f"SE-net-{c}-{i}-{j}", note="перевод жертвы на дроппера")
                nxt = r.choice([x for x in lst if x.uid != d.uid])
                t = ts + timedelta(minutes=6 * len(senders) + r.randint(5, 40))
                part = r.uniform(0.85, 0.98)
                cash = float(round(total_usd * part * COUNTRIES[c][2]))
                if r.random() < 0.5:
                    self._pay(d, t, Channel.p2p, self._wallet(nxt.uid), "wallet", cash, rcp_user=nxt.uid,
                              label=1, scenario="droppers", incident=inc)
                else:
                    self._pay(d, t, Channel.withdrawal, f"c_out_{self.seed}_{c}_{i}", "card", cash,
                              label=1, scenario="droppers", incident=inc)

        # 5. мультиаккаунты ради бонусов
        for i in range(k):
            inc = f"MULTI-{i:03d}"
            c = r.choice(list(COUNTRIES))
            ts = self._ts(r.randrange(self.days))
            fdev = f"d_fraud_farm_{self.seed}_{i}"
            emu = r.random() < 0.5
            for a in range(r.randint(3, 7)):
                u = self._new_user(f"F{self.seed}M{i:03d}{a}", ts + timedelta(minutes=15 * a), c)
                s = self._session(u, device=fdev, emulator=emu)
                t = ts + timedelta(minutes=15 * a)
                self._event(u, t, EventType.login, session=s, login_success=True, label=1,
                            scenario="multiaccount", incident=inc)
                self._event(u, t + timedelta(minutes=3), EventType.bonus_claim, session=s, promo_id="WELCOME",
                            label=1, scenario="multiaccount", incident=inc)

        # 6. получатель в стоп-листе
        for c in COUNTRIES:
            lname = "national_antifraud_kz" if c == "KZ" else "internal"
            for n in range(6):
                val = f"w_blk_{c}_{n}"
                self.stoplists.append({"list_name": lname, "jurisdiction": c, "entity_type": "wallet",
                                       "value": val, "reason": "вымышленная запись для демонстрации"})
        for i, v in enumerate(victims(k)):
            inc = f"STOP-{i:03d}"
            ts = self._ts(r.randrange(self.days))
            val = f"w_blk_{v.country}_{r.randrange(6)}"
            self._pay(v, ts, Channel.p2p, val, "wallet", self._amount(v, r.uniform(1, 6)),
                      label=1, scenario="stoplist", incident=inc)

    def build(self, incidents_per_scenario: int = 25) -> list[Row]:
        self.legit()
        self.fraud(incidents_per_scenario)
        self.rows.sort(key=lambda x: x.event.ts)
        return self.rows


def generate(seed: int = 7, users: int = 2500, days: int = 14, incidents: int = 25) -> tuple[list[Row], list[dict]]:
    g = Gen(seed=seed, users=users, days=days)
    rows = g.build(incidents)
    return rows, g.stoplists
