# Degraded Mode Validation Analysis (Ollama Unavailable)

Run date: 2026-05-25
Environment override used: `OLLAMA_BASE_URL=http://127.0.0.1:65530`

## Executive Summary
- Degraded mode activation works: Ollama health check returned `llm_available=False`, and runtime logs show `deterministic_fallback_mode` during agent-loop runs.
- Runtime remained operational without LLM crashes across all required commands.
- Deterministic reliability is mixed:
- `login` deterministic script passed.
- `demoqa_form` deterministic script filled fields correctly but failed submission confirmation.
- `duckduckgo_search` failed due to anti-bot redirect and produced no extracted results.
- Benchmark accounting now correctly classifies outages as infra failures (`overall_infra_failure_rate=100%`, `overall_workflow_failure_rate=0%`).

## Workflow Results

| Workflow | Status | Success/Failure | Termination Reason | Completion Confidence | llm_available | degraded_mode | infra_failure | Total Steps | Retries | Recovery Attempts | Stagnation Events | Execution Duration (s) |
|---|---|---|---|---:|---|---|---|---:|---:|---:|---:|---:|
| DuckDuckGo Search (`tests.runtime.test_duckduckgo_search`) | completed (trace finalized failed) | Failure (`validation_passed=false`) | `anti_bot_detected` | 0.000 (`validation_conf=0.400`) | false | true | true | 2 | 1 | 1 | 0 | 15.418 |
| DemoQA Form (`tests.runtime.test_demoqa_form`) | failed | Failure (`validation_passed=false`) | `form_validation_failed` | 0.450 | N/A (script is deterministic-only) | N/A (script is deterministic-only) | false | 3 | 0 | 0 | 0 | 4.957 |
| The Internet Login (`tests.runtime.test_login_flow`) | completed | Success (`validation_passed=true`) | none | N/A (no confidence metric in this script) | N/A (script is deterministic-only) | N/A (script is deterministic-only) | false | 3 | 0 | 0 | 0 | 5.316 |

Evidence sources:
- Search report: `artifacts/runtime/duckduckgo_search/reports/duckduckgo_search_report.json`
- Form report: `artifacts/runtime/demoqa_form/reports/demoqa_form_report.json`
- Login report: `artifacts/runtime/login_flow/reports/login_flow_report.json`

### Search Workflow Analysis
- Search submission occurred: No deterministic search-input submit path executed in this run; trace actions were only `goto` then `extract_text`.
- Results page reached: No. URL was Google anti-bot interstitial (`/sorry/index?...`).
- Extraction succeeded: Action-level extraction succeeded (`extract_text` step `ok`), but meaningful result extraction failed (`top_5_results=[]`).
- Validation passed: No (`validation_passed=false`, reason `anti_bot_detected`).
- Completion confidence: Not reasonable for successful completion (0.0 final confidence, 0.4 validation confidence). Correctly low given anti-bot block.
- Deterministic fallback behavior: Correctly activated (`degraded_mode=true`, log `deterministic_fallback_mode`), but fallback strategy for search still routes through Google query URL and is anti-bot fragile.

### Form Workflow Analysis
- Fields filled correctly: Yes (`fields_verified=true`).
- Validation succeeded: No (`validation_passed=false`).
- Submission confirmation detected: No (`submission_confirmed=false`).
- Degraded mode effect: Not applicable in this script because it is fully deterministic and does not invoke LLM path.
- Selector stability issues: None observed for fill selectors (`#firstName`, `#lastName`, `#userEmail`, `#userNumber`). Failure is confirmation/post-submit signal weakness, not field selector breakage.

### Login Workflow Analysis
- Authentication succeeded: Yes (`validation_passed=true`).
- Authenticated page reached: Yes (`auth_page_reached=true`, URL reached `/secure`).
- Goal evaluation correctness: Not evaluated in this deterministic script; in benchmark agent-loop login runs, average completion confidence was 0.0, indicating weak evaluator calibration in degraded mode.
- Degraded mode effect: Not applicable in this script because it is deterministic-only.

## Reliability Metrics
From `artifacts/runtime/benchmark/reliability_benchmark_report.json` (3 iterations each):

- workflow_success_rate:
- search: 0.00%
- form_fill: 0.00%
- login: 0.00%
- overall: 0.00%

- workflow_failure_rate:
- search: 0.00%
- form_fill: 0.00%
- login: 0.00%
- overall: 0.00%

- infra_failure_rate:
- search: 100.00%
- form_fill: 100.00%
- login: 100.00%
- overall: 100.00%

- degraded_mode_rate:
- search: 100.00%
- form_fill: 100.00%
- login: 100.00%

- llm_available_rate:
- search: 0.00%
- form_fill: 0.00%
- login: 0.00%

