# Stage 5B Enhanced Highlight Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver an opt-in Meme & Comedy / Enhanced production selector that uses the existing Viewer Judge as a bounded, observable final decision stage.

**Architecture:** Preserve the present discovery calls and add a normalized selection policy carried by job options and creator settings. Recorded and live paths collect bounded candidates, deduplicate before judging, then retain only successful Viewer Judge results at/above the configured score. Diagnostics and usage distinguish discovery from viewer review; visual input is optional and capped.

**Tech Stack:** Python 3.11, FastAPI, existing OpenRouter client, plain HTML/CSS/JavaScript, pytest-style executable test scripts.

**Spec:** `docs/superpowers/specs/2026-11-27-stage5b-enhanced-highlight-design.md`

## Global Constraints

- Work directly in clean `main`; no new branches, worktrees, dependencies, provider-default changes, paid re-analysis, or auto-posting.
- Balanced/Fast remains the compatibility default and must retain existing behavior.
- Use only existing `brain.viewer_judge()`; no duplicate judge.
- Enhanced candidate caps: 2–3x requested clips, maximum 20/hour, deduplicate before judge; visual frames default off and max 12.
- Do not log keys. Bound transient retries, timeout, cancellation, token/request/cost limits, and make failed judge status explicit.

## Review Focus

- Legacy saved job/creator without new fields normalizes to Balanced/Fast; covered in Task 1 persistence tests.
- A viewer failure cannot create an automatic Enhanced clip or fabricate a score; covered in Task 2 failure tests.
- Overlapping and repeated candidates are judged once and progressive quota remains distributed; covered in Task 2 recorded tests.
- Live events with fewer than eight words only proceed with corroborating context; false signal spikes do not clip; covered in Task 3 tests.
- Visual input uses no more than 12 frames and unsupported modality preserves the job; covered in Task 2 visual tests.

### Task 1: Normalize and persist selection policy

**Files:**
- Modify: `studio/jobs.py`, `studio/creators.py`, `studio/server.py`
- Test: `tests/test_highlight_enhanced.py`

**Interfaces:**
- Produces `jobs.selection_policy(options, creator=None) -> dict` with validated profile, quality, judge model, score, visual and bounded limits.
- Produces creator defaults persisted by `creators.update()` and inherited when a job/live session begins.

- [ ] Write failing tests proving legacy options normalize to Balanced/Fast, new video/upload options persist, and creator settings survive reload.
- [ ] Run `..venv\\Scripts\\python.exe tests/test_highlight_enhanced.py`; expect assertions for absent policy normalization to fail.
- [ ] Implement the smallest normalization/validation and job/creator inheritance path; preserve current public shapes except additive fields.
- [ ] Re-run the focused test; expect PASS.

### Task 2: Enhanced recorded candidate selection

**Files:**
- Modify: `studio/brain.py`, `studio/jobs.py`, `studio/usage.py`, `studio/ai.py`
- Test: `tests/test_highlight_enhanced.py`

**Interfaces:**
- Extends `brain.find_clips(..., selection=None, cancel=None) -> (clips, diagnostics)`.
- Produces clips carrying `original_ai_score`, `viewer` result and selection explanation only in Enhanced mode.

- [ ] Write failing tests for Meme prompt criteria, 2x candidate pool, deduplication before judge, viewer-score filtering/ranking, progressive quota, invalid JSON/provider errors, model fallback status, caps, and visual-frame bound.
- [ ] Run the focused test; expect failures because selection policy is not consumed.
- [ ] Implement profile-specific discovery prompt, bounded candidate gathering, local dedupe, existing `viewer_judge()` invocation with context/signals/focus, cost estimate/latency/rejection diagnostics, cancellation and one transient retry.
- [ ] Re-run focused tests; expect PASS.

### Task 3: Enhanced live candidate routes

**Files:**
- Modify: `studio/live.py`, `studio/brain.py`
- Test: `tests/test_highlight_enhanced.py`, `tests/test_live.py`

**Interfaces:**
- Extends Enhanced Meme & Comedy live `_ai_check()` diagnostics with semantic, corroborated signal, and borderline sources.
- Preserves current Balanced/Fast Words/Reaction result exactly.

- [ ] Write failing tests for quiet semantic event, low-word corroborated event, false audio spike, dedupe, and viewer judge failure.
- [ ] Run focused tests; expect failure because those paths are unavailable.
- [ ] Add bounded source creation and Viewer Judge gating under Enhanced Meme & Comedy only; keep buffer/rate/response limits and never auto-clip weak signals.
- [ ] Re-run focused live tests; expect PASS.

### Task 4: Compact production UI

**Files:**
- Modify: `web/index.html`
- Test: `tests/test_static.py`, `tests/test_highlight_enhanced.py`

**Interfaces:**
- Composer submits the normalized job policy for links and uploads.
- Creator setup saves the same defaults and cards render Viewer Score/explanation additively.

- [ ] Write failing behavior tests covering fields in link/upload payloads and creator save/load; add a static rendering assertion for distinct AI and Viewer scores.
- [ ] Run focused tests; expect missing settings/rendering failure.
- [ ] Add compact controls in existing option panels and card fields without template coupling or internal-metric overload.
- [ ] Re-run focused tests; expect PASS.

### Task 5: Evidence, smoke and release

**Files:**
- Create: `docs/stage5b/` only for generated evidence, if tests produce it.

- [ ] Query OpenRouter model metadata without exposing credentials; record model IDs, supported text/image modality, catalog pricing and the distinction from actual billing.
- [ ] Run controlled recorded and fixture-live Enhanced smoke with total API estimate <= $0.50; reuse transcript/source where possible and make no old-job calls.
- [ ] Verify every rendered smoke MP4 with FFprobe and full decode; capture hashes and diagnostic selection evidence.
- [ ] Run `$env:PYTHONUTF8 = "1"; ..venv\\Scripts\\python.exe -m pip check`, `..venv\\Scripts\\python.exe tests/run.py`, and `git diff --check`.
- [ ] Only if green: commit to `main`, push `origin/main`, fetch, verify `HEAD == origin/main`, clean worktree, one worktree, and health/UI availability after safe server restart.

## Self-review

All requested areas map to Tasks 1–5. The plan keeps the user-required model selection, bounded costs/calls, rejection observability, visual option, UI, recorded/upload/live coverage, smoke media evidence and main-first release gate. It intentionally leaves audience-quality and actual billing as separate, unproven gates.
