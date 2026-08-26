# Search Stabilization Report

Date: 2026-05-25

## Scope
Stabilization and correctness only (no new search features).

## Regressions Found
1. Submit semantics were conflated with `fill`.
- Enter-submit was encoded as a fill-with-newline path and validated by fill validation.
- This caused incorrect validation semantics and timeout-prone selector reads on post-submit pages.

2. Search workflow could complete before full state-machine ownership finalized.
- `extract_text` completion could occur before explicit search validation/metric finalization in some paths.

3. Constructor/interface regression.
- `SearchStateMachine` signature mismatch with `AgentLoop` (`progress_tracker`/`goal_evaluator` kwargs) caused runtime failures.

## Fixes Applied
1. Added explicit `submit` action semantics.
- `ActionExecutor` now supports `action="submit"` with `submit_method` (`enter` or `click`).
- `ActionValidator` now has a dedicated submit validation path (`submit_validated` / `submit_no_observable_change`).
- Result: `fill`, `submit`, `click`, `goto`, and `extract_text` are now distinct validation paths.

2. Stabilized SearchStateMachine flow ownership.
- Search transitions now explicitly run: open -> detect -> fill -> submit -> detect results -> extract -> validate.
- Provider metrics are recorded before completion/failure transitions.
- Added `should_allow_extract_termination()` and `is_failed()` hooks for AgentLoop termination control.

3. Fixed compatibility and integration defects.
- Restored SearchStateMachine constructor compatibility with `AgentLoop`.
- Fixed AgentLoop selection flow regression caused by dead/unreachable LLM fallback block after state-machine return path.

## Files Changed
- `src/webot/workflows/search_state_machine.py`
- `src/webot/workflows/action_executor.py`
- `src/webot/workflows/action_validator.py`
- `src/webot/workflows/agent_loop.py`

## Before/After Metrics (Search)
Benchmark source:
- Before: `artifacts/runtime/benchmark/reliability_benchmark_report_before_provider_rotation.json`
- After: `artifacts/runtime/benchmark/reliability_benchmark_report.json`

Single-run source:
- Before: `artifacts/runtime/duckduckgo_search/reports/duckduckgo_search_report_before_provider_rotation.json`
- After: `artifacts/runtime/duckduckgo_search/reports/duckduckgo_search_report.json`

### Required Comparison
- Success rate:
- Before: `0.00%`
- After: `0.00%`

- Anti-bot rate:
- Before: `0.00`
- After: `0.00`

- Average steps:
- Before: `3.00`
- After: `5.00`

- Average retries:
- Before: `0.00`
- After: `0.00`

- Average completion confidence:
- Before: `0.88552`
- After: `0.07310`

- Average duration:
- Before: `7.382s`
- After: `6.875s`

### Single-run sanity check (`test_duckduckgo_search`)
- Validation:
- Before: pass (`validation_passed=true`)
- After: pass (`validation_passed=true`)

- Steps:
- Before: `3`
- After: `5`

- Duration:
- Before: `9.548s`
- After: `10.419s`

- Provider metrics recorded (after):
- `[{"provider":"duckduckgo","success":true,"completion_confidence":0.95,"anti_bot_detections":0,"retries":0,"duration_seconds":2.712,"reason":"validated"}]`

## Remaining Bottlenecks
1. Benchmark-level search confidence dropped substantially despite single-run pass.
- The stricter state-machine ownership increased steps and altered confidence behavior in benchmark runs.

2. Runtime benchmark success-rate interpretation is still constrained by current infra-failure accounting (all runs marked under degraded infra context), masking workflow-level success for this scenario.

3. Search extraction/validation still uses generic `extract_text` output quality, which can understate confidence in repeated benchmark runs.
