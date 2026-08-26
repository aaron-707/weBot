# ENGINEERING AUDIT - weBot

Date: 2026-05-13
Scope: Current code under `src/webot`, `main.py`, and `tests/manual`.
Method: Static code audit only (no code changes).
Tone: Brutally honest, production-readiness focused.

## Maturity Legend
- COMPLETED: production-ready or close; stable and reliable
- PARTIAL: implemented but limited; meaningful gaps remain
- PLACEHOLDER / EXPERIMENTAL: prototype-level behavior
- HIGH RISK: likely failure points or architectural fragility

## Module-by-Module Audit

### BrowserController (in `main.py` and `tests/manual/test_google_search.py`)
- Current maturity level: PARTIAL
- Strengths
- Uses Playwright async lifecycle correctly (`start` -> `launch` -> `new_context` -> `new_page`).
- Applies default timeout and navigation timeout.
- In `main.py`, shutdown path aggregates close errors instead of silently swallowing.
- Weaknesses
- Not a reusable domain module (`browser/` package is effectively empty).
- Two controller implementations exist (main vs manual test) with duplicated lifecycle logic.
- No context-level policies for retries, downloads, popups, permissions, storage state, or anti-bot-safe configuration.
- Technical debt
- Lifecycle and browser primitives are not centralized in `src/webot/browser/`.
- No typed result contract for browser operations.
- Known failure modes
- Context close/playwright close race and close exceptions can bubble at awkward times.
- No built-in handling for transient navigation/network hangs beyond Playwright timeouts.
- Reliability concerns
- Adequate for bootstrap demos; too thin for reliable multi-site automation.
- Recommended next improvements
- Move to `browser/controller.py` as single implementation.
- Add structured operation wrappers (`goto`, `click`, `fill`) with consistent telemetry and error taxonomy.
- Introduce resilient context options and deterministic page readiness helpers.

### DOM Extraction (`intelligence/dom_extractor.py`)
- Current maturity level: PARTIAL
- Strengths
- Deterministic JS extraction path with visibility filtering.
- Selector building inside page JS attempts stable-first preference (`id`, `data-testid`, `name`, role/class fallback).
- Normalization and de-duplication implemented.
- Weaknesses
- `extract_visible_text` queries many generic tags including `div`, likely producing noisy, large payloads.
- Fallback strategy stops after first non-empty selector bucket, which may miss better candidates.
- No frame/iframe support.
- No explicit bounding of total extracted element count per bucket.
- Technical debt
- Heavy extraction cost each step in `AgentLoop` without adaptive throttling.
- Visibility heuristics are good but not robust for virtualized UIs/shadow DOM.
- Known failure modes
- DOM bloat and token bloat from large pages.
- Missed actionable elements in shadow DOM or deeply dynamic components.
- Reliability concerns
- Reasonable for simple pages; inconsistent on complex SPAs.
- Recommended next improvements
- Add per-bucket caps and configurable extraction profiles.
- Add optional iframe traversal and shadow root probing.
- Distinguish actionable text from generic layout text.

### Selector Generation (`intelligence/selector_builder.py`)
- Current maturity level: PLACEHOLDER / EXPERIMENTAL
- Strengths
- Ranks selector stability and emits Playwright-style selectors.
- Tries semantic locator paths (`label`, `role`, `placeholder`) before CSS.
- Weaknesses
- Not integrated into `AgentLoop`/`ActionExecutor` path.
- Generated strings include `page.` prefix; this is not directly consumable by executor expecting raw locator strings.
- No runtime validation against actual page for uniqueness/visibility.
- Technical debt
- Dead/unused module risk.
- Stability ranking is hand-tuned heuristic without empirical backing.
- Known failure modes
- Produced selector may not execute in current action pipeline.
- Overconfidence in rank for ambiguous labels/text.
- Reliability concerns
- Currently more conceptual than operational.
- Recommended next improvements
- Integrate into recovery + decision flow as selector-candidate generator.
- Emit raw selector contracts compatible with executor, not code snippets.
- Add online selector validation and fallback chain generation.

