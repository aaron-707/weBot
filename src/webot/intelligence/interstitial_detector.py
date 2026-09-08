from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypedDict
from urllib.parse import urlparse


DetectionType = Literal["anti_bot", "captcha", "login_wall", "cookie_wall", "access_denied", "none"]

HIGH_CONFIDENCE_THRESHOLD: float = 0.70


class InterstitialDetection(TypedDict, total=False):
    detection_type: DetectionType
    confidence: float
    matched_signals: list[str]
    recommended_action: str
    is_transient: bool


@dataclass(slots=True)
class InterstitialDetector:
    """Deterministic detector for interstitial and blocking pages."""

    def detect(self, *, url: str, title: str, text: str) -> InterstitialDetection:
        hay = f"{title} {text}".lower()
        parsed_path = urlparse(url).path.lower() if url else ""

        anti_bot_signals = self._match_signals(
            hay,
            ["captcha", "unusual traffic", "verify you are human", "robot check", "cf-challenge"],
        )
        if "/sorry/" in parsed_path:
            anti_bot_signals.append("url:/sorry/")

        rate_limit_signals = self._match_signals(
            hay,
            ["too many requests", "rate limit", "temporarily unavailable", "please slow down"],
        )

        access_denied_signals = self._match_signals(
            hay,
            ["access denied", "forbidden", "you don't have permission", "request blocked"],
        )

        login_wall_signals = self._match_signals(
            hay,
            ["sign in to continue", "log in to continue", "login required", "please sign in"],
        )

        cookie_wall_signals = self._match_signals(
            hay,
            ["accept all cookies", "cookie settings", "we use cookies", "consent"],
        )

        if anti_bot_signals:
            return self._build("anti_bot", anti_bot_signals, "pivot_away_from_direct_navigation")
        if rate_limit_signals:
            return {
                "detection_type": "anti_bot",
                "confidence": 0.60,
                "matched_signals": rate_limit_signals,
                "recommended_action": "transient_backoff_retry",
                "is_transient": True,
            }
        if access_denied_signals:
            return self._build("access_denied", access_denied_signals, "terminate_or_change_target")
        if login_wall_signals:
            return self._build("login_wall", login_wall_signals, "perform_login_flow")
        if cookie_wall_signals:
            return self._build("cookie_wall", cookie_wall_signals, "accept_cookie_consent")

        return {
            "detection_type": "none",
            "confidence": 0.0,
            "matched_signals": [],
            "recommended_action": "continue",
            "is_transient": False,
        }

    @staticmethod
    def _match_signals(haystack: str, patterns: list[str]) -> list[str]:
        found: list[str] = []
        for pattern in patterns:
            if re.search(re.escape(pattern), haystack, flags=re.IGNORECASE):
                found.append(pattern)
        return found

    @staticmethod
    def _build(det_type: DetectionType, signals: list[str], recommendation: str) -> InterstitialDetection:
        confidence = min(1.0, 0.55 + 0.12 * len(signals))
        is_transient = (det_type in {"anti_bot", "captcha", "access_denied"}) and (confidence < HIGH_CONFIDENCE_THRESHOLD)
        return {
            "detection_type": det_type,
            "confidence": confidence,
            "matched_signals": signals,
            "recommended_action": recommendation,
            "is_transient": is_transient,
        }
