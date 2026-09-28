import type { NexusEvent } from "../types";
import { useNow } from "../useNow";

/* ------------------------------------------------------------------ */
/* Live pipeline stepper (T-7.10, 2026-09-28).                        */
/*                                                                     */
/* The operator's "where is it right now" — a fixed canonical pipeline  */
/* (Ingest → Classify → Evidence → Plan → Gate → Resolve) with the     */
/* current stage pulsing and a live elapsed ticker (client-observed    */
/* wall-clock via event `_arrived_ms`, so "waiting 42s" is what the    */
/* operator actually saw). Completed stages carry their honest server  */
/* `stage_duration_ms`; a failed/aborted run marks the offending step  */
/* red and dashes the rest.                                            */
/* ------------------------------------------------------------------ */

type StepKey = "ingest" | "classify" | "evidence" | "plan" | "gate" | "resolve";

const STEPS: { key: StepKey; label: string }[] = [
  { key: "ingest", label: "Ingest" },
  { key: "classify", label: "Classify" },
  { key: "evidence", label: "Evidence" },
  { key: "plan", label: "Plan" },
  { key: "gate", label: "Gate" },
  { key: "resolve", label: "Resolve" },
];

type StepState = "done" | "active" | "pending" | "failed" | "skipped";

const ACTIVE_LABEL: Record<StepKey, string> = {
  ingest: "receiving alert…",
  classify: "classifying…",
  evidence: "calling fetch_service_logs · query_prometheus_metrics…",
  plan: "writing remediation plan…",
  gate: "awaiting operator…",
  resolve: "executing…",
};