### ActionExecutor (`workflows/action_executor.py`)
- Current maturity level: PARTIAL
- Strengths
- Strong input validation and bounded retry loop.
- Clear typed action/result contracts.
- Deterministic execution path for `goto`, `click`, `fill`, `extract_text`.
- Weaknesses
- Single retry policy for all action types; no per-error strategy.
- No smart wait logic around click/fill preconditions (attached, visible, enabled, stable).
- Uses `structlog` while rest of system largely uses standard logger utility; observability is inconsistent.
- Technical debt
- Retry jitter/backoff absent.
- No circuit breaker for repeating hard failures.
- Known failure modes
- Repeating same failing selector burns retries with low chance of recovery.
- Clicks can fail due to transient overlays without automatic pre-check mitigation.
- Reliability concerns
- Good skeleton, not yet robust under hostile/dynamic DOM conditions.
- Recommended next improvements
- Add action-type-specific preconditions and postconditions.
- Align logging stack with shared logger contract.
- Add richer error classification output for recovery engine.

### ActionValidator (`workflows/action_validator.py`)
- Current maturity level: PARTIAL
- Strengths
- Distinct validation logic per action type.
- Includes interstitial detection in `goto` validation.
- Captures before/after state for click validation.
- Weaknesses
- `click` success inferred from broad signals (DOM/modal/url changes), can produce false positives/false negatives.
- `fill` validation only checks exact input value readback; ignores masked/normalized fields.
- Uses body text preview and simplistic heuristics for page state.
- Technical debt
- No event-level instrumentation (network idle deltas, mutation observer based checkpoints).
- Selector state probing is shallow for complex widgets.
- Known failure modes
- Click considered successful when unrelated async DOM churn occurs.
- False failure on controlled inputs that transform values.
- Reliability concerns
- Valuable safety layer, but currently heuristic-heavy.
- Recommended next improvements
- Introduce action-specific observables and stronger deterministic assertions.
- Add widget-aware fill verification (select2, combo boxes, masked inputs).
- Differentiate hard failure vs uncertain validation for smarter recovery decisions.

### AgentLoop (`workflows/agent_loop.py`)
- Current maturity level: PARTIAL
- Strengths
- Clear orchestration boundary and typed history records.
- Deterministic-first strategy implemented before LLM fallback.
- Includes loop guards (repeated goto, ping-pong navigation, repeated fill).
- Integrates recovery and anti-bot termination thresholds.
- Weaknesses
- Very search/Google-shaped deterministic logic in core loop (query extraction and search-specific pivots).
- Completion policy can terminate on `extract_text` success even if task intent not truly satisfied.
- Context compaction still may carry noisy text into LLM prompt.
- Technical debt
- Orchestration owns too many domain-specific heuristics; risks becoming monolith.
- No pluggable strategy registry per task/domain.
- Known failure modes
- Premature completion on non-goal text extraction.
- Repeated fill/click loops on non-search forms despite safeguards.
- Reliability concerns
- Best-implemented subsystem, but fragile beyond narrow task classes.
- Recommended next improvements
- Split deterministic strategies into composable policy modules.
- Introduce explicit goal checkpoints and intent-aligned completion criteria.
- Add per-task execution plans rather than purely stepwise reactive loop.

### RecoveryEngine (`workflows/recovery_engine.py`)
- Current maturity level: PLACEHOLDER / EXPERIMENTAL
- Strengths
- Bounded retries and explicit stop for anti-bot/fill-stagnation.
- Supports candidate selector fallback and optional LLM fallback.
- Weaknesses
- Retry counting key is simplistic (`action:selector`) and can misrepresent true retry context.
- Recovery paths mostly retry/refresh/alt-selector; no deeper remediation patterns.
- LLM fallback prompt is minimal and unconstrained by real page state.
- Technical debt
- No probabilistic ranking of recovery strategies.
- No memory of historically successful remediation by domain/page type.
- Known failure modes
- Can recycle failing actions after superficial refresh.
- Alternative selector search only checks provided candidates, no active discovery path.
- Reliability concerns
- Better than no recovery, but still prototype-grade for production reliability.
- Recommended next improvements
- Add deterministic selector re-discovery using DOM extractor + selector builder.
- Add error-type-specific recovery playbooks.
- Persist and reuse successful recovery patterns.

