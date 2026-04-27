---
date: 2026-04-26
topic: m4-plan-review
focus: review and harden the M4 plan
---

# Ideation: M4 Plan Review

## Codebase Context

ArchMentor is a Python (FastAPI + LiveKit Agent) + TypeScript (Next.js 15) monorepo for an AI-powered live system-design interview mentor. M3 (canvas + session lifecycle) landed 2026-04-25 on `feat/m3-canvas-and-lifecycle`. M4 (streaming brain + sentence-chunked TTS + Haiku summary + content-based phases + counter-argument FSM + cost throttling + frontend cost telemetry) is planned in `docs/plans/2026-04-26-001-feat-m4-streaming-summary-and-cost-controls-plan.md`.

The plan covers ten implementation units across seven phases (grouped as four waves). Pinned dependencies (no bumps): `anthropic==0.96.0`, `livekit-agents==1.5.4`, `streaming-tts==0.3.8`. M3 dogfood findings (master plan §694-697) carry into M4 as call-count throttling + queue-drain prioritisation. The `transcript_window=0` bug is already fixed in commit `ce90164`. `docs/solutions/` is empty (M3 plan flagged the same).

This ideation reviewed the plan against three frames: adversarial / failure modes, scope-and-simplicity cuts, and cross-cutting leverage with M5/M6 + candidate UX. 32 raw candidates generated; adversarial filter merged duplicates and cut weak ideas — 7 survivors.

## Ranked Ideas

### 1. Fingerprint stability across Haiku compaction
**Description:** Unit 1's brain-input fingerprint includes `transcript_window_hash`, but Unit 5's Haiku compactor mutates `transcript_window` (drops oldest N, appends to `session_summary`) on a parallel CAS path. A compaction completing between two otherwise-identical CANVAS_CHANGE dispatches flips the hash even though the brain-relevant context is conceptually unchanged — defeating the cost throttle exactly when it should fire most. Inverse race also exists (compactor enriches summary while transcript shrinks → stale `stay_silent` skip on now-richer prompt). Mitigation: hash a *bucketed* projection — e.g. `(transcript_turn_count + summary_chars_bucketed_to_500, decisions_count, phase, active_argument.topic, event_payload)` — and add a property test asserting compaction-pre vs compaction-post produce identical fingerprints when conceptual context is unchanged.
**Rationale:** The throttle silently degrades to no-op right when the candidate is mid-deep-dive (the moment compaction triggers).
**Downsides:** A bucketed projection is fuzzier than a full hash; the property test is the tripwire that keeps the bucket size honest.
**Confidence:** 90%
**Complexity:** Low
**Status:** Explored — bundled into M4 refinements brainstorm 2026-04-26

### 2. PHASE_TIMER bypasses the cooldown gate
**Description:** Unit 1's exponential-backoff cooldown resets only on TURN_END. PHASE_TIMER fires every 30 s during over-budget phases and exists *to break a stuck silence*. Under the current spec, if `consecutive_stay_silent=4` produces a 32 s cooldown and PHASE_TIMER fires at second 30, the dispatch short-circuits to `skipped_cooldown` — the very nudge designed to break the silence is throttled away. Mitigation: PHASE_TIMER bypasses cooldown but still passes through fingerprint skip (so a duplicate PHASE_TIMER with identical `over_budget_pct` is correctly idempotent).
**Rationale:** Inverts the intended UX — the longer the silence, the more likely the breaking nudge gets eaten.
**Downsides:** None — narrow rule that matches intent; one extra branch in the cooldown gate.
**Confidence:** 95%
**Complexity:** Low
**Status:** Explored — bundled into M4 refinements brainstorm 2026-04-26

### 3. Streaming-cancellation cleanup contract
**Description:** The plan leaves three streaming-cancellation paths under-specified, all of which produce candidate-visible artifacts or replay-fidelity gaps. (a) On `brain_timeout` mid-stream, partial `utterance` audio has already played — R27's "Let me come back to that" then plays *after* the half-sentence, producing audible double-talk. (b) Cancellation between `utterance` complete and `message_stop` produces a snapshot with no `BrainDecision` — replay can't reconstruct what the candidate heard. (c) Schema-violation after partial TTS leaves a half-sentence with no semantic close; the next dispatch may steelman an unrelated point. Mitigation: a single "streaming cancellation cleanup" subsection in Unit 3/4 specifying — (i) on `TimeoutError`/cancel, force `tts_stream.cancel()` + flush before any synthetic recovery; (ii) suppress R27 if any partial audio played (set `_apology_used=True` regardless); (iii) on cancel post-utterance pre-`message_stop`, synthesize a `BrainDecision.partial(utterance=accumulated, reason="cancelled_mid_stream")` whose snapshot row carries enough to replay; (iv) on schema-violation with partial TTS, push a single canned closing token ("— let me restate that.") through the same `tts_stream` before `end_input`, ledger as `reason=schema_violation_partial_recovery`.
**Rationale:** Streaming creates four new "what does the candidate hear vs what's recorded" gaps; bundling them into one cleanup contract avoids implementation drift.
**Downsides:** Adds ~30 lines to Unit 3/4 and 3-4 specific test scenarios.
**Confidence:** 85%
**Complexity:** Medium
**Status:** Explored — bundled into M4 refinements brainstorm 2026-04-26

