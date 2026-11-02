import shutil
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app import demo, ml
from app.engine import ROOT, Engine, UnknownJurisdiction
from app.evaluate import evaluate, stoplist_csv
from app.models import Action, Channel, Event, EventType, Payment, Session
from app.privacy import mask_pan, pseudonymize
from app.rules import parse
from app.store import Store
from client.fallback_client import FraudClient

T0 = datetime(2026, 11, 1, 12, 0)


@pytest.fixture
def eng(tmp_path: Path) -> Engine:
    rules = tmp_path / "rules.yaml"
    shutil.copy(ROOT / "config" / "rules.yaml", rules)
    e = Engine(store=Store(), rules_path=rules)
    e.lists.import_csv((ROOT / "lists" / "sample_stoplists.csv").read_text(encoding="utf-8"), source="test")
    return e


def pay(eid: str, user: str, amount: float, rcp: str, ts: datetime = T0, jur: str = "KZ", cur: str | None = None,
        device: str = "dev1", **sess) -> Event:
    return Event(event_id=eid, event_type=EventType.payment, ts=ts, jurisdiction=jur, user_id=user,
                 account_created=ts - timedelta(days=300),
                 session=Session(device_id=device, ip_country=jur, **sess),
                 payment=Payment(channel=Channel.p2p, amount=amount, currency=cur or {"KZ": "KZT", "UZ": "UZS", "KG": "KGS"}[jur],
                                 recipient_id=rcp, card_hash=f"card_{user}"))


def warm(e: Engine, user: str = "u1", n: int = 6) -> None:
    for i in range(n):
        e.score(pay(f"w{user}{i}", user, 8000, "w_friend", ts=T0 - timedelta(days=10 - i)))


@pytest.mark.parametrize("name", list(demo.SCENARIOS))
def test_demo_scenarios(eng: Engine, name: str) -> None:
    r = demo.run(eng, name)
    action = r["final"]["action"]
    if name == "legit":
        assert action == "allow"
    else:
        assert action in ("challenge", "decline"), r["final"]
        assert r["final"]["reasons"], "нет объяснения"


def test_known_recipient_allowed_with_reasons(eng: Engine) -> None:
    warm(eng)
    d = eng.score(pay("e1", "u1", 9000, "w_friend"))
    assert d.action == Action.allow
    assert any(r.code == "TRUST_KNOWN_RCP" for r in d.reasons)
    assert d.latency_ms < 50


def test_stoplist_declines_and_opens_case(eng: Engine) -> None:
    d = eng.score(pay("e2", "u2", 5000, "w_blk_KZ_0"))
    assert d.action == Action.decline and d.case_id
    assert "stoplist" in d.scenarios
    assert any(r.source == "list" for r in d.reasons)


def test_stoplist_is_per_jurisdiction(eng: Engine) -> None:
    d = eng.score(pay("e3", "u3", 50000, "w_blk_KZ_0", jur="UZ"))
    assert not any(r.source == "list" for r in d.reasons)


def test_limits_in_local_currency(eng: Engine) -> None:
    kz = eng.score(pay("e4", "u4", 6_000_000, "w_friend"))
    uz = eng.score(pay("e5", "u5", 6_000_000, "w_friend", jur="UZ"))
    assert kz.action == Action.decline and any(r.code == "LIMIT_MAX_SINGLE" for r in kz.reasons)
    assert not any(r.code == "LIMIT_MAX_SINGLE" for r in uz.reasons)


def test_currency_mismatch_is_flagged(eng: Engine) -> None:
    d = eng.score(pay("e6", "u6", 100, "w_friend", cur="USD"))
    assert d.action != Action.allow and any(r.code == "DQ_CURRENCY" for r in d.reasons)


def test_unknown_jurisdiction(eng: Engine) -> None:
    with pytest.raises(UnknownJurisdiction):
        eng.score(pay("e7", "u7", 100, "w", jur="ZZ", cur="KZT"))


