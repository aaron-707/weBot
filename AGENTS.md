# AGENTS.md

## Purpose
This repository builds a modular, local-first AI browser automation system in Python. Prioritize reliability, maintainability, and deterministic browser control.

## Architecture Rules
- Keep modules small, typed, and single-purpose.
- Prefer composition over deep inheritance.
- Keep orchestration in `workflows/`; keep low-level operations in domain modules.
- Avoid hardcoded page-specific logic when reusable patterns are possible.

## Deterministic-First Philosophy
- Prefer deterministic browser automation before LLM decisions.
- Use LLM only when deterministic matching/selection is insufficient.
- Do not use screenshot/vision-based analysis unless explicitly introduced later.

## Module Boundaries
- `browser/`: Playwright/browser lifecycle and primitive actions.
- `intelligence/`: DOM extraction and structured page understanding.
- `llm/`: Ollama client and decision helpers.
- `workflows/`: task interpretation, action execution, form filling, recovery.
- `memory/`: runtime session state and action history.
- `utils/`: shared utilities (logger, helpers).
- `config/`: typed settings and environment-driven config.
- `tests/`: unit/integration tests mirroring module structure.

## Coding Standards
- Python 3.11+, typed code required.
- Favor clear, minimal implementations over clever abstractions.
- Keep public interfaces explicit and stable.
- Add comments only where behavior is non-obvious.
- Do not add unnecessary dependencies.

## Logging Standards
- Use shared logger utilities only (`utils/logger.py`).
- Emit structured logs with timestamps.
- Log key lifecycle events, retries, and failures.
- Never log secrets or sensitive user data.

## Error Handling Expectations
- Validate inputs early and fail with clear error messages.
- Catch and handle known operational exceptions (timeouts, missing selectors, navigation failures).
- Return structured success/failure results from workflow modules.
- Use retries with bounded limits; avoid infinite loops.

## Testing Requirements
- Add tests for every new module and critical branch.
- Cover happy path, validation failures, and retry/error behavior.
- Prefer deterministic tests with mocks/stubs for network/LLM/browser where appropriate.
- Keep tests isolated and fast by default.

## Change Discipline
- Do not rewrite unrelated modules.
- Make focused, minimal diffs for the requested scope.
- Preserve existing behavior unless change is explicitly requested.
