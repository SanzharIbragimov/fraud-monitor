"""ML-компонент: логистическая регрессия на поведенческих признаках.

Холодный старт: модель предобучена на синтетическом потоке с размеченными
типологиями мошенничества (app/synthetic.py) — данных заказчика для этого не нужно.
После запуска модель дообучается на результатах разбора инцидентов
(метки «мошенничество / легитимно» от аналитика): POST /v1/model/retrain.

Логистическая регрессия выбрана для объяснимости: вклад каждого признака
в решение считается напрямую и выводится в причинах. При накоплении данных
её можно заменить на градиентный бустинг с SHAP, контракт API не меняется.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "model.json"

# признак → (функция преобразования, подпись для причины)
SPEC: dict[str, tuple] = {
    "log_amount_usd": (lambda f: math.log1p(f["amount_usd"]), "сумма операции"),
    "log_amount_vs_usual": (lambda f: math.log(max(min(f["amount_vs_usual"], 100), 0.01)), "сумма выше обычной"),
    "new_recipient": (lambda f: float(f["new_recipient"]), "новый получатель"),
    "recipient_new_to_system": (lambda f: float(f["recipient_new_to_system"]), "получатель новый в системе"),
    "log_recipient_senders": (lambda f: math.log1p(f["recipient_senders_24h"]), "у получателя много отправителей"),
    "payments_1h": (lambda f: min(f["payments_1h"], 10), "частота операций"),
    "new_recipients_1h": (lambda f: min(f["new_recipients_1h"], 10), "серия новых получателей"),
    "outflow_after_inflow": (lambda f: min(f["outflow_after_inflow"], 2.0), "быстрый вывод входящих"),
    "log_user_senders": (lambda f: math.log1p(f["user_senders_24h"]), "много входящих от разных людей"),
    "night": (lambda f: float(f["night"]), "ночное время"),
    "new_device": (lambda f: float(f["new_device"]), "новое устройство"),
    "emulator": (lambda f: float(f["emulator"]), "эмулятор"),
    "rooted": (lambda f: float(f["rooted"]), "модифицированная ОС"),
    "remote_access_app": (lambda f: float(f["remote_access_app"]), "удалённый доступ"),
    "on_call": (lambda f: float(f["on_call"]), "звонок во время операции"),
    "log_device_users": (lambda f: math.log1p(f["device_users_30d"] - 1), "несколько аккаунтов на устройстве"),
    "ip_country_foreign": (lambda f: float(f["ip_country_foreign"]), "IP другой страны"),
    "geo_impossible": (lambda f: float(f["geo_velocity_kmh"] >= 900), "невозможное перемещение"),
    "failed_logins_1h": (lambda f: min(f["failed_logins_1h"], 10), "неуспешные входы"),
    "password_changed_24h": (lambda f: float(f["password_changed_24h"]), "недавняя смена пароля"),
    "phone_changed_24h": (lambda f: float(f["phone_changed_24h"]), "недавняя смена телефона"),
    "log_cards_device": (lambda f: math.log1p(f["cards_device_24h"]), "много карт на устройстве"),
    "log_card_users": (lambda f: math.log1p(max(f["card_users_30d"] - 1, 0)), "карта у нескольких аккаунтов"),
    "small_payments_1h": (lambda f: min(f["small_payments_1h"], 10), "пробные мелкие операции"),
    "young_account": (lambda f: float(f["account_age_days"] < 2), "новая учётная запись"),
    "log_txn_count": (lambda f: math.log1p(f["user_txn_count"]), "история операций клиента"),
    "is_p2p": (lambda f: float(f["channel"] in ("p2p", "withdrawal")), "перевод / вывод"),
    "is_bonus": (lambda f: float(f["event_type"] == "bonus_claim"), "получение бонуса"),
    "log_bonus_device": (lambda f: math.log1p(max(f["bonus_users_device_30d"] - 1, 0)), "бонусы с одного устройства"),
}
NAMES = list(SPEC)


def vectorize(f: dict) -> np.ndarray:
    return np.array([SPEC[n][0](f) for n in NAMES], dtype=float)


class Model:
    def __init__(self, params: dict) -> None:
        self.version = params["version"]
        self.mean = np.array(params["mean"])
        self.std = np.array(params["std"])
        self.coef = np.array(params["coef"])
        self.intercept = float(params["intercept"])
        self.meta = params.get("meta", {})

    def predict(self, f: dict) -> tuple[float, list[tuple[str, float]]]:
        z = (vectorize(f) - self.mean) / self.std
        contrib = self.coef * z
        logit = self.intercept + contrib.sum()
        p = 1.0 / (1.0 + math.exp(-max(min(logit, 30), -30)))
        top = sorted(((NAMES[i], float(c)) for i, c in enumerate(contrib) if c > 0.3), key=lambda x: -x[1])[:3]
        return p, top

    def params(self) -> dict:
        return {"version": self.version, "features": NAMES, "mean": self.mean.tolist(), "std": self.std.tolist(),
                "coef": self.coef.tolist(), "intercept": self.intercept, "meta": self.meta}


def fit(X: np.ndarray, y: np.ndarray, version: str, meta: dict | None = None) -> Model:
    from sklearn.linear_model import LogisticRegression

    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1.0
    clf = LogisticRegression(C=0.5, class_weight="balanced", max_iter=2000)
    clf.fit((X - mean) / std, y)
    return Model({"version": version, "mean": mean, "std": std, "coef": clf.coef_[0],
                  "intercept": clf.intercept_[0], "meta": meta or {}})


def save(m: Model, path: Path = MODEL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m.params(), ensure_ascii=False, indent=1), encoding="utf-8")


def load(path: Path = MODEL_PATH) -> Model | None:
    if not path.exists():
        return None
    return Model(json.loads(path.read_text(encoding="utf-8")))


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