@pytest.mark.parametrize("bad, msg", [
    ("[[no_such_feature, '>', 1]]", "неизвестный признак"),
    ("[[amount, '=~', 1]]", "неизвестный оператор"),
    ("[[amount, '>', '$limits.nope']]", "нет настройки страны"),
])
def test_rule_validation(eng: Engine, bad: str, msg: str) -> None:
    text = f"version: x\nrules:\n  - id: R1\n    scenario: limits\n    description: t\n    when: {bad}\n    points: 5\n"
    with pytest.raises(Exception) as ei:
        parse(text, eng.jur)
    assert msg in str(ei.value)


def test_rules_hot_update_and_audit(eng: Engine) -> None:
    text = eng.rules_text().replace('version: "2026-11-02.1"', 'version: "test-2"').replace(
        "points: -10", "points: -30")
    eng.update_rules(text, "analyst:test", "analyst")
    assert eng.ruleset.version == "test-2"
    assert eng.store.q("SELECT * FROM audit WHERE action='rules.update'")


def test_case_resolution_feeds_labels_and_list(eng: Engine) -> None:
    warm(eng, "u8")
    d = eng.score(pay("e8", "u8", 900000, "w_mule", on_call=True, remote_access_app=True))
    assert d.action != Action.allow
    cid = d.case_id or eng._open_case(pay("e8", "u8", 900000, "w_mule"), d, eng.last_features)
    eng.resolve_case(cid, "fraud", "analyst:test", "analyst", "подтверждено", add_to_list=True)
    assert eng.store.q("SELECT label FROM labels WHERE event_id='e8'")[0]["label"] == 1
    d2 = eng.score(pay("e9", "u9", 1000, "w_mule"))
    assert d2.action == Action.decline and any(r.source == "list" for r in d2.reasons)
    assert eng.store.q("SELECT * FROM audit WHERE action='case.resolve'")


def test_ml_budget_degrades_gracefully(tmp_path: Path) -> None:
    e = Engine(store=Store(), budget_ms=0)
    d = e.score(pay("e10", "u10", 5000, "w_x"))
    assert d.degraded and d.ml_probability is None


def test_list_import_rejects_bad_rows(eng: Engine) -> None:
    res = eng.lists.import_csv((ROOT / "lists" / "sample_stoplists.csv").read_text(encoding="utf-8"), source="t")
    assert res["rejected"] == 2 and res["imported"] >= 18
    with pytest.raises(ValueError):
        eng.lists.import_csv("a,b\n1,2\n", source="t")


def test_privacy_helpers() -> None:
    assert pseudonymize("+7 707 000 00 00", "s") == pseudonymize("+77070000000", "s")
    assert pseudonymize("a", "s1") != pseudonymize("a", "s2")
    assert mask_pan("4400 4301 2345 6789") == {"card_bin": "440043", "card_last4": "6789"}


def test_fallback_policy() -> None:
    def timeout(_req):
        raise httpx.ReadTimeout("slow")
    c = FraudClient("http://x", "k", transport=httpx.MockTransport(timeout))
    small = pay("f1", "u", 10000, "w").model_dump(mode="json")
    big = pay("f2", "u", 900000, "w").model_dump(mode="json")
    big_uz = pay("f3", "u", 90_000_000, "w", jur="UZ").model_dump(mode="json")
    assert c.score(small)["action"] == "allow"
    assert c.score(big)["action"] == "challenge" and c.deferred       # KZ: отложенная проверка
    assert c.score(big_uz)["action"] == "decline"                      # UZ: отклонить выше лимита


def test_model_contributions_are_explainable() -> None:
    m = ml.load()
    assert m is not None
    e = Engine(store=Store())
    e.score(pay("m1", "um", 5000, "w_a"))
    p, top = m.predict(e.last_features)
    assert 0 <= p <= 1 and all(n in ml.NAMES for n, _ in top)


def test_quality_on_small_synthetic_stream() -> None:
    res = evaluate(ml.load(), seed=21, users=600, days=6, incidents=8)
    assert res["detection_money_ops_pct"] >= 80
    assert res["false_positive_pct"] <= 1.0
    for s in res["scenarios"].values():
        assert s["detected_incidents_pct"] >= 60, s
    _ = stoplist_csv
