"""Нагрузочный тест API: постоянный поток N событий в секунду, замер времени ответа.

    python -m uvicorn app.api:app --port 8000          # в другом окне
    python scripts/loadtest.py --rps 100 --seconds 60

Время считается на стороне клиента (как в ТЗ: время ответа API для заказчика),
включая HTTP. События берутся из вымышленного потока.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.synthetic import generate  # noqa: E402


async def run(url: str, key: str, rps: float, seconds: int, events: list[dict]) -> dict:
    total = int(rps * seconds)
    lat: list[float] = []
    errors: dict[str, int] = {}
    late = 0
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    async with httpx.AsyncClient(base_url=url, headers={"X-API-Key": key}, timeout=5.0, limits=limits) as c:
        async def one(ev: dict) -> None:
            t = time.perf_counter()
            try:
                r = await c.post("/v1/score", json=ev)
                if r.status_code != 200:
                    errors[str(r.status_code)] = errors.get(str(r.status_code), 0) + 1
                    return
                lat.append((time.perf_counter() - t) * 1000)
            except httpx.HTTPError as e:
                errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1

        start = time.perf_counter()
        tasks = []
        for i in range(total):
            target = start + i / rps
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                if delay < -0.05:
                    late += 1
                await asyncio.sleep(0)
            tasks.append(asyncio.create_task(one(events[i % len(events)])))
        await asyncio.gather(*tasks)
        wall = time.perf_counter() - start
    return {"sent": total, "lat": lat, "errors": errors, "wall": wall, "late": late}


def worker(args: tuple) -> dict:
    url, key, rps, seconds, events = args
    return asyncio.run(run(url, key, rps, seconds, events))


def main(url: str, key: str, rps: int, seconds: int, procs: int) -> dict:
    rows, _ = generate(seed=303, users=3000, days=7, incidents=20)
    events = [r.event.model_dump(mode="json") for r in rows]
    stamp = int(time.time())
    for i, e in enumerate(events):  # уникальные id для повторных прогонов
        e["event_id"] = f"LT{stamp}-{i}"
    # разные клиенты на разные процессы, чтобы поведение клиентов не перемешивалось
    chunks = [[e for e in events if hash(e["user_id"]) % procs == k] for k in range(procs)]
    if procs == 1:
        parts = [worker((url, key, rps, seconds, events))]
    else:
        from multiprocessing import Pool
        with Pool(procs) as pool:
            parts = pool.map(worker, [(url, key, rps / procs, seconds, ch) for ch in chunks])
    lat = sorted(x for p in parts for x in p["lat"])
    errors: dict[str, int] = {}
    for p in parts:
        for k, v in p["errors"].items():
            errors[k] = errors.get(k, 0) + v
    total = sum(p["sent"] for p in parts)
    wall = max(p["wall"] for p in parts)
    late = sum(p["late"] for p in parts)

    def p(q: float) -> float:
        return round(lat[min(len(lat) - 1, int(q * len(lat)))], 1) if lat else float("nan")

    return {"target_rps": rps, "seconds": seconds, "client_procs": procs, "sent": total, "ok": len(lat),
            "errors": errors, "achieved_rps": round(total / wall, 1), "late_sends": late,
            "latency_ms": {"p50": p(0.5), "p95": p(0.95), "p99": p(0.99), "max": round(lat[-1], 1) if lat else None},
            "within_200ms_pct": round(100 * sum(x <= 200 for x in lat) / max(len(lat), 1), 2),
            "within_500ms_pct": round(100 * sum(x <= 500 for x in lat) / max(len(lat), 1), 2)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--key", default="demo-integration-key")
    ap.add_argument("--rps", type=int, default=100)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--procs", type=int, default=1, help="процессов-клиентов (для 500+ событий/с)")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    res = main(a.url, a.key, a.rps, a.seconds, a.procs)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if a.out:
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
