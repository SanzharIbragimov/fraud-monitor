"""Клиент для бэкенда платёжного приложения с резервной политикой (раздел 6 ТЗ).

Если антифрод недоступен или не ответил за отведённое время, решение
принимается локально по лимитам страны, которые настраивает заказчик:
  сумма ≤ auto_allow_up_to → пропустить;
  выше → отклонить или отправить на отложенную проверку (above_limit).
Решение по резервной политике помечается source="fallback" — его можно
дослать в антифрод позже для разбора.

Пример:
    client = FraudClient("http://127.0.0.1:8000", "demo-integration-key", timeout_ms=300)
    decision = client.score(event_dict)
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import yaml

DEFAULT_POLICY = Path(__file__).resolve().parent.parent / "config" / "jurisdictions.yaml"


class FraudClient:
    def __init__(self, base_url: str, api_key: str, timeout_ms: int = 300,
                 policy_path: Path = DEFAULT_POLICY, transport: httpx.BaseTransport | None = None) -> None:
        self.http = httpx.Client(base_url=base_url, headers={"X-API-Key": api_key},
                                 timeout=timeout_ms / 1000, transport=transport)
        self.policy = yaml.safe_load(Path(policy_path).read_text(encoding="utf-8"))
        self.deferred: list[dict] = []  # операции на отложенную проверку

    def fallback(self, event: dict, why: str) -> dict:
        jur = self.policy.get(event.get("jurisdiction", ""), {})
        fb = jur.get("fallback", {"auto_allow_up_to": 0, "above_limit": "decline"})
        amount = (event.get("payment") or {}).get("amount", 0)
        if event.get("event_type") != "payment" or amount <= fb["auto_allow_up_to"]:
            action = "allow"
        elif fb["above_limit"] == "defer":
            action = "challenge"  # приложение держит операцию до отложенной проверки
            self.deferred.append(event)
        else:
            action = "decline"
        return {"event_id": event.get("event_id"), "action": action, "source": "fallback", "fallback_reason": why,
                "limit": fb["auto_allow_up_to"], "policy": fb["above_limit"]}

    def score(self, event: dict) -> dict:
        t0 = time.perf_counter()
        try:
            r = self.http.post("/v1/score", json=event)
            if r.status_code >= 500:
                return self.fallback(event, f"http {r.status_code}")
            r.raise_for_status()
            d = r.json()
            d["source"] = "antifraud"
            d["client_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return d
        except httpx.TimeoutException:
            return self.fallback(event, "timeout")
        except httpx.TransportError as e:
            return self.fallback(event, f"unavailable: {type(e).__name__}")
