# weBot

`weBot` is a local-first autonomous browser automation system in Python focused on reliability, deterministic control, and measurable workflow outcomes.

The project is designed to execute realistic user journeys (search, form fill, login) while producing rich artifacts (traces, screenshots, metrics, reports) that support debugging, reliability benchmarking, and portfolio-ready engineering demonstrations.

## 1. Vision and Design Philosophy
The core philosophy is deterministic-first automation:
- Prefer deterministic browser operations whenever they are feasible.
- Use LLM decisions only as a fallback when deterministic selection is insufficient.
- Keep orchestration explicit and typed so failures are traceable and debuggable.
- Optimize for autonomous completion reliability, not architecture size.

This repository intentionally prioritizes:
- Focused modular boundaries.
- Structured logging and runtime evidence.
- Small, localized reliability improvements over sweeping rewrites.

## 2. Current Capabilities
As of today, the system can:
- Run autonomous browser workflows through a central `AgentLoop`.
- Execute and validate actions with retries and bounded recovery.
- Detect interstitial/anti-bot conditions and apply safeguards.
- Track progress, stagnation risk, and completion confidence.
- Record full execution traces and export timeline summaries.
- Run runtime test suites for search/form/login flows.
- Run multi-iteration reliability benchmarks.
- Continue in deterministic degraded mode when Ollama/LLM is unavailable.

### 2.1 Supported User Goals (Current)
- Search workflows:
- Example: `Open DuckDuckGo and search for Python internships`
- Form workflows:
- Example: `Fill DemoQA practice form fields and submit`
- Login workflows:
- Example: `Login to the-internet.herokuapp.com with demo credentials`
- Generic navigation/extraction:
- Example: `Open <url> and extract visible text from body`

## 3. High-Level Architecture
### 3.1 Layering
- `browser` layer: lifecycle and primitive operations (`goto`, `click`, `fill`, `extract_text`).
- `intelligence` layer: DOM extraction, delta summarization, selector heuristics, interstitial detection.
- `llm` layer: Ollama connectivity and decision generation.
- `workflows` layer: orchestration, validation, recovery, state machines, progress/goal analysis.
- `memory` layer: session state, retry counters, recent action/failure history.
- `observability` layer: structured trace timeline and export.
- `tests/runtime` layer: executable reliability harnesses and benchmark reporting.

### 3.2 Control Flow (Agent Loop)
Typical loop per step:
1. Extract current DOM snapshot.
2. Select next action via:
- domain state machine, then
- deterministic strategy, then
- decision engine (LLM or deterministic fallback).
3. Execute action.
4. Validate action outcome.
5. Attempt bounded recovery on failure.
6. Update session memory and DOM delta.
7. Update step history for trace/reporting.
8. Evaluate loop guards and termination conditions.

### 3.3 Simple Architecture Diagram (ASCII)
```text
User Goal
   |
   v
AgentLoop
   |---> State Machines (search/form/login)
   |---> Deterministic Strategy
   |---> DecisionEngine (LLM fallback path)
   |
   v
ActionExecutor ----> Browser Controller (Playwright)
   |
   v
ActionValidator ----> Interstitial Detector
   |
   +----> RecoveryEngine (bounded retries/recovery)
   |
   +----> SessionMemory / ProgressTracker / GoalEvaluator
   |
   +----> ExecutionTrace + Runtime Reports
```

## 4. Repository Structure
- `src/webot/config/`: typed settings and environment resolution.
- `src/webot/intelligence/`: DOM extractor, DOM delta, interstitial detector.
- `src/webot/llm/`: `OllamaClient`, `DecisionEngine`.
- `src/webot/memory/`: session memory and action history tracking.
- `src/webot/observability/`: execution trace capture/export/rendering.
- `src/webot/workflows/`:
- `agent_loop.py`
- `action_executor.py`
- `action_validator.py`
- `recovery_engine.py`
- `progress_tracker.py`
- `goal_evaluator.py`
- `form_filler.py`
- `search_state_machine.py`
- `form_state_machine.py`
- `login_state_machine.py`
- `tests/runtime/`:
- workflow runtime tests
- `test_runner.py`
- `result_validator.py`
- `reliability_benchmark.py`
- `docs/`: audits and reliability analysis write-ups.
- `artifacts/runtime/`: generated traces/reports/screenshots.