### 4. `SessionTelemetry` aggregator + session-end summary row
**Description:** Build a `SessionTelemetry` dataclass on `MentorAgent` that maintains running counters: TTFA histogram (p50/p95), `brain_calls_made`, `skipped_idempotent_count`, `skipped_cooldown_count`, `dropped_stale_count`, `compactions_run`, `compactions_failed`, `phase_nudges_fired`, `argument_rounds_max`, `interruptions_made`, per-phase actual durations. On session end, serialise to a single row — either a JSON column on `sessions.telemetry_json` (one Alembic add) or a `SESSION_TELEMETRY` ledger event. Subsumes the standalone "phase_started_at_ms / phase_durations_s_actual" idea and the per-dispatch TTFA telemetry idea — single structure feeds them all.
**Rationale:** M5's report builder reads one row instead of map-reducing the entire ledger. M6's eval-harness gets a stable cross-session schema. M4's dogfood gate has a single canonical source of truth (instead of grepping logs). Cheapest M5/M6 unblock the plan can ship inside M4.
**Downsides:** Adds one more thing to drain on shutdown; ~80 lines of code; one Alembic add.
**Confidence:** 90%
**Complexity:** Low-Medium
**Status:** Explored — bundled into M4 refinements brainstorm 2026-04-26

### 5. `prompt_version` hash on every BrainSnapshot now
**Description:** Compute SHA-256 over `system.md` + tool schema + problem-card body at agent boot, surface as `Settings.prompt_version`, and write into every `BrainSnapshot` (and `summary_compressed` payload) as a single column or payload field. The plan currently defers per-snapshot prompt versioning to M6, but Unit 7 rewrites `[Phase awareness]`, Unit 8 rewrites `[Counter-argument]`, Unit 6 already touches snapshots — the hash is ~5 lines with negligible schema cost.
**Rationale:** M6's ghost-diff is the eval-harness premise; a stable `prompt_version` is the join key that makes A/B comparison possible at all. Without it, replays spanning M3/M4/M5 boundaries can't tell whether divergent decisions came from a prompt edit or a code edit.
**Downsides:** One Alembic add (single nullable column on `brain_snapshots`); no behavioural change.
**Confidence:** 95%
**Complexity:** Low
**Status:** Explored — bundled into M4 refinements brainstorm 2026-04-26

### 6. Cut Unit 9 (frontend cost indicator) entirely
**Description:** R24/R25 add a `publishData` topic + frontend `<CostBudgetIndicator>` to surface cost-burn. M4's three pain points are latency, cost, dropped utterances — none of the M3 dogfood findings mention candidate cost-visibility as a complaint. The actual operator who needs cost visibility during dogfooding has Postgres + log access. The component would distract candidates mid-design (the plan even collapses it below 50% to avoid that). Cut both the agent-side `_publish_telemetry` topic and the frontend component; defer to M5/M6 polish if a real need surfaces. The `cost_usd_total` is already on `BrainSnapshot` and (per survivor #4) on the new `SessionTelemetry` row — nothing is lost.
**Rationale:** Unobserved need; frontend work that doesn't materially improve any M4 pain point. CLAUDE.md "no speculative features."
**Downsides:** Operator dogfooding needs to read the agent log or query Postgres for cost; minor friction.
**Confidence:** 80%
**Complexity:** Negative (removes work)
**Status:** Explored — bundled into M4 refinements brainstorm 2026-04-26

