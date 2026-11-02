"""Прогон синтетического потока через движок и расчёт показателей.

Модель обучается на одном синтетическом потоке (seed обучения), а проверяется
на другом (seed проверки) — чтобы не оценивать модель на тех же данных.

    python -m app.evaluate            # отчёт в консоль и reports/evaluation.json
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import ml
from .engine import ML_EVENTS, Engine
from .lists import Lists
from .models import Action
from .store import Store
from .synthetic import generate

ROOT = Path(__file__).resolve().parent.parent
SCEN_RU = {
    "social_engineering": "Социальная инженерия", "account_takeover": "Захват учётной записи",
    "compromised_cards": "Скомпрометированные карты", "droppers": "Дропперские схемы",
    "multiaccount": "Мультиаккаунты", "stoplist": "Получатель в стоп-листе",
}


def stoplist_csv(entries: list[dict]) -> str:
    lines = ["list_name,jurisdiction,entity_type,value,reason"]
    lines += [f"{e['list_name']},{e['jurisdiction']},{e['entity_type']},{e['value']},{e['reason']}" for e in entries]
    return "\n".join(lines) + "\n"


def make_engine(stoplists: list[dict], model: ml.Model | None, ml_enabled: bool = True) -> Engine:
    eng = Engine(store=Store(), model=model, ml_enabled=ml_enabled, budget_ms=1e9)
    eng.lists = Lists(eng.store)
    eng.lists.import_csv(stoplist_csv(stoplists), source="synthetic")
    return eng


def collect_training(seed: int, users: int, days: int, incidents: int) -> tuple[np.ndarray, np.ndarray]:
    rows, stop = generate(seed=seed, users=users, days=days, incidents=incidents)
    eng = make_engine(stop, None, ml_enabled=False)
    X, y = [], []
    for r in rows:
        eng.score(r.event, persist_case=False)
        if r.event.event_type in ML_EVENTS:
            X.append(ml.vectorize(eng.last_features))
            y.append(r.label)
    return np.array(X), np.array(y)


def train(seed: int = 101, users: int = 2500, days: int = 14, incidents: int = 30) -> ml.Model:
    X, y = collect_training(seed, users, days, incidents)
    m = ml.fit(X, y, version=f"coldstart-synthetic-{ml.stamp()}",
               meta={"trained_on": f"synthetic seed={seed}, users={users}, days={days}",
                     "rows": int(len(y)), "fraud_rows": int(y.sum()),
                     "note": "предобучение на вымышленных типологиях; дообучается на данных заказчика"})
    return m


def evaluate(model: ml.Model | None, seed: int = 7, users: int = 2500, days: int = 14, incidents: int = 25,
             ml_enabled: bool = True) -> dict:
    rows, stop = generate(seed=seed, users=users, days=days, incidents=incidents)
    eng = make_engine(stop, model, ml_enabled)
    per_s = defaultdict(lambda: {"ops": 0, "flagged": 0, "declined": 0, "incidents": set(), "inc_hit": set()})
    legit = {"ops": 0, "flagged": 0, "declined": 0}
    legit_notes = defaultdict(lambda: [0, 0])
    cases = legit_cases = 0
    money_ops = defaultdict(int)
    money_hit = defaultdict(int)
    t0 = time.perf_counter()
    for r in rows:
        d = eng.score(r.event)
        flagged = d.action != Action.allow
        cases += d.case_id is not None
        if r.label == 0:
            legit_cases += d.case_id is not None
        money = r.event.event_type.value in ("payment", "bonus_claim")
        if r.label and money:
            money_ops[r.scenario] += 1
            money_hit[r.scenario] += flagged
        if r.label:
            s = per_s[r.scenario]
            s["ops"] += 1
            s["flagged"] += flagged
            s["declined"] += d.action == Action.decline
            s["incidents"].add(r.incident)
            if flagged:
                s["inc_hit"].add(r.incident)
        else:
            legit["ops"] += 1
            legit["flagged"] += flagged
            legit["declined"] += d.action == Action.decline
            if r.note:
                legit_notes[r.note][0] += 1
                legit_notes[r.note][1] += flagged
    elapsed = time.perf_counter() - t0
    lat = sorted(eng.latencies)
    total = len(rows)
    fraud_ops = sum(s["ops"] for s in per_s.values())
    fraud_flag = sum(s["flagged"] for s in per_s.values())
    out = {
        "data": f"ВЫМЫШЛЕННЫЕ ДАННЫЕ: synthetic seed={seed}, users={users}, days={days}",
        "model": model.version if (model and ml_enabled) else "без ML (только правила)",
        "events": total,
        "fraud_events": fraud_ops,
        "detection_ops_pct": round(100 * fraud_flag / max(fraud_ops, 1), 1),
        "detection_money_ops_pct": round(100 * sum(money_hit.values()) / max(sum(money_ops.values()), 1), 1),
        "false_positive_pct": round(100 * legit["flagged"] / max(legit["ops"], 1), 3),
        "false_decline_pct": round(100 * legit["declined"] / max(legit["ops"], 1), 3),
        "auto_decision_pct": round(100 * (1 - cases / total), 3),
        "case_share_pct": round(100 * cases / total, 3),
        "case_share_legit_pct": round(100 * legit_cases / max(legit["ops"], 1), 3),
        "scenarios": {
            k: {"name": SCEN_RU[k], "ops": v["ops"], "detected_ops_pct": round(100 * v["flagged"] / v["ops"], 1),
                "declined_ops_pct": round(100 * v["declined"] / v["ops"], 1),
                "money_ops": money_ops[k],
                "detected_money_ops_pct": round(100 * money_hit[k] / max(money_ops[k], 1), 1),
                "incidents": len(v["incidents"]),
                "detected_incidents_pct": round(100 * len(v["inc_hit"]) / len(v["incidents"]), 1)}
            for k, v in sorted(per_s.items(), key=lambda kv: list(SCEN_RU).index(kv[0]))},
        "legit_lookalikes": {k: {"ops": v[0], "flagged": v[1]} for k, v in legit_notes.items()},
        "engine_latency_ms": {"p50": lat[len(lat) // 2], "p95": lat[int(0.95 * len(lat))],
                              "p99": lat[int(0.99 * len(lat))], "max": lat[-1]},
        "throughput_single_thread_eps": round(total / elapsed),
        "top_rules": dict(eng.rule_hits.most_common(12)),
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--users", type=int, default=2500)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--no-ml", action="store_true")
    a = ap.parse_args()
    model = ml.load()
    res = evaluate(model, seed=a.seed, users=a.users, days=a.days, ml_enabled=not a.no_ml)
    if not a.no_ml:
        res["rules_only"] = {k: v for k, v in evaluate(None, seed=a.seed, users=a.users, days=a.days,
                                                       ml_enabled=False).items()
                             if k in ("detection_ops_pct", "detection_money_ops_pct", "false_positive_pct",
                                      "false_decline_pct", "scenarios")}
    out = ROOT / "reports" / "evaluation.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