## 5. Core Implemented Modules
### 5.1 `OllamaClient`
- Adds health probing and cached availability state.
- Supports quick unavailability detection.
- Prevents retry storms with cooldown behavior.
- Exposes `is_available()` for orchestration decisions.

### 5.2 `DecisionEngine`
- Produces structured next-action objects.
- Gracefully falls back to deterministic action selection when LLM is unavailable or malformed.
- Avoids propagating LLM outage exceptions into `AgentLoop`.

### 5.3 `ActionExecutor`
- Executes typed actions with bounded retry.
- Supports distinct action semantics:
- `goto`
- `fill`
- `submit`
- `click`
- `extract_text`

### 5.4 `ActionValidator`
- Validates each action type with dedicated logic.
- Includes separate validation path for `submit` to avoid conflating submit semantics with fill semantics.
- Integrates interstitial detection into navigation validation.

### 5.5 `RecoveryEngine`
- Classifies failures and applies bounded recovery strategies.
- Avoids infinite retries.
- Includes anti-bot and stagnation-aware stop behavior.

### 5.6 `AgentLoop`
- Central autonomous orchestrator.
- Integrates state-machine strategy layer.
- Tracks history, failure streaks, anti-bot pressure, and loop guard triggers.
- Supports deterministic degraded mode (`llm_enabled`, `degraded_mode`).

### 5.7 `ProgressTracker`
- Computes:
- `progress_score`
- `stagnation_score`
- `loop_risk_score`
- `termination_recommendation`
- Uses repeat-action, retry, DOM-change, anti-bot, and fill-loop signals.

### 5.8 `GoalEvaluator`
- Estimates completion confidence per task category.
- Uses deterministic heuristics and progress adjustments.
- Can consult LLM fallback for ambiguous cases.

### 5.9 `ExecutionTrace`
- Captures step-by-step runtime state:
- timestamps
- URL/action/selector
- validation results
- recovery details
- interstitial signals
- goal evaluation
- loop guard/termination reasons
- Supports:
- JSON export
- compact terminal summary
- human-readable step timeline

## 6. Domain State Machines
### 6.1 Search State Machine
Implements explicit transitions:
- open provider
- detect search input
- fill query
- submit (enter first, button fallback)
- detect results page
- extract
- validate

Supports provider-aware behavior and transition logging:
- `state_entered`
- `state_completed`
- `state_failed`

### 6.2 Form State Machine
Transitions:
- detect required fields
- fill fields
- submit
- detect confirmation
- validate submission outcome

### 6.3 Login State Machine
Transitions:
- detect credential fields
- fill username/password
- submit
- verify authenticated page
- validate success indicators

## 7. Runtime Tests and Benchmarking
### 7.1 Workflow Tests
- `python -m tests.runtime.test_duckduckgo_search`
- `python -m tests.runtime.test_demoqa_form`
- `python -m tests.runtime.test_login_flow`

### 7.2 Benchmark
- `python -m tests.runtime.reliability_benchmark <iterations>`

### 7.3 Outputs Per Run
- structured JSON report
- trace JSON
- screenshots
- terminal summary

### 7.4 Metrics Collected
- success/failure
- termination reason
- completion confidence
- total steps
- retries
- recovery attempts
- stagnation events
- anti-bot detections
- execution duration

### 7.5 Current Reliability Snapshot (Latest Measured)

#### A. LLM-Assisted Decision Path (Ollama Live: `qwen2.5:3b`)
Source: `artifacts/runtime/benchmark/reliability_benchmark_report.json` (iterations=5, 20 total runs, generated `2026-09-08T11:19:07Z`).
Health status: `llm_available_rate: 1.0`, `degraded_mode_rate: 0.0`, `overall_success_rate: 1.00`.

