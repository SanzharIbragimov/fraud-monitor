"""Псевдонимизация идентификаторов на стороне заказчика до отправки в антифрод.

Номер телефона, кошелька, IBAN, ID клиента заменяются на HMAC-SHA256 с секретом
заказчика: одинаковые значения дают одинаковый хеш (связи и стоп-листы работают),
но исходное значение из хеша не восстанавливается. Полные номера карт не
передаются вообще — только токен/хеш карты, BIN и последние 4 цифры.
"""
from __future__ import annotations

import hashlib
import hmac


def pseudonymize(value: str, secret: str, prefix: str = "") -> str:
    norm = "".join(value.split()).lower()
    digest = hmac.new(secret.encode(), norm.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{prefix}{digest}"


def mask_pan(pan: str) -> dict:
    """Из номера карты оставить только BIN и последние 4 цифры (вызывать на стороне заказчика)."""
    digits = "".join(ch for ch in pan if ch.isdigit())
    if not 13 <= len(digits) <= 19:
        raise ValueError("некорректный номер карты")
    return {"card_bin": digits[:6], "card_last4": digits[-4:]}
