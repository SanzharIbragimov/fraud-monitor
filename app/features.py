"""Хранилище поведенческого состояния и расчёт признаков события.

Состояние в памяти процесса. В продакшене скользящие окна, профили и графовые
связи переносятся в Redis (или аналог), чтобы несколько экземпляров сервиса
работали с общим состоянием. Время берётся из события (event.ts), поэтому
расчёт детерминирован и пригоден для прогона истории.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import Channel, Event, EventType

H1, H2, H24, D30 = timedelta(hours=1), timedelta(hours=2), timedelta(hours=24), timedelta(days=30)

# Описание признаков: используется для проверки правил и в интерфейсе аналитика.
FEATURES: dict[str, str] = {
    "event_type": "тип события: payment | login | card_bind | profile_change | bonus_claim",
    "channel": "канал платежа: p2p | service | qr | merchant | withdrawal",
    "changed_field": "что изменено в профиле: phone | password | email | device",
    "recipient_type": "тип получателя: wallet | card | phone | merchant | iban",
    "amount": "сумма в валюте страны",
    "amount_usd": "сумма, приведённая к USD (условный курс из настроек страны)",
    "amount_vs_usual": "во сколько раз сумма выше обычной для клиента",
    "user_txn_count": "число проведённых операций клиента",
    "account_age_days": "возраст учётной записи, дней",
    "new_recipient": "первый перевод этому получателю",
    "recipient_new_to_system": "получатель впервые встречается в системе",
    "recipient_senders_24h": "разных отправителей у получателя за 24 ч (включая текущего)",
    "payments_1h": "операций клиента за 1 ч (до текущей)",
    "new_recipients_1h": "новых получателей у клиента за 1 ч (включая текущего)",
    "outflow_after_inflow": "доля входящих за 2 ч средств, которую клиент выводит (0–1+)",
    "inflow_2h": "сумма входящих переводов клиенту за 2 ч",
    "user_senders_24h": "разных отправителей у самого клиента за 24 ч",
    "night": "операция ночью (00:00–05:59 по местному времени)",
    "new_device": "устройство впервые у клиента, у которого уже есть другие известные устройства",
    "device_age_hours": "часов с первого появления устройства у клиента",
    "emulator": "эмулятор",
    "rooted": "root / jailbreak / модифицированная ОС",
    "remote_access_app": "запущено приложение удалённого доступа",
    "on_call": "идёт телефонный звонок",
    "device_users_30d": "разных учётных записей на устройстве за 30 дней",
    "ip_country_foreign": "страна IP отличается от страны присутствия",
    "geo_velocity_kmh": "скорость перемещения от прошлой сессии, км/ч",
    "failed_logins_1h": "неуспешных входов за 1 ч",
    "password_changed_24h": "пароль менялся за последние 24 ч",
    "phone_changed_24h": "номер телефона менялся за последние 24 ч",
    "cards_device_24h": "разных карт на устройстве за 24 ч",
    "card_users_30d": "разных учётных записей у карты за 30 дней",
    "small_payments_1h": "пробных (мелких) операций с устройства за 1 ч",
    "bonus_users_device_30d": "разных учётных записей, получивших бонус с устройства за 30 дней",
    "missing_device": "в событии нет идентификатора устройства",
    "recipient_is_merchant": "получатель — бизнес-клиент",
}


class Window:
    """Скользящее окно событий (ts, value, amount) по ключу."""

    def __init__(self, keep: timedelta) -> None:
        self.keep = keep
        self._d: dict[str, deque] = defaultdict(deque)

    def add(self, key: str, ts: datetime, value: object = None, amount: float = 0.0) -> None:
        self._d[key].append((ts, value, amount))

    def _q(self, key: str, now: datetime) -> deque:
        q = self._d.get(key)
        if q is None:
            return deque()
        while q and q[0][0] < now - self.keep:
            q.popleft()
        return q

    def count(self, key: str, now: datetime, span: timedelta) -> int:
        return sum(1 for ts, _, _ in self._q(key, now) if ts >= now - span)

    def distinct(self, key: str, now: datetime, span: timedelta) -> set:
        return {v for ts, v, _ in self._q(key, now) if ts >= now - span}

    def total(self, key: str, now: datetime, span: timedelta) -> float:
        return sum(a for ts, _, a in self._q(key, now) if ts >= now - span)


@dataclass
class UserProfile:
    amounts: deque = field(default_factory=lambda: deque(maxlen=50))  # суммы проведённых операций, USD
    recipients: set = field(default_factory=set)
    devices: dict = field(default_factory=dict)        # device_id → first seen
    changes: dict = field(default_factory=dict)        # поле → когда менялось
    last_geo: tuple | None = None                      # (lat, lon, ts)
    txn_count: int = 0

    def usual_amount(self) -> float | None:
        if len(self.amounts) < 3:
            return None
        s = sorted(self.amounts)
        return max(s[len(s) // 2], 1.0)  # медиана


@dataclass
class State:
    users: dict[str, UserProfile] = field(default_factory=lambda: defaultdict(UserProfile))
    first_seen: dict[str, datetime] = field(default_factory=dict)  # получатель/клиент → когда впервые встречен
    w1d: Window = field(default_factory=lambda: Window(H24))
    w30d: Window = field(default_factory=lambda: Window(D30))


def _haversine(a: tuple, b: tuple) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def local_hour(ts: datetime, jurisdiction: str) -> int:
    # Упрощение: время события передаётся в местном времени страны.
    return ts.hour


def compute(ev: Event, st: State, jur: dict) -> dict:
    """Признаки события. Не меняет состояние."""
    now, u = ev.ts, st.users[ev.user_id]
    s, p = ev.session, ev.payment
    dev = s.device_id
    f: dict = {
        "event_type": ev.event_type.value,
        "channel": p.channel.value if p else None,
        "changed_field": ev.changed_field,
        "recipient_type": p.recipient_type if p else None,
        "amount": p.amount if p else 0.0,
        "amount_usd": (p.amount / jur["usd_rate"]) if p else 0.0,
        "user_txn_count": u.txn_count,
        "account_age_days": ((now - ev.account_created).total_seconds() / 86400) if ev.account_created else 365.0,
        "night": local_hour(now, ev.jurisdiction) < 6,
        "emulator": s.emulator, "rooted": s.rooted,
        "remote_access_app": s.remote_access_app, "on_call": s.on_call,
        "ip_country_foreign": bool(s.ip_country) and s.ip_country.upper() != ev.jurisdiction,
        "missing_device": dev is None,
        "failed_logins_1h": st.w1d.count(f"fail:{ev.user_id}", now, H1),
        "password_changed_24h": "password" in u.changes and now - u.changes["password"] < H24,
        "phone_changed_24h": "phone" in u.changes and now - u.changes["phone"] < H24,
        "payments_1h": st.w1d.count(f"pay:{ev.user_id}", now, H1),
        "user_senders_24h": len(st.w1d.distinct(f"in:{ev.user_id}", now, H24)),
        "inflow_2h": st.w1d.total(f"in:{ev.user_id}", now, H2),
        "_small_limit": jur["limits"]["small_test_payment"],
    }
    usual = u.usual_amount()
    f["amount_vs_usual"] = (f["amount_usd"] / usual) if (p and usual) else 1.0

    # устройство
    if dev:
        first = u.devices.get(dev)
        # «новое» — только если у клиента уже есть известные устройства (иначе это первый вход, а не смена)
        f["new_device"] = first is None and len(u.devices) > 0
        f["device_age_hours"] = 0.0 if first is None else (now - first).total_seconds() / 3600
        users = st.w30d.distinct(f"dev_users:{dev}", now, D30) | {ev.user_id}
        f["device_users_30d"] = len(users)
        cards = st.w1d.distinct(f"dev_cards:{dev}", now, H24)
        card = (p.card_hash if p else None) or ev.card_hash
        if card:
            cards = cards | {card}
        f["cards_device_24h"] = len(cards)
        f["small_payments_1h"] = st.w1d.count(f"dev_small:{dev}", now, H1)
        bonus = st.w30d.distinct(f"dev_bonus:{dev}", now, D30)
        if ev.event_type == EventType.bonus_claim:
            bonus = bonus | {ev.user_id}
        f["bonus_users_device_30d"] = len(bonus)
    else:
        f.update(new_device=False, device_age_hours=0.0, device_users_30d=1, cards_device_24h=0,
                 small_payments_1h=0, bonus_users_device_30d=0)

    # гео
    f["geo_velocity_kmh"] = 0.0
    if s.lat is not None and s.lon is not None and u.last_geo:
        hours = max((now - u.last_geo[2]).total_seconds() / 3600, 0.05)
        f["geo_velocity_kmh"] = _haversine(u.last_geo, (s.lat, s.lon)) / hours

    # карта
    card = (p.card_hash if p else None) or ev.card_hash
    f["card_users_30d"] = len(st.w30d.distinct(f"card_users:{card}", now, D30) | {ev.user_id}) if card else 0

    # получатель
    f.update(new_recipient=False, recipient_new_to_system=False, recipient_senders_24h=0,
             new_recipients_1h=0, outflow_after_inflow=0.0, recipient_is_merchant=False)
    if p and p.recipient_id:
        r = p.recipient_id
        f["recipient_is_merchant"] = p.recipient_type == "merchant" or p.channel == Channel.merchant
        f["new_recipient"] = r not in u.recipients
        f["recipient_new_to_system"] = r not in st.first_seen
        f["recipient_senders_24h"] = len(st.w1d.distinct(f"in:{r}", now, H24) | {ev.user_id})
        new_r = st.w1d.distinct(f"newrcp:{ev.user_id}", now, H1)
        if f["new_recipient"]:
            new_r = new_r | {r}
        f["new_recipients_1h"] = len(new_r)
    if p and p.channel in (Channel.p2p, Channel.withdrawal) and f["inflow_2h"] > 0:
        out = st.w1d.total(f"out:{ev.user_id}", now, H2) + f["amount_usd"]
        f["outflow_after_inflow"] = round(out / f["inflow_2h"], 3)
    return f


def update(ev: Event, st: State, f: dict, completed: bool) -> None:
    """Обновить состояние после решения. completed — операция не отклонена."""
    now, u, s, p = ev.ts, st.users[ev.user_id], ev.session, ev.payment
    dev = s.device_id
    st.first_seen.setdefault(ev.user_id, now)
    if dev:
        st.w30d.add(f"dev_users:{dev}", now, ev.user_id)
    card = (p.card_hash if p else None) or ev.card_hash
    if card:
        st.w30d.add(f"card_users:{card}", now, ev.user_id)
        if dev:
            st.w1d.add(f"dev_cards:{dev}", now, card)

    if ev.event_type == EventType.login:
        if ev.login_success is False:
            st.w1d.add(f"fail:{ev.user_id}", now)
    if ev.event_type == EventType.profile_change and ev.changed_field and completed:
        u.changes[ev.changed_field] = now
    if ev.event_type == EventType.bonus_claim and dev:
        st.w30d.add(f"dev_bonus:{dev}", now, ev.user_id)

    if p:
        st.w1d.add(f"pay:{ev.user_id}", now)
        if dev and f["amount"] <= f.get("_small_limit", 0):
            st.w1d.add(f"dev_small:{dev}", now)
        if p.recipient_id and f["new_recipient"]:
            st.w1d.add(f"newrcp:{ev.user_id}", now, p.recipient_id)
        if completed:
            u.txn_count += 1
            u.amounts.append(f["amount_usd"])
            if p.recipient_id:
                u.recipients.add(p.recipient_id)
                st.first_seen.setdefault(p.recipient_id, now)
                st.w1d.add(f"in:{p.recipient_id}", now, ev.user_id, f["amount_usd"])
                if p.recipient_user_id and p.recipient_user_id != p.recipient_id:
                    st.w1d.add(f"in:{p.recipient_user_id}", now, ev.user_id, f["amount_usd"])
            if p.channel in (Channel.p2p, Channel.withdrawal):
                st.w1d.add(f"out:{ev.user_id}", now, None, f["amount_usd"])

    if completed:
        if dev and dev not in u.devices and (ev.event_type != EventType.login or ev.login_success is not False):
            u.devices[dev] = now
        if s.lat is not None and s.lon is not None:
            u.last_geo = (s.lat, s.lon, now)
