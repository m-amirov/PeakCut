# Stage 5B Enhanced Highlight Discovery Design

## Intent

Make the existing experimental `brain.viewer_judge()` a bounded production decision stage for new work only. It must make Meme & Comedy / Enhanced selection observable and controllable without changing the established Balanced / Fast path.

## Goals

- Add `highlight_profile` (`balanced`, `meme_comedy`) and `analysis_quality` (`fast`, `enhanced`) to video job options and creator settings.
- In Enhanced, discover a bounded, deduplicated candidate pool and use the existing viewer judge to filter and rank it before rendering.
- Store original and viewer scores, compact explanation, humor metadata, model, token/latency/cost-estimate diagnostics, and explicit rejection/error states.
- Add limited Enhanced Meme & Comedy live paths for semantic, signal-context, and borderline transcript candidates; every automatic clip still requires a successful Viewer Judge result.
- Keep optional visual frames off by default, with a strict 12-frame maximum and graceful unsupported/upstream failure handling.

## Non-goals

- No ASR, renderer, hook/captions/template, HLS/download, NVENC, local-LLM, auto-posting, or provider-default changes.
- No paid re-analysis of existing jobs, no manual labels, no second judge, and no claim that technical integration proves viewer appeal.

## Chosen approach

Extend the existing `brain.find_clips()` and live `Session._ai_check()` contracts with an opt-in selection policy. The primary configured model continues discovery; a per-job/creator Viewer Judge model defaults to `google/gemini-2.5-flash`, independently of the global AI model. The judge call is capped by per-hour candidates (20), a job request cap, token/cost caps, timeouts, cancellation checks, and at most one retry for transient 429/5xx failures.

An unavailable judge does not invent a score or silently report Fast output as Enhanced. The job retains candidates and reports a clear judge failure/fallback state; no enhanced candidate is rendered automatically. Existing records missing these fields normalize to Balanced/Fast.

## UI

The existing add-video options expose profile, quality, judge model, minimum viewer score (initially 75), and experimental visual analysis. Creator setup stores equivalent defaults for live recordings. Job cards show Viewer Score and a short judge explanation/category/key timestamp; detailed fields stay in diagnostics.

## Verification boundary

Tests prove control flow, persistence, quota/dedup/cancellation/failure behavior, and UI/API wiring. A capped live OpenRouter smoke may establish API/model availability and a real local artifact, but does not establish artistic or audience-quality acceptance.
