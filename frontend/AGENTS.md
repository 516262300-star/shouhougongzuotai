# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

The selected visual source for the new Module 5 return-scrap view is the third displayed ideation option: a model-diagnostics dashboard with company summary metrics, ranked model analysis, cause distribution, trend charts, and a persistent right-side model detail. Preserve that information hierarchy for Module 5 unless the user explicitly chooses a new direction. Existing modules keep their established layouts.

The "人工待办" navigation and the order summary's "待人工" metric must remain clickable. The manual-todo view must expose whether the ERP todo was actually sent, the exact assignee, the business trigger reason, the full message content, remote todo ID, timestamps, retry count, and any failure or cancellation reason in a persistent right-side audit detail.

Build app UI in `src/`. Keep `.openai/hosting.json`, `worker/index.js`, `scripts/prepare-sites-build.mjs`, and `tests/sites-worker.test.mjs` intact so the same local prototype can be handed to Sites. Before a Sites handoff, run `npm run build` and `npm run test:sites`; the build must leave `dist/client/index.html`, `dist/server/index.js`, and `dist/.openai/hosting.json`.
