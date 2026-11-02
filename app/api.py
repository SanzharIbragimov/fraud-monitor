"""HTTP API антифрод-контура и рабочее место аналитика.

Роли и ключи (заголовок X-API-Key) задаются в .env, см. .env.example:
  integration — только оценка событий (бэкенд платёжного приложения);
  analyst     — инциденты, правила, стоп-листы, отчёты;
  admin       — всё, плюс вебхуки и переобучение модели.
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from . import demo, ml
from .engine import ROOT, Engine, UnknownJurisdiction
from .features import FEATURES
from .models import Decision, Event
from .store import Store, utcnow
from .webhooks import EVENTS as WEBHOOK_EVENTS
from .webhooks import Webhooks

DB_PATH = os.getenv("DB_PATH", str(ROOT / "data" / "fraud.db"))
API_KEYS = os.getenv("API_KEYS", "integration:demo-integration-key,analyst:demo-analyst-key,admin:demo-admin-key")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "demo-webhook-secret")
BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
DEMO_MODE = os.getenv("DEMO_MODE", "1") == "1"
LISTS_INBOX = Path(os.getenv("LISTS_INBOX", str(ROOT / "lists" / "inbox")))

KEYS: dict[str, str] = {}
for pair in API_KEYS.split(","):
    role, _, key = pair.partition(":")
    if key:
        KEYS[key.strip()] = role.strip()
ROLE_RANK = {"integration": 0, "analyst": 1, "admin": 2}

store = Store(DB_PATH)
webhooks = Webhooks(store, WEBHOOK_SECRET)
engine = Engine(store=store, webhooks=webhooks,
                rules_path=Path(os.getenv("RULES_PATH", str(ROOT / "config" / "rules.yaml"))))
model_status = {"state": "ready", "message": ""}
sink: list[dict] = []

@asynccontextmanager
async def lifespan(_app: FastAPI):
    startup()
    yield


app = FastAPI(lifespan=lifespan, title="Fraud Monitor — антифрод-контур платёжного приложения (прототип)", version="0.1.0",
              description="Оценка риска операций и событий учётной записи в реальном времени. "
                          "Прототип к отклику на задачу Astana Hub (Monexsol). Данные демо — вымышленные.")


class Caller:
    def __init__(self, role: str, key: str) -> None:
        self.role, self.actor = role, f"{role}:{key[-4:]}"


def need(min_role: str):
    def dep(x_api_key: str = Header(..., alias="X-API-Key")) -> Caller:
        role = KEYS.get(x_api_key)
        if role is None:
            raise HTTPException(401, "неверный API-ключ")
        if ROLE_RANK.get(role, -1) < ROLE_RANK[min_role]:
            raise HTTPException(403, f"нужна роль {min_role}")
        return Caller(role, x_api_key)
    return dep


def startup() -> None:
    src = ROOT / "lists" / "sample_stoplists.csv"
    if src.exists() and not engine.lists.index:
        res = engine.lists.import_csv(src.read_text(encoding="utf-8"), source=f"file:{src.name}")
        store.audit("system", "system", "lists.import", src.name, {"imported": res["imported"], "rejected": res["rejected"]})
    LISTS_INBOX.mkdir(parents=True, exist_ok=True)
    engine.lists.start_watcher(LISTS_INBOX, int(os.getenv("LISTS_POLL_SEC", "30")))
    if DEMO_MODE:
        if not webhooks.list():
            webhooks.subscribe(f"{BASE_URL}/demo/webhook-sink", sorted(WEBHOOK_EVENTS))
        if os.getenv("DEMO_WARMUP", "1") == "1" and not store.q("SELECT id FROM cases LIMIT 1"):
            threading.Thread(target=_warmup, daemon=True).start()


def _warmup() -> None:
    """Наполнить панель: прогнать 3 дня вымышленного потока (≈5 тыс. событий)."""
    from .synthetic import generate
    rows, _ = generate(seed=11, users=500, days=3, incidents=6)
    for r in rows:
        engine.score(r.event)


# ---------- оценка ----------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "rules_version": engine.ruleset.version,
            "model_version": engine.model.version if engine.model else "none"}


@app.post("/v1/score", response_model=Decision, tags=["оценка"],
          summary="Оценить событие до проведения: allow / challenge / decline")
def score(ev: Event, c: Caller = Depends(need("integration"))) -> Decision:
    try:
        return engine.score(ev)
    except UnknownJurisdiction as e:
        raise HTTPException(422, str(e))


# ---------- правила ----------
@app.get("/v1/rules", tags=["правила"], response_class=PlainTextResponse)
def get_rules(c: Caller = Depends(need("analyst"))) -> str:
    return engine.rules_text()


@app.post("/v1/rules/validate", tags=["правила"])
def validate_rules(text: str = Body(..., media_type="text/plain"), c: Caller = Depends(need("analyst"))) -> dict:
    from .rules import parse
    try:
        rs = parse(text, engine.jur)
    except Exception as e:  # noqa: BLE001 — показать аналитику причину
        return {"valid": False, "error": str(e)}
    return {"valid": True, "version": rs.version, "rules": len(rs.rules)}


@app.put("/v1/rules", tags=["правила"])
def put_rules(text: str = Body(..., media_type="text/plain"), c: Caller = Depends(need("analyst"))) -> dict:
    try:
        rs = engine.update_rules(text, c.actor, c.role)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"правила не приняты: {e}")
    return {"ok": True, "version": rs.version, "rules": len(rs.rules)}


@app.get("/v1/features", tags=["правила"])
def features(c: Caller = Depends(need("analyst"))) -> dict:
    return FEATURES


@app.get("/v1/jurisdictions", tags=["правила"])
def jurisdictions(c: Caller = Depends(need("analyst"))) -> dict:
    return engine.jur


# ---------- стоп-листы ----------
@app.get("/v1/lists", tags=["стоп-листы"])
def lists(c: Caller = Depends(need("analyst"))) -> list[dict]:
    return engine.lists.summary()


@app.post("/v1/lists/import", tags=["стоп-листы"],
          summary="Импорт CSV: list_name,jurisdiction,entity_type,value,reason")
def import_list(text: str = Body(..., media_type="text/csv"), replace: bool = False,
                c: Caller = Depends(need("analyst"))) -> dict:
    try:
        res = engine.lists.import_csv(text, source=f"api:{c.actor}", replace_list=replace)
    except ValueError as e:
        raise HTTPException(422, str(e))
    store.audit(c.actor, c.role, "lists.import", ",".join(res["lists"]), {"imported": res["imported"], "rejected": res["rejected"]})
    webhooks.emit("lists.updated", {"lists": res["lists"], "imported": res["imported"]})
    return res


# ---------- инциденты ----------
def _case_row(r: dict) -> dict:
    r = dict(r)
    r["reasons"] = json.loads(r["reasons"]) if r.get("reasons") else []
    r["features"] = json.loads(r["features"]) if r.get("features") else {}
    r["scenarios"] = [s for s in (r.get("scenarios") or "").split(",") if s]
    return r


@app.get("/v1/cases", tags=["инциденты"])
def cases(status: str = "open", limit: int = 50, c: Caller = Depends(need("analyst"))) -> list[dict]:
    rows = store.q("SELECT id, event_id, created, jurisdiction, user_id, event_type, amount, currency, action, score,"
                   " scenarios, status, analyst FROM cases WHERE (?='all' OR status=?) ORDER BY score DESC, id DESC LIMIT ?",
                   (status, status, limit))
    for r in rows:
        r["scenarios"] = [s for s in (r["scenarios"] or "").split(",") if s]
    return rows


@app.get("/v1/cases/{case_id}", tags=["инциденты"])
def case(case_id: int, c: Caller = Depends(need("analyst"))) -> dict:
    rows = store.q("SELECT * FROM cases WHERE id=?", (case_id,))
    if not rows:
        raise HTTPException(404, "инцидент не найден")
    out = _case_row(rows[0])
    out["history"] = store.q("SELECT ts, actor, action, details FROM audit WHERE object=? AND action LIKE 'case.%' "
                             "ORDER BY id", (str(case_id),))
    store.audit(c.actor, c.role, "case.view", str(case_id))
    return out


@app.post("/v1/cases/{case_id}/resolve", tags=["инциденты"])
def resolve(case_id: int, body: dict = Body(..., examples=[{"status": "fraud", "comment": "подтверждено клиентом",
                                                             "add_to_list": True}]),
            c: Caller = Depends(need("analyst"))) -> dict:
    try:
        return engine.resolve_case(case_id, body.get("status", ""), c.actor, c.role, body.get("comment", ""),
                                   bool(body.get("add_to_list")))
    except KeyError:
        raise HTTPException(404, "инцидент не найден")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/v1/audit", tags=["инциденты"])
def audit(limit: int = 100, c: Caller = Depends(need("analyst"))) -> list[dict]:
    return store.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))


# ---------- мониторинг и отчёты ----------
@app.get("/v1/decisions/recent", tags=["мониторинг"])
def recent(limit: int = 30, c: Caller = Depends(need("analyst"))) -> list[dict]:
    return list(engine.recent)[:limit]


@app.get("/v1/metrics", tags=["мониторинг"])
def metrics(c: Caller = Depends(need("analyst"))) -> dict:
    m = engine.metrics()
    m["cases_open"] = store.q("SELECT COUNT(*) n FROM cases WHERE status='open'")[0]["n"]
    m["labels"] = store.q("SELECT COUNT(*) n FROM labels")[0]["n"]
    return m


def _csv(rows: list[dict], name: str) -> Response:
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/v1/reports/cases.csv", tags=["отчёты"])
def report_cases(date_from: str = "", date_to: str = "9999", c: Caller = Depends(need("analyst"))) -> Response:
    rows = store.q("SELECT id, created, jurisdiction, user_id, event_type, amount, currency, action, score, scenarios,"
                   " status, analyst, comment, resolved FROM cases WHERE created>=? AND created<=? ORDER BY id",
                   (date_from, date_to))
    store.audit(c.actor, c.role, "report.cases", "", {"from": date_from, "to": date_to, "rows": len(rows)})
    return _csv(rows, "cases.csv")


@app.get("/v1/reports/rules.csv", tags=["отчёты"])
def report_rules(c: Caller = Depends(need("analyst"))) -> Response:
    desc = {r.id: (r.scenario, r.description) for r in engine.ruleset.rules}
    rows = [{"rule": k, "scenario": desc.get(k, ("", ""))[0], "description": desc.get(k, ("", ""))[1], "hits": v}
            for k, v in engine.rule_hits.most_common()]
    return _csv(rows, "rule_hits.csv")


# ---------- вебхуки ----------
@app.post("/v1/webhooks", tags=["вебхуки"])
def add_webhook(body: dict = Body(..., examples=[{"url": "https://example.org/hook", "events": ["case.resolved"]}]),
                c: Caller = Depends(need("admin"))) -> dict:
    try:
        wid = webhooks.subscribe(body["url"], body.get("events", sorted(WEBHOOK_EVENTS)))
    except (KeyError, ValueError) as e:
        raise HTTPException(422, str(e))
    store.audit(c.actor, c.role, "webhook.add", body["url"], body.get("events"))
    return {"id": wid}


@app.get("/v1/webhooks", tags=["вебхуки"])
def get_webhooks(c: Caller = Depends(need("analyst"))) -> dict:
    return {"subscriptions": webhooks.list(), "deliveries": webhooks.delivered[:20],
            "signature": "X-Signature = HMAC-SHA256(тело запроса, секрет)"}


@app.post("/demo/webhook-sink", tags=["демо"], include_in_schema=False)
async def webhook_sink(request: Request) -> dict:
    body = await request.body()
    ok = request.headers.get("X-Signature") == webhooks.sign(body)
    sink.insert(0, {"received": utcnow(), "signature_ok": ok, **json.loads(body)})
    del sink[50:]
    return {"ok": True}


@app.get("/demo/webhook-sink", tags=["демо"])
def webhook_sink_list() -> list[dict]:
    return sink[:20]


# ---------- модель ----------
@app.get("/v1/model", tags=["модель"])
def model_info(c: Caller = Depends(need("analyst"))) -> dict:
    m = engine.model
    return {"status": model_status, "version": m.version if m else None, "meta": m.meta if m else {},
            "weights": dict(sorted(zip(ml.NAMES, (round(x, 3) for x in m.coef)), key=lambda kv: -abs(kv[1])))
            if m else {}}


def _retrain(actor: str, role: str) -> None:
    from .evaluate import collect_training
    try:
        model_status.update(state="training", message="")
        X, y = collect_training(seed=101, users=2500, days=14, incidents=30)
        labels = store.q("SELECT label, features FROM labels")
        if labels:
            Xl = np.array([ml.vectorize(json.loads(r["features"])) for r in labels])
            yl = np.array([r["label"] for r in labels])
            reps = 20  # метки аналитиков заказчика весомее синтетики
            X, y = np.vstack([X] + [Xl] * reps), np.concatenate([y] + [yl] * reps)
        m = ml.fit(X, y, version=f"retrained-{ml.stamp()}",
                   meta={"synthetic_rows": int(len(y) - len(labels) * 20 if labels else len(y)),
                         "customer_labels": len(labels), "trained_at": utcnow()})
        ml.save(m)
        engine.model = m
        store.audit(actor, role, "model.retrain", m.version, {"customer_labels": len(labels)})
        model_status.update(state="ready", message=f"обучена {m.version}, меток заказчика: {len(labels)}")
    except Exception as e:  # noqa: BLE001
        model_status.update(state="error", message=str(e))


@app.post("/v1/model/retrain", tags=["модель"], status_code=202)
def retrain(c: Caller = Depends(need("admin"))) -> dict:
    if model_status["state"] == "training":
        raise HTTPException(409, "обучение уже идёт")
    threading.Thread(target=_retrain, args=(c.actor, c.role), daemon=True).start()
    return {"accepted": True}


# ---------- демо и панель ----------
@app.get("/demo/scenarios", tags=["демо"])
def demo_list() -> dict:
    return demo.SCENARIOS


@app.post("/demo/scenario/{name}", tags=["демо"])
def demo_run(name: str) -> dict:
    if not DEMO_MODE:
        raise HTTPException(404)
    try:
        return demo.run(engine, name)
    except KeyError:
        raise HTTPException(404, "нет такого сценария")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    html = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    return html.replace("{{DEMO_KEY}}", next((k for k, r in KEYS.items() if r == "analyst"), "") if DEMO_MODE else "")

