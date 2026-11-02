"""Скриншоты панели для документов (нужен запущенный сервер и playwright).

    python scripts/screenshots.py
"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path(__file__).resolve().parent.parent / "docs"
URL = "http://127.0.0.1:8000"


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1360, "height": 900}, device_scale_factor=1.5)
        await pg.goto(URL + "/")
        await pg.wait_for_timeout(2500)
        await pg.screenshot(path=OUT / "screen_monitoring.png")
        await pg.click("nav button[data-t=scen]")
        await pg.wait_for_timeout(500)
        await pg.click("#scenBtns button:nth-child(3)")
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path=OUT / "screen_scenario_ato.png", full_page=True)
        await pg.click("#scenBtns button:nth-child(2)")
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path=OUT / "screen_scenario_se.png", full_page=True)
        await pg.click("nav button[data-t=cases]")
        await pg.wait_for_timeout(1200)
        await pg.click("#caseList tr.click")
        await pg.wait_for_timeout(1000)
        await pg.fill("#cmt", "Клиент подтвердил звонок от «службы безопасности»")
        await pg.check("#toList")
        await pg.click("button.r")
        await pg.wait_for_timeout(1500)
        await pg.screenshot(path=OUT / "screen_cases.png", full_page=False)
        await pg.click("nav button[data-t=rules]")
        await pg.wait_for_timeout(1200)
        await pg.click("text=Проверить")
        await pg.wait_for_timeout(600)
        await pg.screenshot(path=OUT / "screen_rules.png")
        await pg.click("nav button[data-t=lists]")
        await pg.wait_for_timeout(800)
        await pg.screenshot(path=OUT / "screen_lists.png")
        await pg.click("nav button[data-t=hooks]")
        await pg.wait_for_timeout(1000)
        await pg.screenshot(path=OUT / "screen_webhooks.png")
        await pg.click("nav button[data-t=audit]")
        await pg.wait_for_timeout(800)
        await pg.screenshot(path=OUT / "screen_audit.png")
        await pg.goto(URL + "/docs")
        await pg.wait_for_timeout(2500)
        await pg.screenshot(path=OUT / "screen_swagger.png")
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())
