import { useEffect, useState } from "react";
import type { NexusEvent, Plan, RemediationStep } from "../types";

/** The mandatory human gate (NG-1). Rendered while the pipeline is parked at
 *  `gate_open` with nothing decided yet: the plan in full, and two buttons.
 *  Approving fires the agent's rollback for real (D-10, env-scoped to the
 *  throwaway repo); rejecting ends the cycle cleanly — no tool ever runs.
 *
 *  Batch C (2026-09-28): the decision must FEEL live. The instant a button is
 *  clicked we render an optimistic "decision recorded" confirmation — the
 *  operator never stares at a dead button while the pipeline resumes. The card
 *  stays mounted (confirming every decision was received) until a terminal
 *  state arrives; only then does it give way to the timeline's terminal row. */
export default function GatePanel({
  incidentId,
  events,
  decide,
}: {
  incidentId: string;
  events: NexusEvent[];
  decide: (id: string, d: "approve" | "reject", reason?: string) => void;
}) {
  const [submitting, setSubmitting] = useState<"approve" | "reject" | null>(null);
  const [reason, setReason] = useState("");
  const last = events[events.length - 1];
  const open = last?.type === "gate_open";
  const gate = events.find((e) => e.type === "gate_open");
  const plan = (gate?.plan as Plan | undefined) ?? null;

  // Optimistic sent-marker folds in the durable `decision` event: even if this
  // component unmounted between click and stream, the decision row proves it
  // landed — the confirmation is never re-fabricated from local state alone.
  const decided = last?.type === "decision" ? String(last.decision) : submitting;

  // A terminal arriving closes the gate — reset local lock.
  useEffect(() => {
    if (last && ["done", "error", "manual_review"].includes(last.type)) {
      setSubmitting(null);
    }
  }, [last]);

  if (!gate) return null;

  const steps: RemediationStep[] = plan?.remediation_steps ?? [];
  const conf =
    typeof plan?.confidence === "number" ? Math.round(plan.confidence * 100) : null;

  const onDecide = (d: "approve" | "reject") => {
    setSubmitting(d);
    decide(incidentId, d, reason || undefined);
  };

  /* After the click and through the pipeline tail, show the confirmation —
   * the machine heard the human, and the run is finishing. */
  if (decided && !open) {
    const approve = decided === "approve";
    return (
      <section className="mt-4 overflow-hidden rounded-lg border border-nexus-border bg-nexus-panel shadow-[0_1px_2px_rgba(16,24,40,0.05)]">
        <div className="flex items-center gap-3 px-4 py-3">
          {approve ? (
            <span className="h-2 w-2 animate-pulse rounded-full bg-nexus-green" />
          ) : (
            <span className="h-2 w-2 rounded-full bg-nexus-amber" />
          )}
          <div className="min-w-0 flex-1">
            <p className="text-[13px] font-semibold text-nexus-text">
              Decision recorded — {approve ? "approve" : "reject"}
            </p>
            <p className="mt-0.5 text-[12px] text-nexus-muted">
              {approve
                ? "Approved. The scoped rollback is executing — watch the rollback stage stream in."
                : "Rejected. No action was taken and no tool ran — this incident is closing."}
            </p>
          </div>
          <span
            className={`shrink-0 rounded border px-2 py-0.5 text-[10px] font-bold tracking-wide ${
              approve
                ? "border-nexus-green/40 bg-nexus-green/10 text-nexus-green"
                : "border-nexus-amber/40 bg-nexus-amber/10 text-nexus-amber"
            }`}
          >
            {approve ? "EXECUTING" : "CLOSING"}
          </span>
        </div>
      </section>
    );
  }

  if (!open) return null;

  return (
    <section className="mt-4 overflow-hidden rounded-lg border border-nexus-amber/40 bg-nexus-panel shadow-[0_1px_2px_rgba(16,24,40,0.05)]">
      <header className="flex items-center justify-between gap-3 border-b border-nexus-border px-4 py-3">
        <div className="flex items-center gap-2.5">
          <span className="h-2 w-2 animate-pulse rounded-full bg-nexus-amber" />
          <h2 className="text-[13px] font-semibold text-nexus-text">
            Action required — {incidentId}
          </h2>
        </div>
        <span className="rounded border border-nexus-amber/40 bg-nexus-amber/10 px-2 py-0.5 text-[10px] font-bold tracking-wide text-nexus-amber">
          GATE OPEN
        </span>
      </header>

      <div className="grid gap-5 p-4 md:grid-cols-2">
        <div>
          <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-nexus-faint">
            Root-cause hypothesis
          </h3>
          <p className="text-[13.5px] font-medium leading-snug text-nexus-text">
            {plan?.root_cause_hypothesis ?? "(none offered)"}
          </p>
          {plan?.reasoning && (
            <div className="mt-3">
              <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-nexus-faint">
                Why this plan
              </h3>
              <p className="text-[12.5px] leading-relaxed text-nexus-muted">
                {plan.reasoning}
              </p>
            </div>
          )}
          <div className="mt-3 space-y-1">
            <div className="flex items-baseline justify-between gap-3 text-[11px]">
              <span className="font-medium uppercase tracking-wide text-nexus-faint">
                Confidence
              </span>
              <span className="font-mono text-[12px] font-semibold text-nexus-text">
                {conf != null ? `${conf}%` : "?"}
              </span>
            </div>
            {conf != null && (
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-nexus-raise">
                <div
                  className="h-full rounded-full bg-nexus-accent"
                  style={{ width: `${conf}%` }}
                />
              </div>
            )}
            <div className="flex items-baseline justify-between gap-3 pt-1 text-[11px]">
              <span className="font-medium uppercase tracking-wide text-nexus-faint">
                Severity
              </span>
              <span className="font-mono text-[12px] text-nexus-text">
                {plan?.severity ?? "?"}
              </span>
            </div>
            <div className="flex items-baseline justify-between gap-3 text-[11px]">
              <span className="font-medium uppercase tracking-wide text-nexus-faint">
                Service
              </span>
              <span className="font-mono text-[12px] text-nexus-text">
                {plan?.affected_service ?? "?"}
              </span>
            </div>
          </div>
        </div>

        <div>
          <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-nexus-faint">
            Remediation plan ({steps.length})
          </h3>
          {steps.length === 0 ? (
            <p className="text-[12.5px] text-nexus-muted">No steps offered.</p>
          ) : (
            <ul className="space-y-2">
              {steps.map((s, i) => (
                <li key={i} className="flex items-start gap-2.5">
                  <span
                    className={`rounded border px-1.5 py-0.5 font-mono text-[10.5px] font-semibold ${
                      s.action === "rollback"
                        ? "border-nexus-red/40 bg-nexus-red/10 text-nexus-red"
                        : "border-nexus-blue/40 bg-nexus-blue/10 text-nexus-blue"
                    }`}
                  >
                    {s.action}
                  </span>
                  <span className="min-w-0 text-[12.5px] leading-snug text-nexus-text">
                    {s.target && (
                      <code className="font-mono text-[11.5px] text-nexus-muted">
                        {s.target}
                      </code>
                    )}
                    {s.reason && (
                      <span className="block text-nexus-muted">{s.reason}</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <footer className="flex flex-col gap-2.5 border-t border-nexus-border bg-nexus-raise/40 px-4 py-3">
        <p className="text-[11.5px] leading-snug text-nexus-muted">
          Approving executes the scoped rollback for real — nothing is faked.
          Rejecting ends this incident: <span className="font-medium text-nexus-text">no action is taken.</span>
        </p>
        <label className="flex flex-col gap-1">
          <span className="text-[10.5px] font-medium uppercase tracking-wide text-nexus-faint">
            Why this decision (optional — recorded with the gate)
          </span>
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={submitting !== null}
            placeholder="e.g. matches the deploy window; authorizing rollback"
            className="rounded-md border border-nexus-border bg-nexus-panel px-3 py-1.5 text-[12.5px] text-nexus-text placeholder:text-nexus-faint focus:border-nexus-accent focus:outline-none disabled:opacity-50"
          />
        </label>
        <div className="flex shrink-0 justify-end gap-2">
          <button
            onClick={() => onDecide("reject")}
            disabled={submitting !== null}
            className="rounded-md border border-nexus-border bg-nexus-panel px-4 py-1.5 text-[12px] font-semibold text-nexus-text transition-colors hover:border-nexus-red/50 hover:text-nexus-red disabled:opacity-50"
          >
            Reject
          </button>
          <button
            onClick={() => onDecide("approve")}
            disabled={submitting !== null}
            className="rounded-md bg-nexus-red px-4 py-1.5 text-[12px] font-semibold text-white transition-colors hover:brightness-110 disabled:opacity-50"
          >
            {submitting === "approve" ? "Executing…" : "Approve & rollback"}
          </button>
        </div>
      </footer>
    </section>
  );
}