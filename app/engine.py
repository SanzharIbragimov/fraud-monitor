"""Оценка риска события: стоп-листы → правила → ML → действие → инцидент.

Порядок внутри одного вызова:
1. проверка страны и валюты;
2. признаки поведения, устройства, сессии и связей (app/features.py);
3. стоп-листы страны (национальные и внутренние);
4. правила аналитика (config/rules.yaml);
5. ML-вероятность, если укладываемся в бюджет времени;
6. итоговый балл → действие по порогам страны, минимальные действия правил;
7. инцидент для разбора, вебхуки, обновление состояния.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter, deque
from pathlib import Path

import yaml

from . import features as F
from . import ml, rules
from .lists import Lists
from .models import Action, Decision, Event, EventType, Reason
from .store import Store, utcnow

ROOT = Path(__file__).resolve().parent.parent
RANK = {Action.allow: 0, Action.challenge: 1, Action.decline: 2}
ML_EVENTS = {EventType.payment, EventType.bonus_claim, EventType.card_bind}


def load_jurisdictions(path: Path = ROOT / "config" / "jurisdictions.yaml") -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class UnknownJurisdiction(ValueError):
    pass


class Engine:
    def __init__(self, store: Store | None = None, rules_path: Path = ROOT / "config" / "rules.yaml",
                 model: ml.Model | None = None, webhooks=None, budget_ms: float | None = None,
                 ml_enabled: bool = True) -> None:
        self.store = store or Store()
        self.jur = load_jurisdictions()
        self.rules_path = rules_path
        self.ruleset = rules.parse(rules_path.read_text(encoding="utf-8"), self.jur)
        self.model = (model or ml.load()) if ml_enabled else None
        self.lists = Lists(self.store)
        self.webhooks = webhooks
        self.state = F.State()
        self.lock = threading.Lock()
        self.budget_ms = budget_ms if budget_ms is not None else float(os.getenv("ML_BUDGET_MS", "120"))
        self.recent: deque = deque(maxlen=300)
        self.stats = Counter()
        self.rule_hits = Counter()
        self.latencies: deque = deque(maxlen=5000)
        self.last_features: dict = {}

    # ---------- правила ----------
    def rules_text(self) -> str:
        return self.rules_path.read_text(encoding="utf-8")

    def update_rules(self, text: str, actor: str, role: str) -> rules.RuleSet:
        new = rules.parse(text, self.jur)  # бросит ValueError с понятной причиной
        old_version = self.ruleset.version
        self.rules_path.write_text(text, encoding="utf-8")
        with self.lock:
            self.ruleset = new
        self.store.audit(actor, role, "rules.update", new.version,
                         {"from": old_version, "to": new.version, "rules": len(new.rules)})
        if self.webhooks:
            self.webhooks.emit("rules.updated", {"version": new.version, "by": actor})
        return new

    # ---------- оценка ----------
    def score(self, ev: Event, persist_case: bool = True) -> Decision:
        t0 = time.perf_counter()
        jur = self.jur.get(ev.jurisdiction)
        if jur is None:
            raise UnknownJurisdiction(f"страна {ev.jurisdiction} не настроена")
        reasons: list[Reason] = []
        scenarios: set[str] = set()
        floor = Action.allow
        create_case = False

        with self.lock:
            rs, model = self.ruleset, self.model
            f = F.compute(ev, self.state, jur)
            self.last_features = f

            # качество данных
            if ev.payment and ev.payment.currency != jur["currency"]:
                reasons.append(Reason(code="DQ_CURRENCY", source="policy", points=0,
                                      text=f"валюта {ev.payment.currency} не совпадает с валютой страны {jur['currency']}"))
                floor = Action.challenge
                scenarios.add("data_quality")
            if ev.event_type == EventType.payment and ev.payment is None:
                reasons.append(Reason(code="DQ_NO_PAYMENT", source="policy", text="нет блока payment"))
                floor = Action.challenge

            # стоп-листы
            p = ev.payment
            entities = {"user": ev.user_id, "device": ev.session.device_id,
                        "card": (p.card_hash if p else None) or ev.card_hash}
            if p and p.recipient_id:
                entities[p.recipient_type if p.recipient_type in ("phone", "wallet", "card", "iban") else "wallet"] = p.recipient_id
            hits = self.lists.check(ev.jurisdiction, jur.get("lists", []), entities)
            points = 0.0
            for h in hits:
                reasons.append(Reason(code=f"LIST_{h['list'].upper()}", source="list", points=100,
                                      text=f"{h['entity_type']} в стоп-листе «{h['list']}» {h['reason']}".strip()))
                scenarios.add("stoplist")
                points += 100
                floor = Action.decline
                create_case = True

            # правила
            for r in rules.evaluate(rs, f, ev.jurisdiction, jur):
                points += r.points
                self.rule_hits[r.id] += 1
                reasons.append(Reason(code=r.id, source="rule", points=r.points, text=r.description))
                if r.points > 0 or r.action:
                    scenarios.add(r.scenario)
                if r.action and RANK[Action(r.action)] > RANK[floor]:
                    floor = Action(r.action)
                create_case = create_case or r.case

            # ML
            prob, degraded = None, False
            if model and ev.event_type in ML_EVENTS:
                if (time.perf_counter() - t0) * 1000 > self.budget_ms:
                    degraded = True
                else:
                    prob, top = model.predict(f)
                    ml_points = round(rs.ml_weight * prob, 1)
                    points += ml_points
                    labels = ", ".join(ml.SPEC[n][1] for n, _ in top) or "нет выраженных факторов"
                    reasons.append(Reason(code="ML_SCORE", source="ml", points=ml_points,
                                          text=f"модель: вероятность {prob:.2f}; главные факторы: {labels}"))

            score = int(max(0, min(100, round(points))))
            th = jur["thresholds"]
            action = Action.decline if score >= th["decline"] else Action.challenge if score >= th["challenge"] else Action.allow
            if RANK[floor] > RANK[action]:
                action = floor
            if action == Action.decline:
                create_case = True

            F.update(ev, self.state, f, completed=action != Action.decline)
            self.stats[action.value] += 1
            self.stats["total"] += 1

        reasons.sort(key=lambda r: -abs(r.points))
        d = Decision(event_id=ev.event_id, action=action, score=score, ml_probability=None if prob is None else round(prob, 4),
                     reasons=reasons, scenarios=sorted(scenarios - {"limits"}) if scenarios else [],
                     jurisdiction=ev.jurisdiction, rules_version=rs.version,
                     model_version=model.version if model else "none", degraded=degraded)
        if create_case and persist_case:
            d.case_id = self._open_case(ev, d, f)
            self.stats["cases"] += 1
        d.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        self.latencies.append(d.latency_ms)
        self.recent.appendleft({"ts": ev.ts.isoformat(timespec="seconds"), "event_id": ev.event_id,
                                "type": ev.event_type.value, "jurisdiction": ev.jurisdiction, "user": ev.user_id,
                                "amount": f["amount"], "currency": jur["currency"], "action": action.value,
                                "score": score, "scenarios": d.scenarios, "case_id": d.case_id,
                                "top": [r.code for r in reasons[:3]], "latency_ms": d.latency_ms})
        if self.webhooks and action == Action.decline:
            self.webhooks.emit("decision.declined", {"event_id": ev.event_id, "score": score,
                                                     "reasons": [r.code for r in reasons[:5]]})
        return d

    # ---------- инциденты ----------
    def _open_case(self, ev: Event, d: Decision, f: dict) -> int:
        p = ev.payment
        clean = {k: v for k, v in f.items() if not k.startswith("_")}
        cid = self.store.x(
            "INSERT INTO cases(event_id, created, jurisdiction, user_id, event_type, amount, currency, recipient_id,"
            " action, score, scenarios, reasons, features) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev.event_id, ev.ts.isoformat(timespec="seconds"), ev.jurisdiction, ev.user_id, ev.event_type.value,
             p.amount if p else None, p.currency if p else None, p.recipient_id if p else None,
             d.action.value, d.score, ",".join(d.scenarios),
             json.dumps([r.model_dump() for r in d.reasons], ensure_ascii=False),
             json.dumps(clean, ensure_ascii=False, default=str)))
        if self.webhooks:
            self.webhooks.emit("case.created", {"case_id": cid, "event_id": ev.event_id, "action": d.action.value,
                                                "scenarios": d.scenarios})
        return cid

    def resolve_case(self, case_id: int, status: str, actor: str, role: str, comment: str = "",
                     add_to_list: bool = False) -> dict:
        if status not in ("fraud", "legit"):
            raise ValueError("status: fraud | legit")
        rows = self.store.q("SELECT * FROM cases WHERE id=?", (case_id,))
        if not rows:
            raise KeyError(case_id)
        c = rows[0]
        self.store.x("UPDATE cases SET status=?, analyst=?, comment=?, resolved=? WHERE id=?",
                     (status, actor, comment, utcnow(), case_id))
        self.store.x("INSERT OR REPLACE INTO labels VALUES (?,?,?,?)",
                     (c["event_id"], 1 if status == "fraud" else 0, c["features"], utcnow()))
        self.store.audit(actor, role, "case.resolve", str(case_id), {"status": status, "comment": comment,
                                                                      "previous": c["status"]})
        if add_to_list and status == "fraud" and c["recipient_id"]:
            self.store.x("INSERT OR REPLACE INTO list_entries VALUES (?,?,?,?,?,?,?)",
                         ("internal", c["jurisdiction"], "wallet", c["recipient_id"], f"инцидент #{case_id}",
                          f"case:{case_id}", utcnow()))
            self.lists.reload()
            self.store.audit(actor, role, "lists.add", c["recipient_id"], {"case": case_id})
        if self.webhooks:
            self.webhooks.emit("case.resolved", {"case_id": case_id, "event_id": c["event_id"], "status": status})
        return {"case_id": case_id, "status": status}

    # ---------- метрики ----------
    def metrics(self) -> dict:
        lat = sorted(self.latencies)

        def pct(q: float) -> float | None:
            return round(lat[min(len(lat) - 1, int(q * len(lat)))], 2) if lat else None

        total = self.stats["total"] or 1
        return {"events": self.stats["total"], "allow": self.stats["allow"], "challenge": self.stats["challenge"],
                "decline": self.stats["decline"], "cases": self.stats["cases"],
                "case_share_pct": round(100 * self.stats["cases"] / total, 3),
                "engine_latency_ms": {"p50": pct(0.5), "p95": pct(0.95), "p99": pct(0.99)},
                "rule_hits": dict(self.rule_hits.most_common()),
                "rules_version": self.ruleset.version, "model_version": self.model.version if self.model else "none"}