### ProgressTracker (`workflows/progress_tracker.py`)
- Current maturity level: PARTIAL
- Strengths
- Thoughtful stagnation and loop-risk scoring with bounded signals.
- Includes anti-bot and repeated-fill pressure signals.
- Produces compact recommendation output.
- Weaknesses
- Heuristic weights are static and uncalibrated.
- Tracker is not tightly integrated into live loop control (used mainly in manual harness + goal eval adjustments).
- Technical debt
- No data-driven tuning from historical runs.
- No confidence intervals/uncertainty on recommendations.
- Known failure modes
- False termination recommendations in noisy DOM environments.
- Missed stagnation when actions differ slightly but are semantically equivalent failures.
- Reliability concerns
- Useful diagnostics tool; currently weak as control authority.
- Recommended next improvements
- Integrate directly into loop termination/pivot decisions.
- Calibrate weights from recorded run corpus.
- Add semantic action similarity, not only string equality.

### GoalEvaluator (`workflows/goal_evaluator.py`)
- Current maturity level: PARTIAL
- Strengths
- Task-type inference and deterministic heuristics for multiple workflows.
- Uses progress signals to adjust confidence and termination behavior.
- LLM fallback only in ambiguous confidence band (good cost control).
- Weaknesses
- Heuristics are broad and can be fooled by incidental page text.
- Goal completion criteria are not grounded in user-provided explicit success conditions.
- Technical debt
- Rule explosion risk as more task types are added.
- No benchmark-based calibration for confidence thresholds.
- Known failure modes
- Overestimates completion when generic success keywords appear.
- Underestimates completion for atypical UIs with weak textual indicators.
- Reliability concerns
- Good start, not trustworthy enough for autonomous high-stakes completion decisions.
- Recommended next improvements
- Add explicit success predicates from task interpreter/planner.
- Store per-task evidence traces used for confidence.
- Add evaluation tests from curated real-world scenarios.

### InterstitialDetector (`intelligence/interstitial_detector.py`)
- Current maturity level: PARTIAL
- Strengths
- Deterministic and lightweight.
- Covers anti-bot, access denied, login wall, cookie wall classes.
- Weaknesses
- Pure keyword matching with no contextual disambiguation.
- No site-specific challenge fingerprints (Cloudflare variants, bot wall JS challenges).
- Technical debt
- Signal sets are small and static.
- No tuning via observed false positives/negatives.
- Known failure modes
- False positives from benign content mentioning “cookies” or “challenge”.
- Missed detection for non-English or custom-styled challenge pages.
- Reliability concerns
- Useful guardrail, but not robust anti-bot intelligence.
- Recommended next improvements
- Add structured challenge indicators (DOM patterns, status cues).
- Add per-domain tuning and allowlist/denylist overrides.

### Ollama Integration (`llm/ollama_client.py`)
- Current maturity level: PARTIAL
- Strengths
- Clear API wrapper, retry behavior, and endpoint normalization.
- Connection and model availability checks implemented.
- Validates prompt/model presence and response shape.
- Weaknesses
- Uses blocking `requests` in otherwise async system (can block event loop paths if called directly from async context).
- No request-level token/time budgeting or cancellation support.
- Technical debt
- No circuit breaker or adaptive backoff.
- Limited telemetry around latency and failure classes.
- Known failure modes
- Slow/hanging local model degrades full loop responsiveness.
- Large prompts can spike latency and downstream timeouts.
- Reliability concerns
- Functional for local prototype use; not hardened for sustained autonomous operation.
- Recommended next improvements
- Move to async HTTP client with timeout tiers and cancellation.
- Add prompt-size guards and response-length controls.
- Add robust retries with exponential backoff and error classes.

### DecisionEngine (`llm/decision_engine.py`)
- Current maturity level: PARTIAL
- Strengths
- Tight JSON schema expectations and fallback action path.
- Prompt is compact compared to raw full DOM dumps.
- Weaknesses
- Still susceptible to prompt drift and malformed model outputs.
- Only first 40 elements are included; selection can miss critical elements.
- Fallback behavior (first visible button) can be dangerous on unknown pages.
- Technical debt
- No confidence score or rationale from model.
- No guardrails for high-impact actions (e.g., destructive clicks).
- Known failure modes
- Incorrect action on ambiguous UIs.
- Repetitive click fallback loops when model output invalid.
- Reliability concerns
- Adequate as secondary policy layer, not reliable as primary planner.
- Recommended next improvements
- Add deterministic candidate action set, ask LLM to rank not invent.
- Add action-risk gating and allowlist-based execution constraints.
- Improve DOM compression strategy (actionability-first sampling).

