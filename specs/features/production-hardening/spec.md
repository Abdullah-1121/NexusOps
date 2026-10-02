# Feature Spec — 8. Production Hardening (D-18 closure, step 1: supervision)

System context: `specs/requirements.md` NFR-6 (Redis must be up); `specs/design.md` D-18 (scorecard + closure plan), D-19 (approved supervision design), R-5 (this risk).

## Phase 1 decision — **APPROVED** (2026-10-02)
**Docker Compose (Redis + console) + `init: true` (tini) + in-container health guard `scripts/run_guard.py`** — the full comparison is recorded in `specs/design.md` D-19. The one-line summary: Docker owns crash-restart + logs; tini owns signal plumbing; the guard owns the honest "is it *serving*?" signal. A supervisor process is the only supervisor — the guard never restarts in-process, and we never run more than one console worker (in-process queue consumer).

## What this feature does (in scope)
- `scripts/run_guard.py` — stdlib-only lifeguard: spawns the console as a child (all CLI args passed through verbatim), probes `GET /api/status` (3 s timeout) each tick, counts **timeouts only** (any HTTP response = healthy, including truthful `redis: unreachable`), and after N consecutive misses escalates SIGTERM → 10 s wait → SIGKILL. Mirrors the child's exit code and exits; forwards SIGTERM/SIGINT (graceful-first, brutal-backstop, per D-11 postmortem #2).
- `--host` flag on `scripts/run_console.py` (default `127.0.0.1` — current behavior unchanged; containers pass `0.0.0.0`). The only application change.
- `Dockerfile` (multi-stage: `node:22-alpine` builds `frontend/dist` via `npm ci`; `python:3.12-slim` runs the app) + `.dockerignore` (secrets excluded) + `docker-compose.yml` (`redis:7-alpine` private to the network + console with `restart: unless-stopped`, `init: true`, `env_file: .env`, `/api/status` healthcheck, `--keep-redis`).
- Guard behavior pinned by pytest against a fake console (crash / clean stop / wedge / SIGTERM-ignoring child).

## Out of scope (recorded)
- Durable incident ledger (future D-17), auth, CI, telemetry sink, SLOs — closure items 2–6, each its own task under this feature or a later spec.
- Publishing the image to a registry; multi-machine deployment; systemd production unit (the guard is the portable pattern; the unit file is a later deployment artifact).
- Changing the bare-metal dev loop (benchmark/seed/console on localhost): containers are the deployment artifact; the local stack stays as-is.

## Acceptance
- `pytest` guard suite passes: crash → exit mirrored; voluntary clean exit → exit mirrored; wedge → SIGTERM → SIGKILL, guard exits nonzero; SIGTERM-ignoring child → SIGKILL (escalation proven); SIGTERM to the guard → forwarded, child drains, guard exits 0.
- Full existing suite still green (104+ tests).
- `docker compose up -d`: both services report healthy (`docker compose ps`), `:8137` serves from the published port, `/api/status` responds.
- Live proof: `docker compose kill console` → the container restarts on its own and `/api/status` returns healthy again (unmonitored-downtime class closed).
- `.env` absent from image layers (`docker history` shows no `.env` layer).