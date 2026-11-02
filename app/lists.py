"""Стоп-листы: внутренние и полученные заказчиком от национальных антифрод-систем.

Формат CSV (UTF-8): list_name,jurisdiction,entity_type,value,reason
entity_type: phone | wallet | card | iban | device | user
value — хеш/токен (псевдонимизированный идентификатор), не открытые данные.

Импорт: POST /v1/lists/import (файл) или папка LISTS_INBOX — файлы оттуда
подхватываются автоматически при изменении (опрос раз в LISTS_POLL_SEC).
"""
from __future__ import annotations

import csv
import io
import threading
import time
from pathlib import Path

from .store import Store, utcnow

ENTITY_TYPES = {"phone", "wallet", "card", "iban", "device", "user"}
REQUIRED = ["list_name", "jurisdiction", "entity_type", "value"]


class Lists:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.index: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._mtimes: dict[str, float] = {}
        self.reload()

    def reload(self) -> None:
        rows = self.store.q("SELECT list_name, jurisdiction, entity_type, value, reason FROM list_entries")
        self.index = {(r["jurisdiction"], r["entity_type"], r["value"]): (r["list_name"], r["reason"] or "")
                      for r in rows}

    def import_csv(self, text: str, source: str, replace_list: bool = False) -> dict:
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"нет колонок: {missing}")
        ok, bad, names = [], [], set()
        for i, r in enumerate(reader, start=2):
            et = (r.get("entity_type") or "").strip().lower()
            val = (r.get("value") or "").strip()
            jur = (r.get("jurisdiction") or "").strip().upper()
            name = (r.get("list_name") or "").strip()
            if et not in ENTITY_TYPES or not val or not jur or not name:
                bad.append({"line": i, "row": r})
                continue
            names.add((name, jur))
            ok.append((name, jur, et, val, (r.get("reason") or "").strip(), source, utcnow()))
        if replace_list:
            for name, jur in names:
                self.store.x("DELETE FROM list_entries WHERE list_name=? AND jurisdiction=?", (name, jur))
        self.store.xmany("INSERT OR REPLACE INTO list_entries VALUES (?,?,?,?,?,?,?)", ok)
        self.reload()
        return {"imported": len(ok), "rejected": len(bad), "rejected_rows": bad[:20], "lists": sorted(n for n, _ in names)}

    def check(self, jurisdiction: str, allowed_lists: list[str], entities: dict[str, str | None]) -> list[dict]:
        hits = []
        for et, val in entities.items():
            if not val:
                continue
            hit = self.index.get((jurisdiction, et, val))
            if hit and hit[0] in allowed_lists:
                hits.append({"list": hit[0], "entity_type": et, "reason": hit[1]})
        return hits

    def summary(self) -> list[dict]:
        return self.store.q("SELECT list_name, jurisdiction, COUNT(*) AS entries, MAX(added) AS updated "
                            "FROM list_entries GROUP BY list_name, jurisdiction ORDER BY jurisdiction, list_name")

    # автоматическое обновление из папки
    def scan_inbox(self, folder: Path) -> list[dict]:
        results = []
        if not folder.exists():
            return results
        for p in sorted(folder.glob("*.csv")):
            m = p.stat().st_mtime
            if self._mtimes.get(str(p)) == m:
                continue
            self._mtimes[str(p)] = m
            try:
                res = self.import_csv(p.read_text(encoding="utf-8"), source=f"file:{p.name}", replace_list=True)
                self.store.audit("system", "system", "lists.import", p.name, {"imported": res["imported"], "rejected": res["rejected"]})
                results.append({"file": p.name, **res})
            except ValueError as e:
                self.store.audit("system", "system", "lists.import_failed", p.name, str(e))
        return results

    def start_watcher(self, folder: Path, every_sec: int) -> None:
        def loop() -> None:
            while True:
                self.scan_inbox(folder)
                time.sleep(every_sec)
        threading.Thread(target=loop, daemon=True, name="lists-watcher").start()