Search (DuckDuckGo, provider: `duckduckgo`):
- success_rate: `1.00`
- average_steps: `5.0`
- average_retries: `0.0`
- anti_bot_detection_rate: `0.0`
- average_completion_confidence: `0.8183`
- average_runtime_seconds: `15.124`

Form fill (DemoQA, provider: `demoqa`):
- success_rate: `1.00`
- average_steps: `6.0`
- average_retries: `0.0`
- anti_bot_detection_rate: `0.0`
- average_completion_confidence: `0.8946`
- average_runtime_seconds: `5.862`

Login (The Internet, provider: `the_internet`):
- success_rate: `1.00`
- average_steps: `4.0`
- average_retries: `0.0`
- anti_bot_detection_rate: `0.0`
- average_completion_confidence: `0.8104`
- average_runtime_seconds: `7.234`

Search (Wikipedia, provider: `wikipedia`):
- success_rate: `1.00`
- average_steps: `5.0`
- average_retries: `0.0`
- anti_bot_detection_rate: `0.0`
- average_completion_confidence: `0.9192`
- average_runtime_seconds: `7.198`

#### B. Degraded-Mode Baseline (Ollama Unavailable)
Source: Baseline benchmark run (iterations=5, generated `2026-09-08T06:39:12Z`).
Health status: `llm_available_rate: 0.0`, `degraded_mode_rate: 1.0` (deterministic fallback path only).

Search (DuckDuckGo):
- success_rate: `1.00`
- average_steps: `5.0`
- average_retries: `0.0`
- anti_bot_detection_rate: `0.0`
- average_completion_confidence: `0.8183`
- average_runtime_seconds: `6.484`

Form fill (DemoQA):
- success_rate: `1.00`
- average_steps: `6.0`
- average_retries: `0.0`
- anti_bot_detection_rate: `0.0`
- average_completion_confidence: `0.95136`
- average_runtime_seconds: `7.7572`

Login (The Internet):
- success_rate: `1.00`
- average_steps: `4.0`
- average_retries: `0.0`
- anti_bot_detection_rate: `0.0`
- average_completion_confidence: `0.7904`
- average_runtime_seconds: `9.4968`


## 8. Degraded Mode (LLM Unavailable)
When Ollama is down:
- runtime continues with deterministic behavior.
- workflows avoid hard crashes due to LLM outages.
- reports include LLM availability/degraded flags.
- benchmark separates infra-failure context from workflow-failure context.

This allows meaningful reliability testing of deterministic automation paths.

## 9. Observability and Reports
### 9.1 Artifact Roots
- `artifacts/runtime/duckduckgo_search/`
- `artifacts/runtime/demoqa_form/`
- `artifacts/runtime/login_flow/`
- `artifacts/runtime/benchmark/`

### 9.2 Engineering Documentation Produced
- `docs/ENGINEERING_AUDIT.md`
- `docs/DEGRADED_MODE_ANALYSIS.md`
- `docs/SEARCH_STABILIZATION_REPORT.md`
- `docs/SEARCH_CONFIDENCE_REGRESSION_ANALYSIS.md`
- `docs/LLM_PATH_VALIDATION_ANALYSIS.md`

These documents capture evidence-based QA/reliability cycles and bottleneck analysis.

### 9.3 Example Execution Trace (Successful Workflow)
Example source: `artifacts/runtime/duckduckgo_search/reports/duckduckgo_search_report.json`

Observed successful search sequence:
1. `goto` DuckDuckGo homepage (`goto_validated`)
2. `fill` search input with query (`fill_validated`)
3. `submit` via Enter/click submit path (`submit_validated`)
4. `extract_text` results content (`extract_text_validated`)
5. final validation passed (`validation_passed=true`, `completion_confidence=0.9683`)

## 10. What Has Been Accomplished Till Date
### Foundation
- Implemented typed modular architecture for browser autonomy.
- Implemented deterministic-first orchestration patterns.
- Established structured logging conventions.

### Workflow Engine
- Built `AgentLoop` with action selection, execution, validation, recovery.
- Added loop guards for repeated/stagnant behavior.
- Added interstitial/anti-bot detection pathways.

