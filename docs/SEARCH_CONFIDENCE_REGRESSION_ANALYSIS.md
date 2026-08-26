# Search Confidence Regression Analysis

Date: 2026-05-25

## Scope
Investigation only. No code changes in this analysis.

Artifacts analyzed:
- Search benchmark report: `artifacts/runtime/benchmark/reliability_benchmark_report.json`
- Baseline benchmark report: `artifacts/runtime/benchmark/reliability_benchmark_report_before_provider_rotation.json`
- Per-run traces:
- `artifacts/runtime/benchmark/runner_artifacts/bench_search_duckduckgo_iter1/traces/bench_search_duckduckgo_iter1-20260525-070116.json`
- `artifacts/runtime/benchmark/runner_artifacts/bench_search_duckduckgo_iter2/traces/bench_search_duckduckgo_iter2-20260525-070141.json`
- `artifacts/runtime/benchmark/runner_artifacts/bench_search_duckduckgo_iter3/traces/bench_search_duckduckgo_iter3-20260525-070204.json`
- `artifacts/runtime/benchmark/runner_artifacts/bench_search_duckduckgo_iter4/traces/bench_search_duckduckgo_iter4-20260525-070227.json`
- `artifacts/runtime/benchmark/runner_artifacts/bench_search_duckduckgo_iter5/traces/bench_search_duckduckgo_iter5-20260525-070250.json`

## Summary Finding
Completion confidence dropped primarily because GoalEvaluator repeatedly outputs `completion_reason=blocked_by_anti_bot` despite validator/interstitial signals showing no anti-bot block.

Root-cause classification:
- `evaluator_calibration`

Secondary contributing factor:
- `state_machine_transition_issue` (extra extract step increases stagnation/loop-risk penalties), but this is not the primary cause of the confidence collapse.

## Evidence Chain
1. ActionValidator outputs are healthy for search runs.
- `goto_validated`, `fill_validated`, `submit_validated`, `extract_text_validated`.
- `interstitial.detection_type` at goto step is `none` with `confidence=0.0`.

2. Results page is reached in all current benchmark runs.
- URLs include DuckDuckGo search query parameters (`q=Python+internships`).

3. Extraction completes successfully in all runs.
- `extract_text` steps are successful.

4. GoalEvaluator output contradicts validator/interstitial.
- Every step’s goal evaluation reason is `blocked_by_anti_bot`.
- Confidence decays across steps in each run:
- Step 1: `0.14262`
- Step 2/3: `0.106915...`
- Step 4: `0.097838...`
- Step 5: `0.023056...`

5. Aggregate metric impact.
- Before stabilization: search `average_completion_confidence=0.88552`, `average_steps=3.0`.
- After stabilization: search `average_completion_confidence=0.0731`, `average_steps=5.0`.

## Per-Run Search Benchmark Report
Provider metrics are not exported per-run by current benchmark output; provider is inferred from URLs in traces.

| Run | Provider Used | Results Page Reached | Extraction Completed | Validation Passed | Completion Confidence | Termination Reason |
|---|---|---|---|---|---:|---|
| iter1 | DuckDuckGo (inferred) | yes | yes | yes | 0.023056 | none |
| iter2 | DuckDuckGo (inferred) | yes | yes | yes | 0.023056 | none |
| iter3 | DuckDuckGo (inferred) | yes | yes | yes | 0.023056 | none |
| iter4 | DuckDuckGo (inferred) | yes | yes | yes | 0.023056 | none |
| iter5 | DuckDuckGo (inferred) | yes | yes | yes | 0.023056 | none |

Notes:
- “Validation Passed” here means action-level/trace workflow progression is successful (`status=completed`, no termination reason in trace).
- Benchmark success-rate remains 0 due broader benchmark scoring/accounting logic, not because these per-run search traces failed action flow.

## Why Confidence Decreased
Direct cause:
- GoalEvaluator anti-bot heuristic is over-triggering (`blocked_by_anti_bot`) in normal search-result content.
- This drives confidence down each step, and progress-adjustment penalties compound the drop.

Contributing factor:
- Stabilized state machine now includes an extra explicit validation phase (more steps), increasing stagnation/loop-risk contribution in progress-adjusted confidence.

## Single Highest-Impact Fix (Recommendation Only)
Narrow anti-bot detection in GoalEvaluator to high-precision signals only.

Specifically:
1. Remove/relax generic text token triggers (especially broad token `challenge`).
2. Require strong anti-bot evidence before setting `blocked_by_anti_bot`:
- URL/path indicators (`/sorry/`, known captcha/challenge endpoints), or
- validator interstitial signal (`detection_type` in `anti_bot|captcha|access_denied`) when available.
3. Do not override search confidence to anti-bot solely from generic page text.

Expected effect:
- Immediate restoration of realistic completion confidence while preserving true anti-bot detection behavior.
