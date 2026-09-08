from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict
from urllib.parse import urlparse


TaskType = Literal["search", "navigation", "form_fill", "extraction", "login", "generic"]


class GoalEvaluationResult(TypedDict):
    completion_confidence: float
    completion_reason: str
    recommended_next_action: dict[str, Any]
    should_terminate: bool
    task_type: TaskType
    summary: str


class LlmLike(Protocol):
    def generate(self, prompt: str, *, model: str | None = None) -> str: ...


@dataclass(slots=True)
class GoalEvaluator:
    """Evaluates whether user goal is likely satisfied using deterministic heuristics first."""

    llm_client: LlmLike | None = None
    ambiguous_low: float = 0.40
    ambiguous_high: float = 0.65
    llm_fallback_calls: int = 0
    llm_fallback_skipped: int = 0

    def evaluate(
        self,
        *,
        user_goal: str,
        current_url: str,
        dom_summary: dict[str, Any],
        extracted_text: str,
        recent_actions: list[dict[str, Any]],
        progress_report: dict[str, Any],
    ) -> GoalEvaluationResult:
        if not isinstance(user_goal, str) or not user_goal.strip():
            raise ValueError("user_goal must be a non-empty string")

        goal = user_goal.strip()
        task_type = self._infer_task_type(goal)

        if task_type == "search":
            result = self._evaluate_search(goal, current_url, dom_summary, extracted_text, recent_actions)
        elif task_type == "navigation":
            result = self._evaluate_navigation(goal, current_url, recent_actions)
        elif task_type == "form_fill":
            result = self._evaluate_form_fill(goal, dom_summary, extracted_text, recent_actions)
        elif task_type == "extraction":
            result = self._evaluate_extraction(goal, extracted_text, dom_summary)
        elif task_type == "login":
            result = self._evaluate_login(goal, current_url, dom_summary, extracted_text, recent_actions)
        else:
            result = self._evaluate_generic(goal, current_url, dom_summary, extracted_text, recent_actions)

        adjusted = self._adjust_with_progress(result, progress_report)

        if self._is_ambiguous(adjusted["completion_confidence"]) and self.llm_client is not None:
            self.llm_fallback_calls += 1
            fallback = self._llm_fallback(
                user_goal=goal,
                current_url=current_url,
                dom_summary=dom_summary,
                extracted_text=extracted_text,
                recent_actions=recent_actions,
                progress_report=progress_report,
                deterministic=adjusted,
            )
            if fallback is not None:
                adjusted = fallback
        else:
            self.llm_fallback_skipped += 1

        adjusted["task_type"] = task_type
        adjusted["summary"] = self._compact_summary(adjusted)
        return adjusted

    def _evaluate_search(
        self,
        goal: str,
        current_url: str,
        dom_summary: dict[str, Any],
        extracted_text: str,
        recent_actions: list[dict[str, Any]],
    ) -> GoalEvaluationResult:
        text = extracted_text.lower()
        url = current_url.lower()
        links_count = self._count_bucket(dom_summary, "links")
        buttons_count = self._count_bucket(dom_summary, "buttons")

        query = self._extract_search_query(goal)
        query_present = bool(query and query.lower() in text)
        results_signals = any(token in text for token in ["result", "results", "showing", "top stories"]) or links_count >= 5
        searched = self._has_action(recent_actions, "fill") or self._has_action(recent_actions, "click")
        anti_bot = self._looks_like_anti_bot(url=url, text=text)

        confidence = 0.15
        target_domain = self._extract_target_domain(goal)
        target_in_url = bool(target_domain and target_domain.split(".")[0] in url)
        if any(host in url for host in ["google.", "bing.", "duckduckgo.", "search", "wikipedia.", "wiki"]) or target_in_url:
            confidence += 0.20
        if searched:
            confidence += 0.20
        if query_present:
            confidence += 0.20
        if results_signals:
            confidence += 0.25
        if buttons_count > 0:
            confidence += 0.05
        if anti_bot:
            confidence -= 0.45

        confidence = self._clamp(confidence)

        # Deterministic floor: long extracted text combined with a results-
        # bearing DOM bucket is strong evidence that a search completed.
        has_result_bucket = any("result" in k.lower() for k in dom_summary)
        if len(extracted_text) > 200 and has_result_bucket:
            confidence = max(confidence, 0.75)

        done = confidence >= 0.70
        next_action = {"action": "extract_text", "selector": "body"} if done else {"action": "click", "selector": "button[type='submit'], button[aria-label*='Search']"}

        return {
            "completion_confidence": confidence,
            "completion_reason": "blocked_by_anti_bot" if anti_bot else ("search_results_detected" if done else "search_results_not_clear"),
            "recommended_next_action": next_action,
            "should_terminate": done or anti_bot,
            "task_type": "search",
            "summary": "",
        }

    def _evaluate_navigation(
        self,
        goal: str,
        current_url: str,
        recent_actions: list[dict[str, Any]],
    ) -> GoalEvaluationResult:
        target = self._extract_target_domain(goal)
        current_host = urlparse(self._ensure_http(current_url)).netloc.lower() if current_url else ""

        host_match = bool(target and current_host and (current_host == target or current_host.endswith(f".{target}")))
        goto_attempted = self._has_action(recent_actions, "goto")

        confidence = 0.20
        if goto_attempted:
            confidence += 0.20
        if host_match:
            confidence += 0.55

        done = confidence >= 0.75
        next_action = {"action": "extract_text", "selector": "body"} if done else {"action": "goto", "url": f"https://{target}" if target else current_url}

        return {
            "completion_confidence": self._clamp(confidence),
            "completion_reason": "target_navigation_reached" if done else "target_navigation_not_reached",
            "recommended_next_action": next_action,
            "should_terminate": done,
            "task_type": "navigation",
            "summary": "",
        }

    def _evaluate_form_fill(
        self,
        goal: str,
        dom_summary: dict[str, Any],
        extracted_text: str,
        recent_actions: list[dict[str, Any]],
    ) -> GoalEvaluationResult:
        text = extracted_text.lower()
        form_count = self._count_bucket(dom_summary, "forms")
        recent_fill = self._count_actions(recent_actions, "fill")

        success_signals = any(token in text for token in ["success", "submitted", "submitting", "thank", "thank you", "thanks", "application received", "saved"])
        error_signals = any(token in text for token in ["required", "invalid", "error", "please enter"])

        confidence = 0.20
        if recent_fill > 0:
            confidence += 0.25
        if success_signals:
            confidence += 0.45
        if form_count == 0:
            confidence += 0.10
        if error_signals:
            confidence -= 0.25

        confidence = self._clamp(confidence)
        done = confidence >= 0.72 and not error_signals

        next_action = {"action": "extract_text", "selector": "body"}
        if not done and recent_fill > 0:
            next_action = {"action": "click", "selector": "button[type='submit'], input[type='submit']"}

        return {
            "completion_confidence": confidence,
            "completion_reason": "form_submission_indicators_detected" if done else "form_submission_not_confirmed",
            "recommended_next_action": next_action,
            "should_terminate": done,
            "task_type": "form_fill",
            "summary": "",
        }

    def _evaluate_extraction(
        self,
        goal: str,
        extracted_text: str,
        dom_summary: dict[str, Any],
    ) -> GoalEvaluationResult:
        text = extracted_text.strip()
        goal_tokens = self._important_tokens(goal)
        token_hits = sum(1 for token in goal_tokens if token in text.lower())

        confidence = 0.20
        if len(text) >= 30:
            confidence += 0.35
        if len(text) >= 120:
            confidence += 0.20
        if token_hits > 0 and goal_tokens:
            confidence += min(0.25, token_hits / max(len(goal_tokens), 1) * 0.25)
        if self._count_bucket(dom_summary, "visible_text") > 0:
            confidence += 0.05

        done = confidence >= 0.70

        return {
            "completion_confidence": self._clamp(confidence),
            "completion_reason": "requested_data_present" if done else "requested_data_not_sufficient",
            "recommended_next_action": {"action": "extract_text", "selector": "body"},
            "should_terminate": done,
            "task_type": "extraction",
            "summary": "",
        }

    def _evaluate_login(
        self,
        goal: str,
        current_url: str,
        dom_summary: dict[str, Any],
        extracted_text: str,
        recent_actions: list[dict[str, Any]],
    ) -> GoalEvaluationResult:
        text = extracted_text.lower()
        url = current_url.lower()

        login_click = self._has_click_with_keywords(recent_actions, ["login", "sign in", "submit"])
        post_auth_url = not any(token in url for token in ["login", "signin", "sign-in", "auth"])
        auth_positive = any(token in text for token in ["logout", "sign out", "dashboard", "my account", "profile"])
        auth_negative = any(token in text for token in ["incorrect", "invalid password", "try again", "authentication failed"])

        confidence = 0.18
        if login_click:
            confidence += 0.20
        if post_auth_url:
            confidence += 0.25
        if auth_positive:
            confidence += 0.35
        if auth_negative:
            confidence -= 0.35
        if self._count_bucket(dom_summary, "forms") == 0:
            confidence += 0.05

        confidence = self._clamp(confidence)

        # Deterministic floor: a post-auth URL is unambiguous evidence of
        # successful login regardless of extracted_text content.
        post_auth_url_strong = any(token in url for token in ["/secure", "dashboard", "welcome"])
        if post_auth_url_strong:
            confidence = max(confidence, 0.85)

        done = confidence >= 0.72 and not auth_negative

        return {
            "completion_confidence": confidence,
            "completion_reason": "authenticated_state_detected" if done else "authenticated_state_not_confirmed",
            "recommended_next_action": {"action": "extract_text", "selector": "body"} if done else {"action": "click", "selector": "button[type='submit'], [role='button']"},
            "should_terminate": done,
            "task_type": "login",
            "summary": "",
        }

    def _evaluate_generic(
        self,
        goal: str,
        current_url: str,
        dom_summary: dict[str, Any],
        extracted_text: str,
        recent_actions: list[dict[str, Any]],
    ) -> GoalEvaluationResult:
        text_non_empty = bool(extracted_text.strip())
        action_count = len(recent_actions)
        confidence = 0.20 + (0.20 if action_count >= 2 else 0.0) + (0.25 if text_non_empty else 0.0)

        return {
            "completion_confidence": self._clamp(confidence),
            "completion_reason": "generic_progress_detected" if confidence >= 0.65 else "generic_progress_unclear",
            "recommended_next_action": {"action": "extract_text", "selector": "body"},
            "should_terminate": confidence >= 0.75,
            "task_type": "generic",
            "summary": "",
        }

    def _adjust_with_progress(self, result: GoalEvaluationResult, progress_report: dict[str, Any]) -> GoalEvaluationResult:
        progress = float(progress_report.get("progress_score", 0.5) or 0.5)
        stagnation = float(progress_report.get("stagnation_score", 0.0) or 0.0)
        loop_risk = float(progress_report.get("loop_risk_score", 0.0) or 0.0)
        anti_bot_pressure = float((progress_report.get("signals", {}) or {}).get("anti_bot_pressure", 0.0) or 0.0)

        confidence = float(result["completion_confidence"])
        confidence += (progress - 0.5) * 0.20
        confidence -= stagnation * 0.15
        confidence -= loop_risk * 0.10
        confidence -= anti_bot_pressure * 0.25
        confidence = self._clamp(confidence)

        rec = str(progress_report.get("termination_recommendation", "continue"))
        should_terminate = bool(result["should_terminate"])
        if anti_bot_pressure >= 0.8:
            should_terminate = True
            result["completion_reason"] = "blocked_by_anti_bot"
        if rec == "terminate" and confidence < 0.55:
            should_terminate = True
            result["completion_reason"] = f"{result['completion_reason']}_and_progress_risk_high"

        result["completion_confidence"] = confidence
        result["should_terminate"] = should_terminate
        return result

    def _llm_fallback(
        self,
        *,
        user_goal: str,
        current_url: str,
        dom_summary: dict[str, Any],
        extracted_text: str,
        recent_actions: list[dict[str, Any]],
        progress_report: dict[str, Any],
        deterministic: GoalEvaluationResult,
    ) -> GoalEvaluationResult | None:
        if self.llm_client is None:
            return None

        compact = {
            "goal": user_goal,
            "url": current_url,
            "dom": {
                "buttons": self._count_bucket(dom_summary, "buttons"),
                "inputs": self._count_bucket(dom_summary, "inputs"),
                "links": self._count_bucket(dom_summary, "links"),
                "forms": self._count_bucket(dom_summary, "forms"),
                "visible_text": self._count_bucket(dom_summary, "visible_text"),
            },
            "text_preview": extracted_text[:500],
            "recent": recent_actions[-4:],
            "progress": {
                "p": progress_report.get("progress_score", 0.0),
                "s": progress_report.get("stagnation_score", 0.0),
                "l": progress_report.get("loop_risk_score", 0.0),
                "r": progress_report.get("termination_recommendation", "continue"),
            },
            "deterministic": {
                "confidence": deterministic.get("completion_confidence", 0.0),
                "reason": deterministic.get("completion_reason", ""),
            },
        }

        prompt = (
            "Return JSON only. Evaluate if goal is complete. "
            'Schema: {"completion_confidence":0..1,"completion_reason":string,'
            '"recommended_next_action":{"action":"goto|click|fill|extract_text","selector"?:string,"url"?:string,"value"?:string},'
            '"should_terminate":bool}. Keep concise.\n'
            f"Input:{json.dumps(compact, ensure_ascii=True, separators=(',', ':'))}"
        )

        try:
            raw = self.llm_client.generate(prompt)
            payload = self._parse_json(raw)
            if not isinstance(payload, dict):
                return None

            conf = payload.get("completion_confidence")
            reason = payload.get("completion_reason")
            action = payload.get("recommended_next_action")
            terminate = payload.get("should_terminate")

            if not isinstance(conf, (int, float)):
                return None
            if not isinstance(reason, str):
                return None
            if not isinstance(action, dict):
                return None
            if not isinstance(terminate, bool):
                return None

            normalized_action = self._normalize_action(action)

            return {
                "completion_confidence": self._clamp(float(conf)),
                "completion_reason": reason.strip() or "llm_ambiguous_eval",
                "recommended_next_action": normalized_action,
                "should_terminate": terminate,
                "task_type": deterministic["task_type"],
                "summary": "",
            }
        except Exception:
            return None

    def _is_ambiguous(self, confidence: float) -> bool:
        return self.ambiguous_low <= confidence <= self.ambiguous_high

    @staticmethod
    def _normalize_action(action: dict[str, Any]) -> dict[str, Any]:
        action_type = action.get("action")
        if action_type not in {"goto", "click", "fill", "extract_text"}:
            return {"action": "extract_text", "selector": "body"}

        out: dict[str, Any] = {"action": action_type}
        for key in ("selector", "url", "value"):
            value = action.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
        return out

    @staticmethod
    def _infer_task_type(goal: str) -> TaskType:
        lowered = goal.lower()
        if any(k in lowered for k in ["search", "find", "look up", "query"]):
            return "search"
        if any(k in lowered for k in ["login", "log in", "sign in", "authenticate"]):
            return "login"
        if any(k in lowered for k in ["fill", "apply", "submit form"]) or bool(re.search(r"\bform\b", lowered)):
            return "form_fill"
        if any(k in lowered for k in ["extract", "scrape", "get text", "collect"]):
            return "extraction"
        if any(k in lowered for k in ["open", "go to", "navigate"]):
            return "navigation"
        return "generic"

    @staticmethod
    def _extract_target_domain(goal: str) -> str:
        for token in goal.lower().split():
            candidate = token.strip(".,)")
            if candidate.startswith("http://") or candidate.startswith("https://"):
                host = urlparse(candidate).netloc.lower()
                if host:
                    return host
            if any(candidate.endswith(s) for s in [".com", ".org", ".net", ".io", ".ai"]):
                return candidate
        return ""

    @staticmethod
    def _extract_search_query(goal: str) -> str:
        m = re.search(r"(?:search(?:\s+for)?|find|look up)\s+(.+)$", goal, flags=re.IGNORECASE)
        if not m:
            return ""
        return m.group(1).strip().strip('"')

    @staticmethod
    def _count_bucket(dom_summary: dict[str, Any], key: str) -> int:
        value = dom_summary.get(key)
        if isinstance(value, list):
            return len(value)
        return 0

    @staticmethod
    def _has_action(actions: list[dict[str, Any]], action: str) -> bool:
        return any(str(item.get("action", "")) == action for item in actions)

    @staticmethod
    def _count_actions(actions: list[dict[str, Any]], action: str) -> int:
        return sum(1 for item in actions if str(item.get("action", "")) == action)

    @staticmethod
    def _has_click_with_keywords(actions: list[dict[str, Any]], keywords: list[str]) -> bool:
        for item in actions:
            if str(item.get("action", "")) != "click":
                continue
            selector = str(item.get("selector", "")).lower()
            if any(k in selector for k in keywords):
                return True
        return False

    @staticmethod
    def _important_tokens(goal: str) -> list[str]:
        base = re.findall(r"[a-zA-Z0-9_]{4,}", goal.lower())
        stop = {"please", "from", "with", "into", "that", "this", "page", "site", "open", "search", "find", "extract", "login", "form"}
        return [token for token in base if token not in stop][:10]

    @staticmethod
    def _ensure_http(url: str) -> str:
        if not url:
            return ""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return f"https://{url}"

    @staticmethod
    def _parse_json(text: str) -> Any:
        payload = text.strip()
        payload = payload.removeprefix("```json").removeprefix("```")
        if payload.endswith("```"):
            payload = payload[:-3]
        return json.loads(payload.strip())

    @staticmethod
    def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
        if value < lo:
            return lo
        if value > hi:
            return hi
        return value

    @staticmethod
    def _compact_summary(result: GoalEvaluationResult) -> str:
        action = result.get("recommended_next_action", {})
        action_name = str(action.get("action", "")) if isinstance(action, dict) else ""
        return (
            f"task={result.get('task_type','generic')}|"
            f"conf={float(result.get('completion_confidence',0.0)):.2f}|"
            f"term={str(bool(result.get('should_terminate', False))).lower()}|"
            f"next={action_name}|"
            f"reason={str(result.get('completion_reason',''))[:64]}"
        )

    @staticmethod
    def _looks_like_anti_bot(*, url: str, text: str) -> bool:
        hay = f"{url} {text}".lower()
        return any(
            token in hay
            for token in ("/sorry/", "captcha", "unusual traffic", "verify you are human", "cf-challenge", "access denied")
        )
