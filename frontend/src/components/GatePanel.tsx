import { useEffect, useState } from "react";
import type { NexusEvent, Plan, RemediationStep } from "../types";

/** The mandatory human gate (NG-1). Rendered while the pipeline is parked at
 *  `gate_open` with nothing decided yet: the plan in full, and two buttons.
 *  Approving fires the agent's rollback for real (D-10, env-scoped to the
 *  throwaway repo); rejecting ends the cycle cleanly — no tool ever runs. */
export default function GatePanel({
  incidentId,
  events,
  decide,
}: {
  incidentId: string;
  events: NexusEvent[];
  decide: (id: string, d: "approve" | "reject") => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const last = events[events.length - 1];
  const open = last?.type === "gate_open";
  const gate = events.find((e) => e.type === "gate_open");
  const plan = (gate?.plan as Plan | undefined) ?? null;

  // A decision/rollback/done arriving closes the gate — reset local lock.
  useEffect(() => {
    if (last && ["decision", "rollback", "done", "error", "manual_review"].includes(last.type)) {
      setSubmitting(false);
    }
  }, [last]);

  if (!open || !gate) return null;

  const steps: RemediationStep[] = plan?.remediation_steps ?? [];

  return (
    <section className="mt-4 rounded-lg border border-nexus-amber/60 bg-nexus-panel">
      <header className="flex items-center justify-between border-b border-nexus-border px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-nexus-amber" />
          <h2 className="text-sm font-medium text-nexus-text">
            Waiting for your decision — {incidentId}
          </h2>
        </div>
        <span className="rounded border border-nexus-amber/50 px-2 py-0.5 text-[10px] font-semibold text-nexus-amber">
          GATE OPEN
        </span>
      </header>

      <div className="grid gap-4 p-4 md:grid-cols-2">
        <div>
          <h3 className="mb-1 text-[11px] uppercase tracking-widest text-nexus-muted">
            Root-cause hypothesis
          </h3>
          <p className="text-sm text-nexus-text">
            {plan?.root_cause_hypothesis ?? "(none offered)"}
          </p>
          <div className="mt-3 flex gap-4 text-xs">
            <div>
              <span className="text-nexus-muted">Confidence</span>
              <div className="font-mono text-nexus-text">
                {plan?.confidence != null ? `${Math.round(plan.confidence * 100)}%` : "?"}
              </div>
            </div>
            <div>
              <span className="text-nexus-muted">Severity</span>
              <div className="font-mono text-nexus-text">{plan?.severity ?? "?"}</div>
            </div>
            <div>
              <span className="text-nexus-muted">Service</span>
              <div className="font-mono text-nexus-text">{plan?.affected_service ?? "?"}</div>
            </div>
          </div>
        </div>

        <div>
          <h3 className="mb-1 text-[11px] uppercase tracking-widest text-nexus-muted">
            Remediation plan
          </h3>
          {steps.length === 0 ? (
            <p className="text-xs text-nexus-muted">No steps offered.</p>
          ) : (
            <ul className="space-y-1.5">
              {steps.map((s, i) => (
                <li key={i} className="flex items-start gap-2 text-xs">
                  <span
                    className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold ${
                      s.action === "rollback"
                        ? "border-nexus-red/60 text-nexus-red"
                        : "border-nexus-blue/50 text-nexus-blue"
                    }`}
                  >
                    {s.action}
                  </span>
                  <span className="min-w-0 text-nexus-text">
                    {s.target && <code className="font-mono text-nexus-muted">{s.target}</code>}
                    {s.reason && <span className="block truncate text-nexus-muted">{s.reason}</span>}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <footer className="flex items-center justify-between gap-3 border-t border-nexus-border px-4 py-3">
        <p className="text-[11px] text-nexus-muted">
          Approving executes the scoped GitHub rollback for real (throwaway repo).
          Rejecting ends this incident — nothing fires.
        </p>
        <div className="flex gap-2">
          <button
            onClick={() => {
              setSubmitting(true);
              decide(incidentId, "reject");
            }}
            disabled={submitting}
            className="rounded-md border border-nexus-border px-4 py-2 text-xs font-medium text-nexus-text transition-colors hover:bg-nexus-raise disabled:opacity-50"
          >
            Reject
          </button>
          <button
            onClick={() => {
              setSubmitting(true);
              decide(incidentId, "approve");
            }}
            disabled={submitting}
            className="rounded-md bg-nexus-red px-4 py-2 text-xs font-semibold text-nexus-bg transition-colors hover:brightness-110 disabled:opacity-50"
          >
            {submitting ? "Executing…" : "Approve rollback"}
          </button>
        </div>
      </footer>
    </section>
  );
}