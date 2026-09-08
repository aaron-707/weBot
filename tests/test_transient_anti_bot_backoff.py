from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.workflows.recovery_engine import RecoveryEngine
from webot.intelligence.interstitial_detector import InterstitialDetector


def test_interstitial_detector_transient_vs_high_confidence():
    """Detector should set is_transient=True for soft/rate-limit signals (< 0.70) and False for confirmed >= 0.70."""
    detector = InterstitialDetector()

    # Rate-limit single signal -> confidence = 0.55 + 0.12*1 = 0.67 (< 0.70) -> is_transient = True
    rate_limit_res = detector.detect(
        url="https://api.example.com/data",
        title="429 Too Many Requests",
        text="You have exceeded your rate limit. Please wait.",
    )
    assert rate_limit_res["detection_type"] == "anti_bot"
    assert rate_limit_res["confidence"] < 0.70
    assert rate_limit_res["is_transient"] is True

    # Strong Cloudflare challenge with multiple tokens -> confidence >= 0.70 -> is_transient = False
    cf_res = detector.detect(
        url="https://example.com/cdn-cgi/challenge-platform",
        title="Just a moment...",
        text="Please verify you are human to continue. cf-challenge ray id 12345 captcha.",
    )
    assert cf_res["detection_type"] == "anti_bot"
    assert cf_res["confidence"] >= 0.70
    assert cf_res["is_transient"] is False


@pytest.mark.asyncio
async def test_recovery_engine_confirmed_anti_bot_terminates_immediately():
    """Confirmed anti-bot (is_transient=False or confidence >= 0.70) must terminate immediately without retry."""
    engine = RecoveryEngine()
    mock_page = MagicMock()
    action = {"action": "click", "selector": "#submit"}

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        decision = await engine.recover(
            page=mock_page,
            error_type="anti_bot_blocked",
            failed_action=action,
            retry_count=0,
            interstitial={"detected": True, "confidence": 0.85, "is_transient": False},
        )

        assert decision["recovered"] is False
        assert decision["strategy"] == "stop_no_retry"
        mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_engine_transient_anti_bot_single_backoff():
    """Transient anti-bot (is_transient=True, retry_count=0) should backoff once and retry."""
    engine = RecoveryEngine()
    mock_page = MagicMock()
    action = {"action": "click", "selector": "#submit"}

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        decision = await engine.recover(
            page=mock_page,
            error_type="anti_bot_blocked",
            failed_action=action,
            retry_count=0,
            interstitial={"detected": True, "confidence": 0.67, "is_transient": True},
        )

        assert decision["recovered"] is True
        assert decision["strategy"] == "transient_backoff_retry"
        assert decision["next_action"] == action
        mock_sleep.assert_awaited_once_with(1.5)


@pytest.mark.asyncio
async def test_recovery_engine_transient_anti_bot_stops_on_second_attempt():
    """If transient anti-bot persists on retry (retry_count > 0), do not retry again."""
    engine = RecoveryEngine()
    mock_page = MagicMock()
    action = {"action": "click", "selector": "#submit"}

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        decision = await engine.recover(
            page=mock_page,
            error_type="anti_bot_blocked",
            failed_action=action,
            retry_count=1,
            interstitial={"detected": True, "confidence": 0.67, "is_transient": True},
        )

        assert decision["recovered"] is False
        assert decision["strategy"] == "stop_no_retry"
        mock_sleep.assert_not_called()
