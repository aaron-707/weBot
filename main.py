from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.config.settings import settings
from webot.intelligence.dom_extractor import DomExtractor
from webot.utils.logger import get_logger, setup_logging


class BrowserController:
    """Minimal browser controller for project bootstrap flow."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def open(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=settings.browser.headless)
        self._context = await self._browser.new_context(user_agent=settings.browser.user_agent)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(settings.browser.timeout_ms)
        self._page.set_default_navigation_timeout(settings.browser.navigation_timeout_ms)

    async def goto(self, url: str) -> None:
        page = self.page
        await page.goto(url, wait_until="domcontentloaded")

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser page is not initialized. Call open() first.")
        return self._page

    async def close(self) -> None:
        close_errors: list[str] = []

        if self._context is not None:
            try:
                await self._context.close()
            except Exception as exc:  # noqa: BLE001
                close_errors.append(f"context: {exc}")

        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001
                close_errors.append(f"browser: {exc}")

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:  # noqa: BLE001
                close_errors.append(f"playwright: {exc}")

        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None

        if close_errors:
            raise RuntimeError("; ".join(close_errors))


async def run() -> int:
    setup_logging(
        logger_name=settings.logging.logger_name,
        level=settings.logging.level,
        log_file=settings.logging.log_file,
    )
    logger = get_logger(__name__)

    browser = BrowserController()
    extractor = DomExtractor()

    try:
        logger.info("starting_browser")
        await browser.open()

        target_url = "https://google.com"
        logger.info("navigating", extra={"url": target_url})
        await browser.goto(target_url)

        logger.info("extracting_visible_buttons")
        buttons = await extractor.extract_buttons(browser.page)

        clean_results: list[dict[str, Any]] = [
            {
                "type": item.get("type", "button"),
                "text": item.get("text", ""),
                "selector": item.get("selector", ""),
                "visible": bool(item.get("visible", False)),
            }
            for item in buttons
        ]

        print(json.dumps(clean_results, indent=2, ensure_ascii=True))
        logger.info("extraction_completed", extra={"button_count": len(clean_results)})
        return 0
    except Exception:
        logger.exception("main_execution_failed")
        return 1
    finally:
        try:
            await browser.close()
            logger.info("browser_closed")
        except Exception:
            logger.exception("browser_close_failed")


def main() -> None:
    exit_code = asyncio.run(run())
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
