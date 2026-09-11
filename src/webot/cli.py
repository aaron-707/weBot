"""General-purpose entrypoint: free-text prompt in, autonomous run out.

This is the missing link between the existing runtime test scripts
(each of which hardcodes a goal + starting URL for one specific site)
and the "type a prompt, it just does the work" experience.

Usage:
    python -m webot.cli "Open duckduckgo and search for python internships"
    python -m webot.cli "Login to the-internet.herokuapp.com with demo credentials" --headless

Scope note: this wires the same AgentLoop/state-machine engine the
runtime tests use, with the same dependency graph. It intentionally
does NOT replicate the full per-step ExecutionTrace/progress recording
those tests do (that logic includes site-specific result-extraction
JS that doesn't generalize to an arbitrary site) — it reports a final
status/confidence summary and a screenshot instead. Wire in
ExecutionTrace recording later if step-by-step traces are needed for
an arbitrary prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.browser.controller import BrowserController
from webot.config.settings import resolve_ollama_config_sources, settings
from webot.intelligence.dom_extractor import DomExtractor
from webot.intelligence.dom_delta import DomDelta
from webot.llm.decision_engine import DecisionEngine
from webot.llm.ollama_client import OllamaClient
from webot.memory.session_memory import SessionMemory
from webot.utils.logger import get_logger, setup_logging
from webot.workflows.action_executor import ActionExecutor
from webot.workflows.action_validator import ActionValidator
from webot.workflows.agent_loop import AgentLoop
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.workflows.progress_tracker import ProgressTracker
from webot.workflows.recovery_engine import RecoveryEngine
from webot.workflows.search_state_machine import SearchStateMachine
from webot.workflows.task_interpreter import TaskInterpreter


async def run_prompt(
    prompt: str = "",
    *,
    headless: bool = False,
    max_steps: int = 10,
    keep_open: bool = False,
    cdp_url: str | None = None,
) -> dict[str, Any]:
    """Execute a task prompt using the browser's current page or an explicit start URL."""
    logger = get_logger(__name__)

    ollama_info = resolve_ollama_config_sources(settings)
    ollama_client = OllamaClient(
        base_url=ollama_info["base_url"],
        model=ollama_info["model"],
        timeout_seconds=float(settings.ollama.timeout_seconds),
        max_retries=2,
        retry_delay_seconds=0.5,
        model_source=ollama_info["model_source"],
        base_url_source=ollama_info["base_url_source"],
    )
    llm_available = ollama_client.is_available()
    decision_engine = DecisionEngine(ollama_client=ollama_client)
    interpreter = TaskInterpreter(decision_engine=decision_engine)

    browser = BrowserController(headless=headless)
    out_dir = PROJECT_ROOT / "artifacts" / "runtime" / "cli"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        await browser.open(cdp_url=cdp_url)
        active_url = browser.page.url

        if not prompt.strip():
            if headless:
                raise ValueError("A task prompt must be provided when running in headless mode.")
            print(f"\n[weBot] Connected to browser. Active URL: {active_url or 'about:blank'}")
            try:
                prompt = (await asyncio.to_thread(input, "[weBot] Enter prompt: ")).strip()
            except (KeyboardInterrupt, EOFError):
                prompt = ""
            if not prompt:
                return {"status": "cancelled", "prompt": "", "final_url": active_url}

        explicit_site = interpreter.extract_start_url(prompt)
        start_url: str

        if active_url and active_url not in ("", "about:blank"):
            # Browser is already running on a page
            if explicit_site:
                curr_host = urlparse(active_url).netloc.lower()
                target_host = urlparse(explicit_site).netloc.lower()
                if curr_host != target_host and not curr_host.endswith("." + target_host) and not target_host.endswith("." + curr_host):
                    logger.info("navigating_to_requested_site", extra={"url": explicit_site})
                    await browser.goto(explicit_site)
                    start_url = explicit_site
                else:
                    logger.info("using_current_page", extra={"url": active_url})
                    start_url = active_url
            else:
                # Prompt has no explicit site -> run directly on whatever the browser is currently showing
                logger.info("using_current_page", extra={"url": active_url})
                start_url = active_url
        else:
            # Browser is on about:blank
            if explicit_site:
                start_url = explicit_site
                logger.info("navigating", extra={"url": start_url})
                await browser.goto(start_url)
            elif SearchStateMachine._is_search_goal(prompt):
                start_url = "https://duckduckgo.com"
                logger.info("defaulting_search_start_url", extra={"url": start_url})
                await browser.goto(start_url)
            else:
                raise ValueError(
                    "The browser is currently on a blank page and no starting website was specified in the prompt.\n"
                    "Please mention a target website or URL, e.g.:\n"
                    "  - 'open youtube and search for ...'\n"
                    "  - 'search for ... on youtube'\n"
                    "  - 'search for python internships' (defaults to DuckDuckGo when blank)\n"
                    "Or connect to an already-open browser using --cdp."
                )

        dom_extractor = DomExtractor()
        session_memory = SessionMemory(max_actions=400)
        action_executor = ActionExecutor(browser_controller=browser, max_retries=2, retry_delay_seconds=0.5)
        action_validator = ActionValidator(timeout_ms=settings.browser.timeout_ms)
        dom_delta = DomDelta(max_items_per_bucket=12)
        recovery_engine = RecoveryEngine(max_retries=2, llm_client=ollama_client)
        progress_tracker = ProgressTracker(window_size=20)
        goal_evaluator = GoalEvaluator(llm_client=ollama_client)

        agent_loop = AgentLoop(
            page=browser.page,
            dom_extractor=dom_extractor,
            decision_engine=decision_engine,
            action_executor=action_executor,
            action_validator=action_validator,
            session_memory=session_memory,
            dom_delta=dom_delta,
            recovery_engine=recovery_engine,
            max_steps=max_steps,
            max_consecutive_failures=3,
            complete_on_extract_text=True,
            llm_enabled=llm_available,
            degraded_mode=not llm_available,
            progress_tracker=progress_tracker,
            goal_evaluator=goal_evaluator,
        )

        loop_result = await agent_loop.run(prompt)

        latest_dom = await dom_extractor.extract_all(browser.page)
        progress_report = progress_tracker.evaluate_progress()
        extracted_text = ""
        final_data = loop_result.get("final_data") if isinstance(loop_result, dict) else None
        if isinstance(final_data, dict) and isinstance(final_data.get("text"), str):
            extracted_text = final_data["text"]

        goal_eval = goal_evaluator.evaluate(
            user_goal=prompt,
            current_url=browser.page.url,
            dom_summary=latest_dom,
            extracted_text=extracted_text,
            recent_actions=session_memory.get_recent_actions(limit=12),
            progress_report=progress_report,
        )

        screenshot_path = out_dir / "last_run.png"
        await browser.page.screenshot(path=str(screenshot_path), full_page=True)

        summary = {
            "prompt": prompt,
            "start_url": start_url,
            "status": loop_result.get("status") if isinstance(loop_result, dict) else "unknown",
            "steps_executed": loop_result.get("steps_executed") if isinstance(loop_result, dict) else None,
            "final_url": browser.page.url,
            "llm_available": llm_available,
            "degraded_mode": not llm_available,
            "completion_confidence": goal_eval.get("completion_confidence"),
            "screenshot": str(screenshot_path),
        }
        (out_dir / "last_run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

        while keep_open and not headless:
            # Guard: check if browser page is still alive
            try:
                if browser.page.is_closed():
                    print("[weBot] Browser page was closed. Exiting.")
                    break
                curr_url = browser.page.url
            except Exception:
                print("[weBot] Browser connection lost. Exiting.")
                break

            print(f"\n[weBot] Active URL: {curr_url}")
            try:
                next_prompt = await asyncio.to_thread(
                    input, "[weBot] Enter next prompt (or press Enter to exit): "
                )
            except (KeyboardInterrupt, EOFError):
                break
            next_prompt = next_prompt.strip()
            if not next_prompt:
                break

            # Only navigate if the follow-up explicitly requests navigation
            has_nav_intent = bool(re.search(r"\b(open|go\s+to|goto|navigate\s+to|visit|https?://)\b", next_prompt, re.IGNORECASE))
            if has_nav_intent:
                next_site = interpreter.extract_start_url(next_prompt)
                if next_site:
                    curr_host = urlparse(browser.page.url).netloc.lower()
                    target_host = urlparse(next_site).netloc.lower()
                    if curr_host != target_host and not curr_host.endswith("." + target_host) and not target_host.endswith("." + curr_host):
                        logger.info("navigating_to_requested_site", extra={"url": next_site})
                        nav_result = await browser.goto(next_site)
                        if isinstance(nav_result, dict) and not nav_result.get("ok", True):
                            err_msg = nav_result.get("error", "")
                            if "closed" in str(err_msg).lower():
                                print(f"[weBot] Browser closed unexpectedly: {err_msg}")
                                break
                            logger.warning("follow_up_navigation_failed", extra={"url": next_site, "error": err_msg})

            # Create fresh session state for each follow-up task
            follow_up_memory = SessionMemory(max_actions=400)
            follow_up_delta = DomDelta(max_items_per_bucket=12)
            sub_tracker = ProgressTracker(window_size=20)
            sub_loop = AgentLoop(
                page=browser.page,
                dom_extractor=dom_extractor,
                decision_engine=decision_engine,
                action_executor=action_executor,
                action_validator=action_validator,
                session_memory=follow_up_memory,
                dom_delta=follow_up_delta,
                recovery_engine=recovery_engine,
                max_steps=max_steps,
                max_consecutive_failures=3,
                complete_on_extract_text=True,
                llm_enabled=llm_available,
                degraded_mode=not llm_available,
                progress_tracker=sub_tracker,
                goal_evaluator=goal_evaluator,
            )

            try:
                sub_result = await sub_loop.run(next_prompt)
                sub_status = sub_result.get("status") if isinstance(sub_result, dict) else "unknown"
                print(f"[weBot] Task status: {sub_status}")
            except Exception as exc:
                err_str = str(exc).lower()
                if "closed" in err_str or "target" in err_str:
                    print(f"[weBot] Browser closed during task: {exc}")
                    break
                logger.warning("follow_up_task_failed", extra={"error": str(exc)})
                print(f"[weBot] Task failed: {exc}")

        return summary
    finally:
        try:
            await browser.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="weBot: run a free-text browser task autonomously.")
    parser.add_argument("prompt", nargs="?", default="", help="Natural-language task, e.g. 'search for X on duckduckgo'")
    parser.add_argument("--headless", action="store_true", help="Run without a visible browser window")
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument(
        "--keep-open",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep the browser window open after task completion for inspection or follow-up prompts (default: True)",
    )
    parser.add_argument(
        "--cdp",
        dest="cdp_url",
        default=None,
        help="Connect to an already running browser instance via CDP (e.g. http://127.0.0.1:9222)",
    )
    args = parser.parse_args()

    setup_logging(
        logger_name=settings.logging.logger_name,
        level=settings.logging.level,
        log_file=settings.logging.log_file,
    )

    summary = asyncio.run(
        run_prompt(
            args.prompt,
            headless=args.headless,
            max_steps=args.max_steps,
            keep_open=args.keep_open,
            cdp_url=args.cdp_url,
        )
    )
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary.get("status") in {"completed", "cancelled"} else 1)


if __name__ == "__main__":
    main()
