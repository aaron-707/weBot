from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.browser.controller import BrowserController
from webot.config.settings import settings
from webot.intelligence.dom_extractor import DomExtractor
from webot.utils.logger import get_logger, setup_logging


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
