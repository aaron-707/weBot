from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Literal, TypedDict


ActionStatus = Literal["success", "failure"]


class ActionRecord(TypedDict, total=False):
    action: str
    selector: str
    status: ActionStatus
    timestamp: str
    error: str
    metadata: dict[str, Any]


@dataclass(slots=True)
class SessionMemory:
    """In-memory session tracker for workflow execution state."""

    max_actions: int = 200
    current_page: str = ""
    _actions: Deque[ActionRecord] = field(default_factory=deque)
    _failed_actions: Deque[ActionRecord] = field(default_factory=deque)
    _retry_counts: dict[str, int] = field(default_factory=dict)
    _successful_selectors: set[str] = field(default_factory=set)

    def add_action(
        self,
        *,
        action: str,
        status: ActionStatus,
        selector: str | None = None,
        page: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a normalized action record and update session stats."""
        if page:
            self.current_page = page

        record: ActionRecord = {
            "action": action,
            "selector": selector or "",
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {},
        }
        if error:
            record["error"] = error

        self._actions.append(record)
        self._trim_actions_if_needed()

        key = self._retry_key(action, selector)
        if status == "failure":
            self._failed_actions.append(record)
            self._retry_counts[key] = self._retry_counts.get(key, 0) + 1
        else:
            if selector:
                self._successful_selectors.add(selector)
            # Reset retry count on successful execution.
            if key in self._retry_counts:
                self._retry_counts[key] = 0

    def get_recent_actions(self, limit: int = 10) -> list[ActionRecord]:
        if limit <= 0:
            return []
        return list(self._actions)[-limit:]

    def get_failures(self, limit: int = 10) -> list[ActionRecord]:
        if limit <= 0:
            return []
        return list(self._failed_actions)[-limit:]

    @property
    def retry_counts(self) -> dict[str, int]:
        return dict(self._retry_counts)

    @property
    def successful_selectors(self) -> set[str]:
        return set(self._successful_selectors)

    def _trim_actions_if_needed(self) -> None:
        while len(self._actions) > self.max_actions:
            self._actions.popleft()

        # Keep failed action buffer bounded to same capacity.
        while len(self._failed_actions) > self.max_actions:
            self._failed_actions.popleft()

    @staticmethod
    def _retry_key(action: str, selector: str | None) -> str:
        return f"{action}:{selector or ''}"