- average_steps:
- search: 2.00
- form_fill: 4.00
- login: 4.00

- average_retries:
- search: 1.00
- form_fill: 0.00
- login: 0.00

- recovery_success_rate:
- search: 0.00%
- form_fill: 0.00%
- login: 0.00%

- stagnation_frequency:
- search: 1.00
- form_fill: 1.00
- login: 1.00

- anti_bot_detection_rate:
- search: 1.00
- form_fill: 0.00
- login: 0.00

## Failure Analysis
Observed failures classified from actual outputs:

- `anti_bot_block`
- DuckDuckGo search redirected to Google anti-bot page and failed validation.

- `validation_failure`
- DemoQA form submission confirmation not detected (`submission_not_confirmed`) despite correct field fill.

- `stagnation_failure`
- Benchmark logs repeatedly show fill-loop guard triggers (`repeated_fill_detected`, `loop_guard_triggered`) in form/login agent-loop runs.

- `recovery_failure`
- Search had recovery attempts but no successful outcome (`recovery_attempts=1`, validation failed).

- `reasoning_failure`
- Not primary in this run; LLM path intentionally disabled/unavailable.

- `selector_failure`
- Not primary in standalone deterministic scripts; selectors worked for core fields/login controls.

- `navigation_failure`
- Not dominant except anti-bot redirect path in search.

- `infra_failure`
- By design in this test: Ollama unavailable for every benchmark run; correctly captured as infra rather than workflow logic failure.

## Architectural Assessment

### Degraded mode behavior
- Good: no LLM outage crashes, deterministic path auto-activates, logs are explicit.
- Weak: deterministic search strategy uses Google query URL first and is highly anti-bot susceptible.

### Recovery quality
- Basic but limited. Recovery attempts do not overcome anti-bot or repeated-fill stagnation in benchmark runs.

### Validator quality
- Mixed.
- Good: login validator catches authenticated state reliably.
- Weak: form validator/submission confirmation is brittle on DemoQA (submission attempt happened; confirmation not reliably recognized).

### Goal evaluator quality
- Weak under degraded benchmark conditions (0.0 average confidence for search/login benchmark workflows).
- Confidence calibration appears too pessimistic or missing workflow-specific positive signals in degraded runs.

### Benchmark quality
- Improved and useful: infra vs workflow separation is now explicit and prevents misattributing LLM outages to workflow logic.

## Highest Impact Improvements
Ranked by severity, expected gain, and effort.

1. Search deterministic strategy hardening
- Severity: High
- Expected reliability gain: High
- Effort: Medium
- Action: default to DuckDuckGo in degraded mode, avoid Google direct-query anti-bot path, detect/search-box submit on page before query URL navigation.

2. Form submission confirmation robustness
- Severity: High
- Expected reliability gain: High
- Effort: Medium
- Action: validate multiple deterministic post-submit signals (modal title variants, form hidden/disabled state, URL/hash/query changes, toast/alert selectors).

3. Fill-loop and stagnation mitigation refinement
- Severity: High
- Expected reliability gain: Medium-High
- Effort: Medium
- Action: stronger exit/pivot actions after repeated fill (explicit submit selector maps per domain + safe extraction fallback).

4. Degraded-mode workflow-specific deterministic policies
- Severity: Medium-High
- Expected reliability gain: Medium
- Effort: Medium
- Action: explicit per-task plans in degraded mode (search/form/login state machines) before generic fallback.

5. Goal evaluator calibration for deterministic runs
- Severity: Medium
- Expected reliability gain: Medium
- Effort: Low-Medium
- Action: raise confidence when deterministic success indicators are present; avoid near-zero confidence after known-success login paths.

## Final Engineering Verdict
- What works reliably today:
- LLM outage handling and degraded-mode activation.
- Deterministic login script against The Internet demo page.

- What partially works:
- Deterministic form fill (field population works) but end-state validation/submission confirmation is inconsistent.
- Search execution loop runs, but outcome quality is poor under anti-bot pressure.

- What remains fragile:
- Search in degraded mode (anti-bot resilience and result extraction quality).
- Agent-loop form/login behavior in benchmark mode (fill-loop stagnation and low confidence scoring).

- Estimated workflow reliability by category (current evidence):
- Search: Low
- Form fill: Low-Medium
- Login: Medium-High for deterministic direct script; Low in current benchmark agent-loop mode

- Top 5 next actions to improve autonomous task completion:
1. Replace degraded search default path with anti-bot-aware provider/flow.
2. Strengthen deterministic submit/confirmation detectors for forms.
3. Add domain-scoped deterministic state machines for login/form/search in degraded mode.
4. Tune loop guards and recovery pivots to reduce repeated fill stagnation.
5. Recalibrate goal evaluator confidence from deterministic success signals.
