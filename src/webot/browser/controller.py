from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from webot.config.settings import settings
from webot.utils.logger import get_logger

logger = get_logger(__name__)


class BrowserController:
    """Centralized Playwright browser lifecycle and interaction controller."""

    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._is_cdp: bool = False

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser page is not initialized. Call open() first.")
        return self._page

    async def open(self, cdp_url: str | None = None) -> None:
        self._playwright = await async_playwright().start()
        if cdp_url:
            self._is_cdp = True
            self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
            self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            logger.info("connected_over_cdp", extra={"cdp_url": cdp_url, "current_url": self._page.url})
        else:
            self._is_cdp = False
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._context = await self._browser.new_context(user_agent=settings.browser.user_agent)
            self._page = await self._context.new_page()
        self._page.set_default_timeout(settings.browser.timeout_ms)
        self._page.set_default_navigation_timeout(settings.browser.navigation_timeout_ms)

    async def close(self) -> None:
        errors: list[str] = []
        closers = []
        if not self._is_cdp and self._context:
            closers.append((self._context.close, "context"))
        if self._browser:
            closers.append((self._browser.close, "browser"))
        if self._playwright:
            closers.append((self._playwright.stop, "playwright"))

        for closer, label in closers:
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {exc}")
        self._context = self._browser = self._playwright = self._page = None
        if errors:
            raise RuntimeError("; ".join(errors))

    async def goto(self, url: str) -> dict[str, bool | str | None]:
        try:
            await self.page.goto(url, wait_until="domcontentloaded")
            return {"ok": True, "error": None}
        except Exception as exc:  # noqa: BLE001
            logger.warning("goto_failed", extra={"url": url, "error": str(exc)})
            return {"ok": False, "error": str(exc)}

    async def click(self, selector: str) -> dict[str, bool | str | None]:
        try:
            await self.page.click(selector)
            return {"ok": True, "error": None}
        except Exception as exc:  # noqa: BLE001
            logger.warning("click_failed", extra={"selector": selector, "error": str(exc)})
            return {"ok": False, "error": str(exc)}

    async def fill(self, selector: str, value: str) -> dict[str, bool | str | None]:
        try:
            await self.page.fill(selector, value)
            return {"ok": True, "error": None}
        except Exception as exc:  # noqa: BLE001
            logger.warning("fill_failed", extra={"selector": selector, "error": str(exc)})
            return {"ok": False, "error": str(exc)}

    async def extract_text(self, selector: str) -> str:
        return (await self.page.locator(selector).first.inner_text()).strip()

    async def screenshot(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(target), full_page=True)

    async def wait_for_stable(self, selector: str, timeout_ms: int = 5000) -> None:
        page = self.page
        await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
        locator = page.locator(selector)

        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
        while asyncio.get_event_loop().time() < deadline:
            try:
                if await locator.first.is_enabled():
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)

        if not await locator.first.is_enabled():
            raise TimeoutError(f"Element '{selector}' was not enabled within {timeout_ms}ms")

    async def set_editor_content(
        self, code: str, selector: str | None = None
    ) -> dict[str, bool | str | None]:
        """Inject code into a rich web editor (Monaco/CodeMirror/textarea)."""
        try:
            # 1. Try Monaco JavaScript API first (instant, clean, reliable)
            monaco_set = await self.page.evaluate(
                """(content) => {
                    if (window.monaco && window.monaco.editor) {
                        const models = window.monaco.editor.getModels();
                        if (models && models.length > 0) {
                            models[0].setValue(content);
                            return true;
                        }
                    }
                    return false;
                }""",
                code,
            )
            if monaco_set:
                logger.info("editor_content_set_via_monaco_api")
                return {"ok": True, "error": None}

            # 2. Try CodeMirror JavaScript API
            codemirror_set = await self.page.evaluate(
                """(content) => {
                    const cmElem = document.querySelector('.CodeMirror');
                    if (cmElem && cmElem.CodeMirror) {
                        cmElem.CodeMirror.setValue(content);
                        return true;
                    }
                    return false;
                }""",
                code,
            )
            if codemirror_set:
                logger.info("editor_content_set_via_codemirror_api")
                return {"ok": True, "error": None}

            # 3. Fallback: focus editor area and use keyboard select-all + insertion
            target_selector = selector or ".monaco-editor, [data-track-load='code_editor'], .monaco-scrollable-element, textarea"
            locator = self.page.locator(target_selector).first
            await locator.click()
            await self.page.keyboard.press("ControlOrMeta+A")
            await self.page.keyboard.press("Backspace")
            await self.page.keyboard.insert_text(code)
            logger.info("editor_content_set_via_keyboard_insertion")
            return {"ok": True, "error": None}
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_editor_content_failed", extra={"error": str(exc)})
            return {"ok": False, "error": str(exc)}