### FormFiller (`workflows/form_filler.py`)
- Current maturity level: PARTIAL
- Strengths
- Deterministic field detection and mapping first, optional LLM fallback second.
- Supports profile typing and result reporting.
- Weaknesses
- Selector generation for fields can be unstable (`tag` fallback too broad).
- Limited profile schema and field semantics.
- `select` handling only by label match, no value/index fallback.
- Technical debt
- No handling of multi-step forms, dependent fields, async validation messages.
- No validation loop after fill to detect rejection/format issues.
- Known failure modes
- Wrong deterministic mapping (e.g., “name” collisions) fills incorrect fields.
- Hidden/duplicate fields may get matched unexpectedly.
- Reliability concerns
- Works for simple forms; unreliable for modern complex application forms.
- Recommended next improvements
- Use stronger field identity signatures and scope by form container.
- Add post-fill validation and correction pass.
- Expand profile model and field normalization policies.

### SessionMemory (`memory/session_memory.py`)
- Current maturity level: PARTIAL
- Strengths
- Clean bounded in-memory action history with retry/success selector tracking.
- Simple typed action records.
- Weaknesses
- Retry state is ephemeral and per-process only.
- No richer temporal/causal linking between failures and page states.
- Technical debt
- Missing persistence or snapshot export.
- No memory pruning by semantic importance.
- Known failure modes
- On restart, system forgets successful selectors and failure history.
- Retry counters may reset in ways that underrepresent unstable selectors.
- Reliability concerns
- Fine for single-session loop control, weak for longer-running agent resilience.
- Recommended next improvements
- Add optional persistent session snapshots.
- Track failure signatures with page/domain context.
- Add APIs for memory-guided strategy selection.

### Logging / Observability (`utils/logger.py`, mixed usage across modules)
- Current maturity level: PARTIAL
- Strengths
- Structured JSON logs with rotation.
- Captures essential record metadata.
- Weaknesses
- Inconsistent logging stack (`structlog` in ActionExecutor vs shared logger elsewhere).
- `extra` payload fields are not explicitly serialized by formatter (custom fields may be lost unless embedded in message).
- `src/webot/logging/logging.yaml` exists but appears unused.
- Technical debt
- No trace IDs, run IDs, or step correlation IDs across loop.
- No metrics pipeline (success rate, retries, latency histograms).
- Known failure modes
- Difficult root-cause analysis across modules due to inconsistent log shape.
- Missing context around why decisions were made.
- Reliability concerns
- Observability is adequate for manual debugging, not for production SRE-grade diagnostics.
- Recommended next improvements
- Standardize logger usage and payload schema.
- Add correlation IDs per run/step/action.
- Emit machine-parseable decision/recovery evidence fields.

### Config / Settings (`config/settings.py`, `.env`)
- Current maturity level: COMPLETED (for current scope)
- Strengths
- Typed settings via Pydantic with environment support.
- Supports both nested and flat env override conventions for Ollama.
- Includes source-resolution helper for active config introspection.
- Weaknesses
- Config scope is still minimal relative to workflow complexity.
- Technical debt
- Missing explicit schema for strategy thresholds currently hardcoded in modules.
- Known failure modes
- Invalid env values outside current validated fields can silently bypass intended behavior.
- Reliability concerns
- Strong foundation; likely stable.
- Recommended next improvements
- Externalize major heuristic thresholds into config.
- Add startup config validation report for operational clarity.

### Manual Tests (`tests/manual/test_google_search.py`)
- Current maturity level: PLACEHOLDER / EXPERIMENTAL
- Strengths
- Useful integration harness wiring all core modules.
- Produces rich debug output and log file.
- Weaknesses
- Not automated CI-grade tests; manual, environment-dependent, and flaky by design.
- Only one scenario (Google search) with strong coupling to anti-bot-sensitive target.
- No unit test coverage for critical deterministic branches.
- Technical debt
- `tests/` lacks mirrored unit/integration structure promised by architecture doc.
- No deterministic mocks for LLM/browser/network behaviors.
- Known failure modes
- Fails due to external site behavior changes, anti-bot prompts, locale changes.
- Reliability concerns
- Demonstration artifact, not a reliability safety net.
- Recommended next improvements
- Add fast deterministic unit tests per module.
- Add hermetic integration tests on controlled local test pages.
- Keep manual tests as optional exploratory checks only.

