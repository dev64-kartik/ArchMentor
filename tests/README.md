# tests/

Cross-app test assets.

- `eval-harness/` — replay recorded sessions through new prompts; diff
  interrupt decisions side-by-side with reasoning (ghost diff). Lands in M6.
- `fixtures/` — shared fixtures: sample session JSONL, Excalidraw scenes,
  transcript snippets.

Per-app unit tests live alongside their source:

- `apps/api/tests/`
- `apps/agent/tests/`
- `apps/web/**/*.test.ts` (colocated)
