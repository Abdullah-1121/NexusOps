# Feature Spec — 6. Demonstration Film (Replay Player)

System context: `specs/requirements.md` FR-9, §5.2 (tool contracts), §5.4 (approval contract); `specs/design.md` D-10.

## Phase 1 decision — **APPROVED** (2026-09-23)
**Pattern B: checkpoint-derived player.** The player reads a recorded benchmark run (`outG1.json` shape) and derives a per-incident beat timeline — *alert → severity → evidence → plan → gate → decision → rollback → terminal* — because the state-machine graph defines the stage order deterministically and the checkpoint records the fields that fill each beat. Rejected Pattern A (live re-run during the demo: real quota burn or canned outputs with fakes, non-deterministic, live failure risk). Rejected a new frame-recorder in the pipeline (a change to the benchmark machinery for a pure-presentation need — violates Ponytail; the checkpoint already carries ~80% of each beat).

- **Live approval gate (the thesis moment):** the film pauses at the gate beat and the operator approves/rejects live via the §5.4 decision contract (`approve_rollback` + WS/POST message). Human-in-the-loop (NG-1) is *performed*, not replayed.
- **Real scoped rollback:** on an approved rollback-worthy incident, `trigger_github_rollback` executes its real backend — a GitHub REST call creating a rollback **tag** (`refs/tags/rollback-<incident>-<ts>`) on the env-configured throwaway repo, then verifies via `GET .../git/ref/tags/<tag>`. Stdlib `urllib` only (zero new deps). Gate invariant unchanged: no approval ⇒ `rejected`, no API call.
- **Honesty rules:** recorded outages render as `manual_review` with the recorded reason (never dressed up as resolved); the target repo is labeled on screen; the GitHub token lives in `.env` only, never logged or rendered.

## What this feature does (in scope)
- A player views over the recorded checkpoint (`outG1.json`): per-incident beat timeline, play/pause/step, per-beat detail (severity, plan text, decision, gate timing, terminal state, recorded reason on manual review).
- A **live gate interaction**: at the gate beat the operator approves or rejects through the same decision contract as production; an approved rollback executes the real scoped GitHub tag rollback (D-10) and the result (tag SHA, verified) is shown.
- Honest rendering of the recorded run's terminal states, including the 2 outages (`manual_review` with recorded 503 reason) and the 1 rejection.
- Env-gated GitHub rollback configuration: `NEXUSOPS_GITHUB_REPO`, `NEXUSOPS_GITHUB_TOKEN`, optional `NEXUSOPS_GITHUB_LAST_GOOD_SHA` — missing config degrades loudly (never a silent "performed").

## Out of scope (roadmap toward B — recorded, not built now)
- Real alert ingestion from live sources (Prometheus/PagerDuty).
- Real evidence queries against live systems.
- Revert *commit* rollback (real code change) — heavier; tag-based marker is the v1 rollback signal.
- Any new dependency; any change to the benchmark/report pipeline.

## Acceptance
- Loading the checkpoint renders the full beat sequence for all 10 recorded incidents; `m-02` shows `rejected`, `c-01`/`a-01` show `manual_review` with the recorded `FF RCA unavailable` reason.
- Play/pause/step controls work with no model invocation (no token use, deterministic).
- Approving the gate on `w-01`/`i-01` (resolved-with-rollback incidents) creates a verifiable `rollback-*` tag on the configured throwaway repo (API-verified), and the UI shows the tag SHA.
- Rejecting at the gate returns `rejected` with no API call (trace/log asserts no refs call).
- Missing GitHub config ⇒ loud `manual_review`-style error on the live gate, never a silent "performed (mock)".