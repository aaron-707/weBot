from __future__ import annotations

import sys
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webot.intelligence.interstitial_detector import InterstitialDetector
from webot.workflows.action_validator import ActionValidator
from webot.workflows.goal_evaluator import GoalEvaluator


CLOUDFLARE_CHALLENGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <title>Just a moment... | Protected Domain</title>
</head>
<body>
    <div class="cf-challenge">
        <h1 id="cf-challenge-title">Verify you are human</h1>
        <p>Please wait while we verify your browser before proceeding.</p>
        <div id="challenge-stage">
            <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/orchestrate/chk_js/ch"></iframe>
        </div>
    </div>
</body>
</html>
"""

GOOGLE_SORRY_CAPTCHA_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Sorry...</title>
</head>
<body>
    <div id="recaptcha">
        <h1>Our systems have detected unusual traffic from your computer network.</h1>
        <p>Please solve the captcha below to continue searching.</p>
        <form action="index" method="post">
            <div class="g-recaptcha" data-sitekey="6LfwuyUTAAAAAOAmoS0fdqijA2Pnoq1H1JyD2TUQ"></div>
            <input type="submit" value="Submit">
        </form>
    </div>
</body>
</html>
"""

AWS_WAF_ROBOT_CHECK_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Robot Check</title>
</head>
<body>
    <p>To discuss automated access to Amazon data please contact api-services-support@amazon.com.</p>
    <p>Enter the characters you see below</p>
    <div id="captcha">
        <img src="https://images-na.ssl-images-amazon.com/captcha/example.jpg">
    </div>
</body>
</html>
"""

ACCESS_DENIED_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Access Denied - 403 Forbidden</title>
</head>
<body>
    <h1>Access Denied</h1>
    <p>You don't have permission to access "http://example.com" on this server.</p>
</body>
</html>
"""


def test_interstitial_detector_identifies_cloudflare_challenge() -> None:
    detector = InterstitialDetector()
    result = detector.detect(
        url="https://example.com/cdn-cgi/challenge-platform/chk_js",
        title="Just a moment...",
        text="Verify you are human. cf-challenge running in background.",
    )
    assert result["detection_type"] == "anti_bot"
    assert result["confidence"] >= 0.67
    assert any("verify you are human" in s for s in result["matched_signals"])
    assert result["recommended_action"] == "pivot_away_from_direct_navigation"


def test_interstitial_detector_identifies_google_sorry_url_and_unusual_traffic() -> None:
    detector = InterstitialDetector()
    result = detector.detect(
        url="https://www.google.com/sorry/index?continue=https://www.google.com/search%3Fq%3Dpython",
        title="Sorry...",
        text="Our systems have detected unusual traffic from your computer network. Solve captcha.",
    )
    assert result["detection_type"] == "anti_bot"
    assert "url:/sorry/" in result["matched_signals"]
    assert any("unusual traffic" in s for s in result["matched_signals"])
    assert any("captcha" in s for s in result["matched_signals"])


def test_interstitial_detector_identifies_aws_robot_check() -> None:
    detector = InterstitialDetector()
    result = detector.detect(
        url="https://amazon.com/errors/validateCaptcha",
        title="Robot Check",
        text="Enter characters seen below. captcha image.",
    )
    assert result["detection_type"] == "anti_bot"
    assert any("robot check" in s for s in result["matched_signals"])
    assert any("captcha" in s for s in result["matched_signals"])


def test_interstitial_detector_identifies_access_denied() -> None:
    detector = InterstitialDetector()
    result = detector.detect(
        url="https://example.com/protected",
        title="Access Denied",
        text="Forbidden. You don't have permission to access on this server.",
    )
    assert result["detection_type"] == "access_denied"
    assert result["recommended_action"] == "terminate_or_change_target"


def test_interstitial_detector_ignores_benign_coding_challenge() -> None:
    detector = InterstitialDetector()
    result = detector.detect(
        url="https://techjobs.example/python-coding-challenge",
        title="Python Coding Challenge 2026",
        text="Join our 30-Day Python Coding Challenge and accept the challenge!",
    )
    assert result["detection_type"] == "none"
    assert result["confidence"] == 0.0


def test_goal_evaluator_triggers_blocked_by_anti_bot_on_genuine_markup() -> None:
    evaluator = GoalEvaluator()
    eval_result = evaluator.evaluate(
        user_goal="Search for python jobs",
        current_url="https://www.google.com/sorry/index",
        dom_summary={"recaptcha": []},
        extracted_text="Our systems have detected unusual traffic. Solve captcha to continue.",
        recent_actions=[{"action": "goto", "url": "https://www.google.com/search?q=python"}],
        progress_report={"progress_score": 0.0, "signals": {}},
    )
    assert eval_result["completion_reason"] == "blocked_by_anti_bot"
    assert eval_result["should_terminate"] is True
    assert eval_result["completion_confidence"] <= 0.10


def test_goal_evaluator_does_not_trigger_anti_bot_on_benign_coding_challenge() -> None:
    evaluator = GoalEvaluator()
    eval_result = evaluator.evaluate(
        user_goal="Search for python jobs",
        current_url="https://duckduckgo.com/?q=python+internships",
        dom_summary={"results": [{"title": "Python Challenge"}]},
        extracted_text="Join our Python Coding Challenge today! Great internships available.",
        recent_actions=[{"action": "fill", "selector": "input", "value": "python internships"}],
        progress_report={"progress_score": 0.5, "signals": {}},
    )
    assert eval_result["completion_reason"] != "blocked_by_anti_bot"


@pytest.mark.asyncio
async def test_live_browser_anti_bot_detection_and_action_validator() -> None:
    """Verifies that ActionValidator and InterstitialDetector correctly block
    and classify a page presenting genuine Cloudflare anti-bot challenge markup.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.set_content(CLOUDFLARE_CHALLENGE_HTML)

            validator = ActionValidator(timeout_ms=5000)
            validation = await validator.validate(
                page=page,
                action={"action": "goto", "url": "https://example.com/protected"},
            )

            # Assert ActionValidator correctly blocked progression
            assert validation["success"] is False
            assert validation["reason"] == "goto_blocked_anti_bot"
            interstitial = validation["details"]["interstitial"]
            assert interstitial["detection_type"] == "anti_bot"
            assert interstitial["confidence"] >= 0.67
            assert any("verify you are human" in s for s in interstitial["matched_signals"])

            # Assert GoalEvaluator triggers blocked_by_anti_bot
            evaluator = GoalEvaluator()
            page_text = await page.inner_text("body")
            goal_eval = evaluator.evaluate(
                user_goal="Search for python jobs",
                current_url=page.url,
                dom_summary={},
                extracted_text=page_text,
                recent_actions=[{"action": "goto", "url": "https://example.com/protected"}],
                progress_report={"progress_score": 0.0, "signals": {}},
            )
            assert goal_eval["completion_reason"] == "blocked_by_anti_bot"
            assert goal_eval["should_terminate"] is True

        finally:
            await page.close()
            await browser.close()