### Reliability Infrastructure
- Added full execution trace system with export and timeline rendering.
- Added runtime test harness and benchmark suite.
- Added deterministic validators for search/form/login outcomes.

### LLM Resilience
- Added Ollama availability probing + cache/cooldown.
- Added graceful fallback in decision flow.
- Added degraded-mode behavior and reporting semantics.

### Domain Strategy Layer
- Added search/form/login explicit state machines.
- Added state transition logging hooks.
- Added provider-aware search execution groundwork.

### QA Cycles and Hardening
- Ran repeated benchmark cycles with before/after comparisons.
- Fixed false-negative form submission validation behavior.
- Stabilized submit semantics to avoid submit-as-fill validation ambiguity.
- Investigated search confidence regression and identified evaluator calibration bottleneck.

### 10.1 Current Maturity Status
Completed:
- Modular workflow engine with typed action execution/validation/recovery.
- Runtime trace/report pipeline with benchmark harness.
- Degraded-mode deterministic fallback and LLM availability handling across both live and offline states.
- LLM-assisted decision path and ambiguous-state evaluation verified against live Ollama (`tests/runtime/test_llm_ambiguous_path.py`) and wired into standard pytest suite.
- Anti-bot and interstitial detection verified against genuine Cloudflare/captcha/Access-Denied fixtures and live browser challenges (`tests/test_anti_bot_detection.py`), proving high-precision discrimination between true blocks and benign text.
- Provider-level metrics propagation into aggregate benchmark summaries and JSON reports.

Partial:
- Search confidence calibration during intermediate query submission steps.
- Alignment between workflow success signals and benchmark success accounting.

Future Work:
- Implement bounded recovery pivots for anti-bot interstitials (back-off delays, session persistence, mirror endpoints) before terminating.
- Tighten search-state intermediate confidence mapping to prevent unnecessary ambiguous dips.
- Improve benchmark scoring semantics for degraded-mode passes.

### 10.2 Engineering Concepts Demonstrated (Resume Talking Points)
- Deterministic-first autonomous browser orchestration.
- Typed workflow state machines with explicit transition logging.
- Graceful degradation and fault isolation for unavailable external AI services.
- Structured observability: step traces, validation evidence, and reliability metrics.
- Reliability engineering loops: before/after benchmark comparison and bottleneck-driven fixes.
- Bounded retry/recovery and loop-guard safety mechanisms for autonomous agents.

## 11. Known Limitations
- Anti-bot recovery is currently observational: when genuine challenges or access restrictions occur, the system accurately detects them and terminates with `blocked_by_anti_bot`, but does not attempt interactive captcha solving or proxy rotation.
- Standard benchmark workflows intentionally resolve deterministically via domain state machines; non-deterministic LLM decision pathways require ambiguous task prompts or unfamiliar layouts (`tests/runtime/test_llm_ambiguous_path.py`) to actively engage.

## 12. Immediate Next Priorities
1. Develop bounded recovery strategies for anti-bot interstitials (exponential back-off, alternative endpoint pivoting) prior to hitting termination thresholds.
2. Calibrate search-state machine intermediate confidence mapping to avoid artificial ambiguity dips during query submission.
3. Continue short, evidence-driven QA loops with localized changes only.


## 13. Setup and Usage
### 13.1 Requirements
- Python 3.11+
- Playwright
- Optional local Ollama runtime

### 13.2 Install
```bash
pip install -r requirements.txt
python -m playwright install
```

### 13.3 Run a Workflow Test
```bash
python -m tests.runtime.test_duckduckgo_search
```

### 13.4 Run Reliability Benchmark
```bash
python -m tests.runtime.reliability_benchmark 5
```

## 14. Operating Principles for Future Contributions
- Keep diffs focused and localized.
- Preserve typed interfaces and module boundaries.
- Prefer deterministic control before adding heuristic complexity.
- Validate with runtime evidence, not assumptions.
- Prioritize reliability impact over architectural expansion.
