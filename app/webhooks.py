"""Вебхуки для асинхронных событий: отказ по операции, новый инцидент,
результат разбора, изменение правил. Доставка в фоне, подпись HMAC-SHA256
в заголовке X-Signature, до 3 попыток с паузой.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import queue
import threading
import time
import uuid

import httpx

from .store import Store, utcnow

EVENTS = {"decision.declined", "case.created", "case.resolved", "rules.updated", "lists.updated"}


class Webhooks:
    def __init__(self, store: Store, secret: str) -> None:
        self.store = store
        self.secret = secret.encode()
        self.q: queue.Queue = queue.Queue(maxsize=10000)
        self.delivered: list[dict] = []  # последние доставки (для панели)
        self.client = httpx.Client(timeout=3.0)
        threading.Thread(target=self._worker, daemon=True, name="webhooks").start()

    def subscribe(self, url: str, events: list[str]) -> int:
        bad = set(events) - EVENTS
        if bad:
            raise ValueError(f"неизвестные события: {sorted(bad)}")
        return self.store.x("INSERT INTO webhooks(url, events, created) VALUES (?,?,?)",
                            (url, ",".join(events), utcnow()))

    def list(self) -> list[dict]:
        return self.store.q("SELECT * FROM webhooks ORDER BY id")

    def sign(self, body: bytes) -> str:
        return hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    def emit(self, event: str, data: dict) -> None:
        subs = [s for s in self.list() if event in s["events"].split(",")]
        if not subs:
            return
        payload = {"id": str(uuid.uuid4()), "event": event, "ts": utcnow(), "data": data}
        for s in subs:
            try:
                self.q.put_nowait((s["url"], payload))
            except queue.Full:
                self.store.audit("system", "system", "webhook.dropped", s["url"], event)

    def _worker(self) -> None:
        while True:
            url, payload = self.q.get()
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            status = None
            for attempt in range(3):
                try:
                    r = self.client.post(url, content=body, headers={
                        "Content-Type": "application/json", "X-Signature": self.sign(body),
                        "X-Event": payload["event"]})
                    status = r.status_code
                    if r.status_code < 500:
                        break
                except httpx.HTTPError as e:
                    status = f"error: {type(e).__name__}"
                time.sleep(0.5 * (attempt + 1))
            self.delivered.insert(0, {"ts": utcnow(), "url": url, "event": payload["event"], "status": status})
            del self.delivered[50:]
