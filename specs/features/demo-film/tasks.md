# Tasks — 6. Demonstration Film

Phase gates per AGENTS.md: 1 socratic-architect → 2 ponytail → 3 implementation → 4 adversarial-reviewer → 5 feynman. Task state: **todo → in-progress → done → blocked**. Update the board (`specs/tasks.md`) after each verified cycle.

| # | Task | Phases 1–5 | Verified |
|---|---|---|---|
| 6.1 | Checkpoint reader: derive per-incident beat timeline from `outG1.json` shape | 1–4 ✅ | `app/player.py derive_beats` — honest beats from recorded fields; outage renders as FAILED+reason, never fabricated; verified live against real `outG1.json` |
| 6.2 | Real scoped GitHub rollback in `trigger_github_rollback` (tag create + verify, stdlib, env-gated, gate-first) | 1–4 ✅ | `app/rollback.py perform_github_rollback`; gate check stays before any API call (NG-1, tested); loud RollbackError→ToolError; `_request` seam patched in tests |
| 6.3 | Player UI: play/pause/step, beat detail, honest terminal states | 1–4 ✅ | `PLAYER_HTML` + `/api/films` + `/api/film/{id}`; browser-verified c-02 (7 beats, live gate) and c-01 (5 beats, honest outage, no gate) |
| 6.4 | Live gate interaction on the film (approve/reject via §5.4; approved → real rollback) | 1–4 ✅ | `POST /api/film/{id}/decision` drives the SAME production chain (`approve_rollback` → `trigger_github_rollback`); reject never calls the API; missing env → loud 502; 76 tests pass |
| 6.5 | Board + requirements mirror (`specs/tasks.md`, FR-9, NG-2/NG-4, §5.2) | ✅ | see below |

## Phase 1 decision (2026-09-23) — Pattern B
Checkpoint-derived player over the recorded run; live approval gate; real-but-scoped GitHub tag rollback with stdlib `urllib`. Rationale + trade-offs in `spec.md` and `specs/design.md` D-10.

## Context7 stamps
- `VERIFIED GitHub REST git-refs API — POST /repos/{owner}/{repo}/git/refs {"ref": "refs/tags/<name>", "sha": "<commit-sha>"} creates a tag; GET /repos/{owner}/{repo}/git/ref/tags/<name> verifies (404 if absent)` (docs.github.com REST, 2026-09-23). No new Python library.

## Phase 4 findings
1. **Film approve bypassed the production gate chain — fixed (architectural).** First cut called `perform_github_rollback` directly from the player, creating a *second* approved-registry split. Rewired: the film's live Approve drives the SAME chain as production — `approve_rollback()` records the human approval (NG-1), then `trigger_github_rollback()` executes. Test asserts `demo-c-02` lands in `_APPROVED`.
2. **Fixed tag name → 422 on second demo — fixed.** First cut pinned `rollback-{incident}-live`; replaying the same incident twice hit GitHub's "reference already exists". Routing through the production tool inherits its timestamped tag names (`rollback-<sha10>-<ts>`), making every execution unique.
3. **Player JS bug (browser-only, tests couldn't catch): `$()` is `getElementById` but was handed a CSS selector** — `render()` crashed at beat 2 in a real browser. Fixed with `document.querySelector`. Lesson: the film needed a real-browser pass, not just TestClient.
4. **Honest-outage invariant:** `c-01`/`a-01` (no plan) render FAILED + recorded 503 reason and expose NO live gate (409 on decision POST) — you cannot approve absent a plan.
5. **Accepted:** evidence-bead is a stage marker ("tool calls live in the OTel trace, not the checkpoint") — fabricating per-call args would have violated the honesty rule.

## Secrets hygiene
- `NEXUSOPS_GITHUB_TOKEN`: gitignored `.env` only; fine-grained PAT scoped to the single throwaway repo (`Contents: Read and write`); never logged, never rendered in the UI. The player labels the target repo on screen.

## Setup
No new dependency — stdlib `urllib` for the GitHub REST call; existing FastAPI/WS surface for the player page.