### 7. YAGNI cuts bundle: drop new Settings, drop `"keep"` sentinel, drop `docs/solutions/*` deliverables
**Description:** Three small cuts that strengthen the plan without changing its shape: (a) inline `summary_compaction_threshold=30` and `haiku_model` as module constants in `haiku_client.py` rather than new `Settings` fields — neither has an operator-tuning consumer in M4 and `Settings` fields are forever (`.env.example` + placeholder-rejector + test fixtures + rollout matrix). (b) Cut the `"keep"` schema sentinel on `new_active_argument` — the union `object | null | "keep"` is itself a schema-violation surface; revert to `object | null` and use the rule "absent or null = no change unless prior is stale" — replay snapshots from M2/M3 stay deterministic and the brain has fewer ways to fail tool-use. (c) Drop the four `docs/solutions/*.md` writeup deliverables — none are load-bearing for M4 acceptance, M3 already noted the gap and didn't fill it, and pre-implementation prose drifts the moment the implementation diverges. Write them post-dogfood against actual code if a lesson hardens.
**Rationale:** Three small "no premature abstraction" cuts that match global standards and reduce maintenance surface.
**Downsides:** (b) raises a small risk that the brain transiently drops the key — but the auto-clear-stale path already covers that (3 min, rounds=0 → cleared). The plan's own backwards-compat shim "missing key = keep" already neutralises the absence-vs-null ambiguity, making the schema sentinel redundant.
**Confidence:** 85%
**Complexity:** Negative (removes work)
**Status:** Explored — bundled into M4 refinements brainstorm 2026-04-26

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | A11: publishData cadence misses Haiku cost | Plan already says emit telemetry from `_run_compaction` after CAS apply — duplicate |
| 2 | A8: `_summary_in_flight` orphan on Haiku 5xx | Mitigation is a 3-line `try/finally` the plan's discipline already implies |
| 3 | A6: phase-timer survives shutdown | Real but narrow; the plan's "task cancel + drain" pattern handles it |
| 4 | A3: queue mutation race during cancelled brain task | Real but narrow; survivors #3 and #4 cover the surrounding surface |
| 5 | A9: SentenceTokenizer flush on cancellation | Subsumed by survivor #3's cleanup contract |
| 6 | S3: Drop `--cost-throttle-stats` replay flag | Low-stakes; minor convenience flag |
| 7 | S4: Collapse Unit 6 (use snapshot discriminator for SUMMARY_COMPRESSED) | Compaction is genuinely a distinct model + cost from BRAIN_DECISION; the analogy to PHASE_NUDGE doesn't hold |
| 8 | S7: Replace TTL bump with bigger fixed TTL | Static TTL doesn't bump for items queued *during* a long brain call — strictly worse than R23 |
| 9 | S8: Cut CLAUDE.md edit pre-dogfood | Low marginal value; CLAUDE.md edits are fast post-merge |
| 10 | S9: Resolve open questions or remove them | Some open questions genuinely depend on dogfood numbers; deferring is correct |
| 11 | S10: Drop "Frontend telemetry payload growth" risk row | Evaporates if survivor #6 (cut Unit 9) is accepted |
| 12 | S11: Move `PHASE_SOFT_BUDGETS_S` out of `session_state.py` | Placement disagreement is too low-stakes |
| 13 | C2: Persist `phase_started_at_ms` + `phase_durations_s_actual` | Subsumed by survivor #4's `SessionTelemetry` |
| 14 | C3: New `streaming_first_sentence` AiSpeakingState | Marginal benefit — the audible win arrives ~500ms later anyway; UX polish |
| 15 | C4: TTFA in `ai_telemetry` | Subsumed by survivor #4 |
| 16 | C5: Counter-argument round count in UI pill | Tension with survivor #6 (cut Unit 9); speculative UX |
| 17 | C7: Per-sentence `tts_sentence_chunk` ledger events | Too noisy; ledger volume cost outweighs replay value |
| 18 | C8: `m4_safety_policies.py` module | Premature abstraction — concentrating constants before any has been tuned even once |
| 19 | C9: Persist raw streaming partial high-water mark | Specific to a rare failure mode; survivor #3 covers the partial-decision snapshot for the common cancel path |
| 20 | C10: `last_dispatch_seq` resume cursor for M6 | Speculative for M6; reconnection design hasn't started |
| 21 | A5: `"keep"` sentinel breaks replay | Merged into survivor #7's YAGNI bundle (cut the sentinel) |
| 22 | A1: Schema-violation rollback recovery sentence | Merged into survivor #3 |
| 23 | A4: R27 + streaming → doubled audio | Merged into survivor #3 |
| 24 | A10: Empty `state_updates` snapshot on cancel | Merged into survivor #3 |

## Session Log

- 2026-04-26: Initial ideation — 32 candidates generated across 3 frames (adversarial, scope, cross-cutting), 7 survivors after dedupe + adversarial filter
- 2026-04-26: All 7 survivors bundled into a single M4-refinements brainstorm (M3 precedent) — see `docs/brainstorms/2026-04-26-m4-plan-refinements-requirements.md`
