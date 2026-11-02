import json
from datetime import datetime

from fastapi.testclient import TestClient

from app.api import app, webhooks

INT = {"X-API-Key": "demo-integration-key"}
AN = {"X-API-Key": "demo-analyst-key"}

EVENT = {
    "event_id": "api-1", "event_type": "payment", "ts": datetime(2026, 11, 2, 10, 0).isoformat(),
    "jurisdiction": "KZ", "user_id": "api_user",
    "session": {"device_id": "api_dev", "ip_country": "KZ"},
    "payment": {"channel": "p2p", "amount": 15000, "currency": "KZT", "recipient_id": "w_blk_KZ_1",
                "card_hash": "tok_1", "card_bin": "440043", "card_last4": "1234"},
}


def test_api_flow() -> None:
    with TestClient(app) as c:
        assert c.post("/v1/score", json=EVENT).status_code == 422  # нет ключа
        assert c.post("/v1/score", json=EVENT, headers={"X-API-Key": "wrong"}).status_code == 401
        r = c.post("/v1/score", json=EVENT, headers=INT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["action"] == "decline" and d["case_id"] and d["reasons"]

        assert c.get("/v1/cases", headers=INT).status_code == 403  # роль integration не видит инциденты
        cases = c.get("/v1/cases", headers=AN).json()
        assert any(x["id"] == d["case_id"] for x in cases)
        r = c.post(f"/v1/cases/{d['case_id']}/resolve", json={"status": "fraud", "comment": "ok"}, headers=AN)
        assert r.status_code == 200
        assert c.get(f"/v1/cases/{d['case_id']}", headers=AN).json()["status"] == "fraud"

        bad = dict(EVENT, event_id="api-2", payment=dict(EVENT["payment"], card_bin="12ab"))
        assert c.post("/v1/score", json=bad, headers=INT).status_code == 422

        r = c.put("/v1/rules", content="rules: [", headers={**AN, "Content-Type": "text/plain"})
        assert r.status_code == 422
        rules = c.get("/v1/rules", headers=AN).text
        assert c.post("/v1/rules/validate", content=rules, headers={**AN, "Content-Type": "text/plain"}).json()["valid"]

        csv_text = "list_name,jurisdiction,entity_type,value,reason\ninternal,KG,wallet,w_kg_bad,тест\n"
        r = c.post("/v1/lists/import", content=csv_text, headers={**AN, "Content-Type": "text/csv"})
        assert r.json()["imported"] == 1

        assert "cases.csv" in c.get("/v1/reports/cases.csv", headers=AN).headers["content-disposition"]
        assert c.get("/v1/audit", headers=AN).json()
        spec = c.get("/openapi.json").json()
        assert "/v1/score" in spec["paths"]


def test_webhook_signature_checked_by_sink() -> None:
    with TestClient(app) as c:
        body = json.dumps({"event": "case.created", "data": {"case_id": 1}}).encode()
        c.post("/demo/webhook-sink", content=body, headers={"X-Signature": webhooks.sign(body)})
        c.post("/demo/webhook-sink", content=body, headers={"X-Signature": "forged"})
        got = c.get("/demo/webhook-sink").json()
        assert got[0]["signature_ok"] is False and got[1]["signature_ok"] is True
