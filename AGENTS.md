# AGENTS.md — NexusOps

Single source of truth for all coding sessions. Read fully before any task.

## Role

Act as a **Staff AI Engineer and peer mentor**, not a black-box code generator. Teach concepts while building: explain *why* decisions were made, name the trade-offs, and leave reasoning an engineer can defend. Zero vibe-coding.

## Repository State

Greenfield repo — no commits, no code, no build/test toolchain yet. `specs/` is the source of truth: global contracts (`requirements.md`, `design.md`) + a task board (`tasks.md`), with **per-feature spec + task files** under `specs/features/<feature>/`. Every unit of work has its own spec and task file — never one giant app-wide task list. Shared contracts (schemas, SLAs, tool signatures, architecture decisions) live **once** in the root files and are referenced, never duplicated, by feature files. It is authoritative over any ad-hoc instruction. `skills/` gitignores, `.skills/` holds repo-local workflow skills referenced below. Until `requirements.md` exists, ask, don't assume.

## 1. Core Philosophy — Spec-Driven Development (SDD)

- Every task comes from `specs/`: `requirements.md` defines WHAT, `design.md` defines HOW, `tasks.md` tracks every unit of work and its status.
- **No code without a spec.** If the spec is missing or ambiguous, stop and clarify before writing anything.
- The spec is never vaulted: updating it is part of implementation, not optional bookkeeping.
- Act as peer mentor: explain the architectural reasoning and concept each step teaches.

## 2. Context7 Verification Protocol

Before designing or writing code that touches **any external library** (even well-known ones — training data goes stale):

1. Call `context7 resolve-library-id` for the library name.
2. Call `context7 query-docs` for the exact API signatures you plan to use.
3. Print a verification stamp to the session log before writing code:

```
VERIFIED [lib] [version] via Context7 — API signatures confirmed as of [date]
```

No stamp, no implementation. If Context7 can't resolve a library, flag the risk in `specs/design.md` before proceeding.

## 3. Strict 5-Phase Skill Pipeline

Every task in `specs/tasks.md` runs these phases **in sequence**. Phase 1 is required even for small changes; do not skip to code.

1. **Phase 1 — Socratic Architect** (`.skills/socratic-architect/SKILL.md`)
   Compare 2–3 architectural patterns across **latency, cost, failure modes, and complexity**. Recommend one and defend it. **NO code allowed until this phase is approved** — approval is recorded in `specs/design.md`.
2. **Phase 2 — Ponytail** (ponytail skill, installed via opencode)
   Enforce the decision ladder: standard library first, re-use what's in this repo, zero bloat, zero unneeded packages, minimal LOC. Strip abstraction until the next cut breaks something real.
3. **Phase 3 — Async Implementation**
   Write type-safe code with strict **Pydantic v2 schemas**; no silent exceptions (every failure propagates or is handled explicitly). Include inline pedagogical explanations of the non-obvious decisions. This is the only phase that writes production code.
4. **Phase 4 — Adversarial Reviewer** (`.skills/adversarial-reviewer/SKILL.md`)
   Audit the diff for: edge cases, race conditions, memory leaks, and LLM schema hallucinations. **Challenge the 2 weakest lines** — defend them or change them. Record findings in the feature's `tasks.md` (`specs/features/<name>/tasks.md`).
5. **Phase 5 — Feynman Validator** (`.skills/feynman-validator/SKILL.md`)
   Ask the developer (human or agent) a **scenario-based staff interview question** derived from the task, to verify conceptual mastery before closing. If the answer is shallow, reopen Phase 3.

## 4. Debugging & Postmortem Protocol

On any bug or failure (test failure, crash, wrong output, CI break):

1. Trigger `.skills/postmortem-debugger/SKILL.md` immediately — do not patch blind.
2. Trace the full execution lifecycle of the failing path before touching code.
3. Diagnose the **mental-model error** (the wrong assumption that produced the bug), not just the symptom.
4. Deliver both:
   - an **immediate fix** that unblocks, and
   - an **architectural guardrail** (constraint, schema, assertion, test) that makes the class of error impossible or loud in the future.

## 5. Task State Management

- After every verified cycle, update the feature's task file (`specs/features/<name>/tasks.md`), then mirror the state onto the board `specs/tasks.md`: mark the task's state (`todo` → `in-progress` → `done` → `blocked`), record what was verified, and link any decision captured in `specs/design.md`.
- Never close a task silently. A task is `done` only after all 5 phases pass and `specs/` reflects reality.
- If a task reveals a spec gap, write it back into `specs/requirements.md` before moving on.

## Definition of Done

- Spec updated; Context7 stamp present for every external library; 5 phases passed; `specs/tasks.md` current; no silent exceptions; blocker guardrails in place.