## HIGH RISK Register (Cross-Cutting)
- Orchestration fragility: `AgentLoop` mixes generic orchestration with domain-specific search heuristics.
- Token explosion risk: DOM extraction plus text-heavy prompts can balloon quickly on large pages.
- Selector fragility: extraction selectors and fallback actions are not strongly validated against ambiguity.
- Recovery shallowness: retries/refresh dominate; limited deep remediation paths.
- Async/blocking mismatch: synchronous Ollama requests can degrade async responsiveness.
- Test coverage risk: near-zero deterministic automated coverage for core behavior.
- External dependency volatility: reliance on public search engines in manual scenario invites anti-bot instability.

## System-Level Assessment

### Biggest architectural strengths
- Clear module boundaries by concern (intelligence/workflows/llm/memory/config).
- Deterministic-first intent is present in several paths.
- Typed data structures and explicit method contracts in many modules.

### Biggest architectural risks
- Orchestration centralization in `AgentLoop` is becoming heuristic-heavy and task-specific.
- Multiple strategy layers (deterministic, validator, recovery, goal evaluation, LLM) are not unified by a common planning state model.
- Browser primitives are not formalized in `browser/` package despite architecture target.

### Current bottlenecks
- DOM extraction overhead on every step.
- LLM latency and malformed output handling loops.
- Weak selector resilience causing repeated failures and retries.

### Anti-patterns avoided
- No deep inheritance maze.
- Bounded retries and some loop guards exist (avoids obvious infinite loops).
- No screenshot/vision dependency (aligned with deterministic-first philosophy).

### Likely scaling problems
- Performance: token/context growth on complex pages.
- Reliability: heuristic drift as more websites/task types are added.
- Maintainability: monolithic orchestration logic accumulating special cases.

## Reliability Assessment (Estimated)
- Workflow reliability (simple deterministic tasks on friendly sites): 0.55 to 0.70
- Workflow reliability (real-world dynamic sites): 0.25 to 0.45
- Selector stability across site changes: 0.35 to 0.55
- Recovery effectiveness after first failure: 0.25 to 0.40
- Anti-bot resilience: 0.10 to 0.25

Interpretation: current system is promising as an engineering prototype, but below production-grade reliability for unattended autonomous browsing.

## Recommended Priorities

### Highest-impact next fixes (ranked)
1. Build deterministic test suite (unit + hermetic integration) for `AgentLoop`, `ActionValidator`, `RecoveryEngine`, `DomExtractor`.
2. Refactor `AgentLoop` into pluggable strategy policies; remove search-engine-specific hardcoding from core loop.
3. Integrate selector generation + validation into a robust selector resolution pipeline.
4. Upgrade Ollama client to async with strict prompt/response budget controls.
5. Standardize observability schema (run/step/action IDs, structured evidence fields).
6. Introduce explicit success criteria model from interpreted task intent.

### Unnecessary future work (for now)
1. Multi-model orchestration and model routing.
2. Advanced UI vision/screenshot reasoning.
3. Distributed execution or multi-browser parallelism.

### Features to avoid prematurely
1. Large memory persistence systems before deterministic core reliability is strong.
2. Complex plugin ecosystems before interfaces are stabilized.
3. Aggressive autonomous retries on anti-bot pages (increases block risk).

## Resume / Portfolio Assessment

### Current resume strength
- Solid: shows thoughtful architecture decomposition, typed Python, Playwright integration, and deterministic-first design intent.
- Weakness: lacks evidence of production reliability via tests/benchmarks.

### What would most improve impressiveness
1. Publish reliability metrics from repeatable benchmark scenarios.
2. Add strong automated test coverage with clear pass/fail quality gates.
3. Demonstrate robust recovery on controlled adversarial pages (dynamic DOM, delayed elements, modal interruptions).
4. Show architectural refactor from heuristic monolith loop to strategy plugin system.

### Best demo scenarios
1. Controlled local multi-step form flow with deterministic completion and recovery from injected selector changes.
2. Navigation + extraction on a stable docs site with repeatable success metrics.
3. Failure-handling demo: induced timeout/missing-selector then successful deterministic recovery.

### Strongest engineering talking points
- Deterministic-first policy layered with bounded LLM usage.
- Structured progress/stagnation scoring and anti-loop controls.
- Typed modular architecture and explicit workflow contracts.

## Final Verdict
Current weBot is a credible, well-structured prototype with several thoughtful reliability mechanisms, but it is not production-ready yet. The biggest gap is not ideas; it is validated robustness under repeatable tests and stronger selector/recovery orchestration on real dynamic pages.