function fmtElapsed(ms: number): string {
  if (!isFinite(ms) || ms < 0) return "—";
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

function fmtMs(ms: unknown): string | null {
  return typeof ms === "number" ? `${Math.round(ms)}ms` : null;
}

export default function PipelineProgress({ events }: { events: NexusEvent[] }) {
  const now = useNow();
  if (events.length === 0) return null;

  const has = (t: string) => events.some((e) => e.type === t);
  const first = (t: string) => events.find((e) => e.type === t);
  const last = (t: string) => {
    let out: NexusEvent | undefined;
    for (const e of events) if (e.type === t) out = e;
    return out;
  };
  const arrived = (e: NexusEvent | undefined) =>
    typeof e?._arrived_ms === "number" ? (e._arrived_ms as number) : Date.now();

  const mrStage = first("manual_review")?.stage as string | undefined;
  const error = has("error");
  const doneEv = has("done") ? last("done") : undefined;
  const terminalType = doneEv?.terminal as string | undefined;

  // failedAt: the canonical step where the pipeline aborted (manual_review
  // names its stage; a generic error lands on plan).
  let failedAt: StepKey | null = null;
  if (mrStage === "classify") failedAt = "classify";
  else if (mrStage === "rca") failedAt = "plan";
  else if (error) failedAt = "plan";

  const done: Record<StepKey, boolean> = {
    ingest: has("ingest"),
    classify: has("classified"),
    evidence: has("tool_call"),
    plan: has("plan"),
    // T-7.10 P4: a step is only *done* when it really happened. The gate is
    // done iff an operator produced a `decision` (§5.4 publishes it; nothing
    // else can) — an aborted run (manual_review/error) never reached a gate,
    // so it must render skipped/dashed, never a green check. Resolve is done
    // only on a *real* terminal: `done` events are also published for
    // manual_review aborts, and those must not claim an execution.
    gate: has("decision"),
    resolve: !!doneEv && terminalType !== "manual_review",
  };
  // failed/skipped steps still count as "resolved" rows for sequencing.
  if (failedAt) done[failedAt] = true;

  const state: Record<StepKey, StepState> = {
    ingest: "pending",
    classify: "pending",
    evidence: "pending",
    plan: "pending",
    gate: "pending",
    resolve: "pending",
  };
  let activeKey: StepKey | null = null;

  for (const s of STEPS) {
    if (s.key === failedAt) {
      state[s.key] = "failed";
      activeKey = null;
      continue;
    }
    if (done[s.key]) {
      state[s.key] = "done";
      continue;
    }
    if (failedAt && STEPS.findIndex((x) => x.key === s.key) > STEPS.findIndex((x) => x.key === failedAt)) {
      state[s.key] = "skipped";
      continue;
    }
    if (!activeKey) {
      state[s.key] = "active";
      activeKey = s.key;
    }
  }
  if (has("gate_open") && !done.gate) {
    state.gate = "active";
    activeKey = "gate"; // awaiting operator overrides the generic active
    state.plan = "done";
  }

  // Live elapsed for the active step: now minus when that step began.
  const startAt: Record<StepKey, number> = {
    ingest: arrived(first("ingest")),
    classify: arrived(first("ingest")),
    evidence: arrived(first("classified")),
    plan: arrived(first("tool_call")),
    gate: arrived(first("gate_open")),
    resolve: arrived(first("decision")),
  };
  const activeMs = activeKey ? now - startAt[activeKey] : 0;

  const stepMs = (k: StepKey): string | null => {
    if (k === "ingest") return null;
    if (k === "classify") return fmtMs(last("classified")?.stage_duration_ms);
    if (k === "evidence") return fmtMs(last("tool_call")?.stage_duration_ms);
    if (k === "plan") return fmtMs(last("plan")?.stage_duration_ms);
    if (k === "gate") {
      const w = last("decision")?.gate_wait_ms;
      if (typeof w === "number") return `${Math.round(w)}ms`;
      return null;
    }
    if (k === "resolve") return fmtMs(last("done")?.t_resolve_ms);
    return null;
  };

  return (
    <section className="overflow-hidden rounded-lg border border-nexus-border bg-nexus-panel shadow-[0_1px_2px_rgba(16,24,40,0.05)]">
      <header className="flex items-center justify-between gap-3 border-b border-nexus-border px-4 py-2.5">
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-nexus-muted">
          Pipeline
        </h2>
        {activeKey ? (
          <span className="flex items-center gap-2 text-[11.5px] text-nexus-muted">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-nexus-accent" />
            <span>{ACTIVE_LABEL[activeKey]}</span>
            <span className="font-mono text-[11px] font-semibold text-nexus-text">
              {fmtElapsed(activeMs)}
            </span>
          </span>
        ) : null}
      </header>

      <ol className="flex items-start px-4 py-3.5">
        {STEPS.map((s, i) => {
          const st = state[s.key];
          const isLast = i === STEPS.length - 1;
          return (
            <li key={s.key} className={`flex flex-1 items-start ${isLast ? "max-w-[110px]" : ""}`}>
              <div className="flex w-full flex-col items-center">
                <div className="flex w-full items-center">
                  <div className={`h-0.5 flex-1 ${i === 0 ? "invisible" : st === "pending" || st === "skipped" ? "bg-nexus-border" : "bg-nexus-green/50"}`} />
                  <Node state={st} />
                  <div className={`h-0.5 flex-1 ${isLast ? "invisible" : st === "done" || st === "active" ? "bg-nexus-green/50" : "bg-nexus-border"}`} />
                </div>
                <span
                  className={`mt-1.5 text-[10.5px] font-medium ${
                    st === "done"
                      ? "text-nexus-green"
                      : st === "active"
                        ? "text-nexus-accent"
                        : st === "failed"
                          ? "text-nexus-red"
                          : st === "skipped"
                            ? "text-nexus-faint"
                            : "text-nexus-faint"
                  }`}
                >
                  {s.label}
                </span>
                <span
                  className={`mt-0.5 font-mono text-[9.5px] ${
                    st === "active" ? "text-nexus-accent" : "text-nexus-faint"
                  }`}
                >
                  {st === "active"
                    ? fmtElapsed(activeMs)
                    : st === "done"
                      ? stepMs(s.key) ?? "✓"
                      : st === "failed"
                        ? "aborted"
                        : st === "skipped"
                          ? "—"
                          : "…"}
                </span>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function Node({ state }: { state: StepState }) {
  if (state === "done") {
    return (
      <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-nexus-green text-[11px] font-bold text-white">
        ✓
      </span>
    );
  }
  if (state === "active") {
    return (
      <span className="relative grid h-6 w-6 shrink-0 place-items-center">
        <span className="absolute inset-0 animate-ping rounded-full bg-nexus-accent/30" />
        <span className="h-4 w-4 rounded-full bg-nexus-accent" />
      </span>
    );
  }
  if (state === "failed") {
    return (
      <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-nexus-red text-[12px] font-bold text-white">
        !
      </span>
    );
  }
  if (state === "skipped") {
    return (
      <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full border border-dashed border-nexus-faint text-[11px] text-nexus-faint">
        –
      </span>
    );
  }
  return (
    <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full border-2 border-nexus-border bg-nexus-panel" />
  );
}