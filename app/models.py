"""Контракт данных API: событие на оценку риска и решение.

Полные номера карт не принимаются: только токен/хеш, BIN и последние 4 цифры.
Идентификаторы клиентов передаются псевдонимизированными (см. app/privacy.py).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class EventType(str, Enum):
    payment = "payment"                # платёж или перевод (до проведения)
    login = "login"                    # вход в приложение
    card_bind = "card_bind"            # привязка карты
    profile_change = "profile_change"  # смена телефона / пароля / e-mail / устройства
    bonus_claim = "bonus_claim"        # получение бонуса / кешбэка по акции


class Channel(str, Enum):
    p2p = "p2p"              # перевод другому клиенту / на карту / по номеру телефона
    service = "service"      # оплата услуг
    qr = "qr"                # оплата по QR
    merchant = "merchant"    # оплата бизнесу (эквайринг)
    withdrawal = "withdrawal"  # вывод на карту / в банк


class Action(str, Enum):
    allow = "allow"          # пропустить
    challenge = "challenge"  # запросить дополнительное подтверждение (выполняет приложение заказчика)
    decline = "decline"      # отклонить


class Session(BaseModel):
    device_id: Optional[str] = Field(None, description="Идентификатор устройства из SDK")
    os: Optional[str] = None
    emulator: bool = False
    rooted: bool = Field(False, description="Root / jailbreak / модифицированная ОС")
    remote_access_app: bool = Field(False, description="Запущено приложение удалённого доступа")
    on_call: bool = Field(False, description="Идёт телефонный звонок во время операции")
    ip: Optional[str] = None
    ip_country: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


class Payment(BaseModel):
    channel: Channel
    amount: float = Field(gt=0)
    currency: str
    recipient_id: Optional[str] = Field(None, description="Кошелёк/телефон/карта получателя (хеш)")
    recipient_type: str = Field("wallet", description="wallet | card | phone | merchant | iban")
    recipient_user_id: Optional[str] = Field(None, description="Если получатель — клиент этого же приложения")
    card_hash: Optional[str] = Field(None, description="Токен/хеш карты списания")
    card_bin: Optional[str] = None
    card_last4: Optional[str] = None

    @field_validator("currency")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("card_bin")
    @classmethod
    def bin_len(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not (v.isdigit() and 6 <= len(v) <= 8):
            raise ValueError("BIN — 6–8 цифр")
        return v


class Event(BaseModel):
    event_id: str
    event_type: EventType
    ts: datetime
    jurisdiction: str = Field(description="Страна присутствия: KZ, UZ, KG …")
    user_id: str
    account_created: Optional[datetime] = None
    session: Session = Session()
    payment: Optional[Payment] = None
    # для login
    login_success: Optional[bool] = None
    # для profile_change: phone | password | email | device
    changed_field: Optional[str] = None
    # для card_bind
    card_hash: Optional[str] = None
    card_bin: Optional[str] = None
    # для bonus_claim
    promo_id: Optional[str] = None

    @field_validator("jurisdiction")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper()


class Reason(BaseModel):
    code: str
    source: str = Field(description="rule | ml | list | policy")
    points: float = 0
    text: str = ""


class Decision(BaseModel):
    event_id: str
    action: Action
    score: int = Field(ge=0, le=100)
    ml_probability: Optional[float] = None
    reasons: list[Reason] = []
    scenarios: list[str] = Field([], description="Сценарии раздела 4 ТЗ, на которые похожа операция")
    case_id: Optional[int] = None
    jurisdiction: str
    rules_version: str
    model_version: str
    latency_ms: float = 0
    degraded: bool = Field(False, description="Ответ без ML-компонента (превышен бюджет времени)")


class Outcome(BaseModel):
    """Итог операции после решения: прошла ли проверка, проведена ли операция."""
    completed: bool
