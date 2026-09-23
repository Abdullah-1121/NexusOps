import type { ReactNode } from "react";
import type { NexusEvent, Plan } from "../types";

/** Stage metadata: label, gutter dot color, and a one-line summary of each
 *  event type the pipeline can emit (D-11 vocabulary). Unknown types render
 *  neutrally instead of disappearing — the console is forward-compatible. */
interface StageMeta {
  label: string;
  dot: string; // tailwind bg class for the gutter dot
}

const STAGE: Record<string, StageMeta> = {
  ingest: { label: "Incident received", dot: "bg-nexus-accent" },
  classified: { label: "Classified", dot: "bg-nexus-blue" },
  escalating: { label: "Escalating to frontier model", dot: "bg-nexus-amber" },
  tool_call: { label: "Evidence gathered", dot: "bg-nexus-blue" },
  plan: { label: "Remediation plan", dot: "bg-nexus-blue" },
  gate_open: { label: "Awaiting operator approval", dot: "bg-nexus-amber animate-pulse" },
  decision: { label: "Operator decision", dot: "bg-nexus-amber" },
  rollback: { label: "Rollback executing", dot: "bg-nexus-red" },
  done: { label: "Terminal", dot: "bg-nexus-green" },
  manual_review: { label: "Human review required", dot: "bg-nexus-red" },
  error: { label: "Pipeline error", dot: "bg-nexus-red" },
};

const TERMINAL_STYLE: Record<string, string> = {
  resolved: "text-nexus-green",
  rejected: "text-nexus-amber",
  manual_review: "text-nexus-red",
};

function clip(json: string, max = 320): string {
  return json.length > max ? `${json.slice(0, max)}…` : json;
}

function payload(ev: NexusEvent): ReactNode {
  switch (ev.type) {
    case "ingest":
      return (
        <Mono>
          {String(ev.service)} · {String(ev.message)}
        </Mono>
      );
    case "classified":
      return (
        <Mono>
          severity={String(ev.severity)} confidence={String(ev.triage_confidence)}
          {ev.ambiguous ? " ambiguous" : ""}
        </Mono>
      );
    case "tool_call":
      return <Mono>{(ev.evidence as unknown[] | undefined)?.length ?? 0} evidence items</Mono>;
    case "plan": {
      const plan = ev.plan as Plan | undefined;
      if (!plan) return <Mono>{clip(JSON.stringify(ev.plan))}</Mono>;
      return (
        <Mono>
          {plan.root_cause_hypothesis ?? "?"} · confidence{" "}
          {String(plan.confidence ?? "?")} · {(plan.remediation_steps ?? []).length} steps
          {plan.requires_approval ? " · requires approval" : ""}
        </Mono>
      );
    }
    case "gate_open":
      return <Mono>plan locked in — your call at the gate</Mono>;
    case "decision":
      return (
        <Mono>
          {String(ev.decision)} by {String(ev.actor)}
        </Mono>
      );
    case "rollback": {
      const r = ev.rollback_result as { tag?: string } | undefined;
      return <Mono>{r?.tag ? `tag ${r.tag}` : clip(JSON.stringify(ev.rollback_result))}</Mono>;
    }
    case "done":
      return (
        <Mono>
          terminal={String(ev.terminal)}
          {typeof ev.t_resolve_ms === "number" ? ` · ${ev.t_resolve_ms}ms` : ""}
          {ev.manual_review_reason ? ` · ${String(ev.manual_review_reason)}` : ""}
        </Mono>
      );
    case "manual_review":
      return <Mono>{String(ev.reason)}</Mono>;
    case "error":
      return <Mono>{String(ev.error)}</Mono>;
    default:
      return <Mono>{clip(JSON.stringify(ev))}</Mono>;
  }
}

function Mono({ children }: { children: ReactNode }) {
  return <div className="mt-0.5 truncate font-mono text-[11px] text-nexus-muted">{children}</div>;
}

/** The live stage timeline for one incident — each pipeline milestone as a
 *  beat (the same honest beats the feature-6 film derived from checkpoints,
 *  now produced live by the running machine). */
export default function Timeline({
  incidentId,
  events,
}: {
  incidentId: string;
  events: NexusEvent[];
}) {
  return (
    <section className="rounded-lg border border-nexus-border bg-nexus-panel">
      <header className="border-b border-nexus-border px-4 py-3">
        <div className="flex items-baseline justify-between">
          <h2 className="font-mono text-sm text-nexus-text">{incidentId}</h2>
          <span className="text-[11px] uppercase tracking-widest text-nexus-muted">
            {events.length} stages
          </span>
        </div>
      </header>
      <ol className="p-4">
        {events.map((ev) => {
          const meta = STAGE[ev.type] ?? {
            label: ev.type,
            dot: "bg-nexus-muted/50",
          };
          const isDone = ev.type === "done";
          const doneCls = isDone
            ? TERMINAL_STYLE[String(ev.terminal)] ?? "text-nexus-text"
            : "text-nexus-text";
          return (
            <li key={ev.seq} className="relative flex gap-3 pb-5 last:pb-0">
              <div className="flex flex-col items-center">
                <span className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${meta.dot}`} />
                <span className="mt-1 w-px flex-1 bg-nexus-border" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-2">
                  <span className={`text-xs font-medium ${doneCls}`}>{meta.label}</span>
                  <span className="font-mono text-[10px] text-nexus-muted">#{ev.seq}</span>
                </div>
                {payload(ev)}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}