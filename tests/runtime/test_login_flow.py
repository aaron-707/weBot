from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.observability.execution_trace import ExecutionTrace


@dataclass(slots=True)
class BrowserController:
    headless: bool = False
    _playwright: Playwright | None = None
    _browser: Browser | None = None
    _context: BrowserContext | None = None
    _page: Page | None = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not initialized")
        return self._page

    async def open(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        self._page.set_default_timeout(20_000)
        self._page.set_default_navigation_timeout(20_000)

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()


def _credentials() -> tuple[str, str]:
    username = os.getenv("WEBOT_DEMO_LOGIN_USER", "tomsmith").strip()
    password = os.getenv("WEBOT_DEMO_LOGIN_PASS", "SuperSecretPassword!").strip()
    return username, password


async def run_login_flow_test(*, headless: bool = False) -> int:
    started = datetime.now(timezone.utc)
    t0 = perf_counter()
    out_dir = PROJECT_ROOT / "artifacts" / "runtime" / "login_flow"
    (out_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    (out_dir / "reports").mkdir(parents=True, exist_ok=True)

    screenshot_before = out_dir / "screenshots" / "before_submit.png"
    screenshot_after = out_dir / "screenshots" / "after_submit.png"
    report_path = out_dir / "reports" / "login_flow_report.json"
    username, password = _credentials()

    trace = ExecutionTrace(
        goal="Open The Internet login page, login with demo credentials, verify authenticated page",
        trace_id=f"login-flow-{started.strftime('%Y%m%d-%H%M%S')}",
        metadata={"headless": headless, "target": "the-internet.herokuapp.com"},
    )
    browser = BrowserController(headless=headless)

    try:
        await browser.open()
        page = browser.page

        await page.goto("https://the-internet.herokuapp.com/login", wait_until="domcontentloaded")
        trace.record_step(
            step=1,
            url=page.url,
            selected_action={"action": "goto", "url": page.url},
            success=True,
        )

        await page.fill("#username", username)
        await page.fill("#password", password)
        await page.screenshot(path=str(screenshot_before), full_page=True)
        trace.record_step(
            step=2,
            url=page.url,
            selected_action={"action": "fill", "selector": "#username/#password"},
            selector="#username,#password",
            success=True,
        )

        await page.click("button[type='submit']")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(600)
        await page.screenshot(path=str(screenshot_after), full_page=True)

        current_url = page.url.lower()
        body_text = (await page.locator("body").inner_text()).lower()
        flash_text = (await page.locator("#flash").inner_text()).lower() if await page.locator("#flash").count() > 0 else ""

        auth_page = "/secure" in current_url
        success_indicator = "you logged into a secure area!" in flash_text or "secure area" in body_text
        error_indicator = any(x in flash_text for x in ["your username is invalid", "your password is invalid"])
        logout_visible = await page.locator("a.button.secondary.radius").count() > 0

        validation_passed = auth_page and success_indicator and not error_indicator and logout_visible
        detected_failures: list[str] = []
        if not auth_page:
            detected_failures.append("authenticated_page_not_reached")
        if not success_indicator:
            detected_failures.append("success_indicator_missing")
        if error_indicator:
            detected_failures.append("login_error_detected")
        if not logout_visible:
            detected_failures.append("logout_control_missing")

        trace.record_step(
            step=3,
            url=page.url,
            selected_action={"action": "click", "selector": "button[type='submit']"},
            selector="button[type='submit']",
            validation={
                "success": validation_passed,
                "reason": "login_verification_complete",
                "details": {
                    "auth_page": auth_page,
                    "success_indicator": success_indicator,
                    "error_indicator": error_indicator,
                    "logout_visible": logout_visible,
                },
            },
            success=validation_passed,
        )

        trace.finalize(
            status="completed" if validation_passed else "failed",
            termination_reason="" if validation_passed else "login_validation_failed",
        )
        trace_path = trace.export_trace(out_dir / "traces" / f"{trace.trace_id}.json")

        report: dict[str, Any] = {
            "test_name": "the_internet_login_flow",
            "started_at": started.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "validation_passed": validation_passed,
            "validation_reason": "authenticated_page_reached" if validation_passed else "login_validation_failed",
            "detected_failures": detected_failures,
            "metrics": {
                "execution_duration_seconds": round(perf_counter() - t0, 3),
                "total_steps": 3,
                "auth_page_reached": auth_page,
                "success_indicator": success_indicator,
                "error_detected": error_indicator,
            },
            "artifacts": {
                "trace_path": str(trace_path),
                "screenshot_before": str(screenshot_before),
                "screenshot_after": str(screenshot_after),
                "report_path": str(report_path),
            },
            "trace_terminal_summary": trace.render_terminal_summary(),
            "trace_step_timeline": trace.render_step_timeline(),
        }
        report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")

        print("=== LOGIN TEST SUMMARY ===")
        print(f"validation_passed={validation_passed}")
        print(f"trace={trace_path}")
        print(f"report={report_path}")
        return 0 if validation_passed else 1

    except Exception as exc:  # noqa: BLE001
        trace.finalize(status="failed", termination_reason="runner_exception")
        trace_path = trace.export_trace(out_dir / "traces" / f"{trace.trace_id}.json")
        failure = {
            "test_name": "the_internet_login_flow",
            "status": "failed",
            "error": str(exc),
            "trace_path": str(trace_path),
            "execution_duration_seconds": round(perf_counter() - t0, 3),
        }
        report_path.write_text(json.dumps(failure, ensure_ascii=True, indent=2), encoding="utf-8")
        print(f"Test failed: {exc}")
        print(f"trace={trace_path}")
        print(f"report={report_path}")
        return 1
    finally:
        try:
            await browser.close()
        except Exception:
            pass


def main() -> None:
    exit_code = asyncio.run(run_login_flow_test(headless=False))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()

