import { useState, type ReactNode } from "react";
import type { EvidenceItem, NexusEvent, Plan, RemediationStep } from "../types";

/* ------------------------------------------------------------------ */
/* Incident record (T-7.10, 2026-09-28): one consolidated operator     */
/* panel with EVERYTHING about the run — alert payload, classification,*/
/* evidence roll-up, plan, gate decision, terminal, timings, raw       */
/* events. No detail hides behind a click (the timeline remains for    */
/* the narrative; this is the audit record).                           */
/* ------------------------------------------------------------------ */

function K({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="shrink-0 text-[11px] font-medium uppercase tracking-wide text-nexus-faint">
        {label}
      </span>
      <span className="min-w-0 break-all text-right text-[12.5px] text-nexus-text">{value}</span>
    </div>
  );
}

function M({ children }: { children: ReactNode }) {
  return <span className="font-mono text-[11.5px] text-nexus-text">{children}</span>;
}

function pretty(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** One readable line per evidence record: timestamp · level · message/summary,
 *  falling back to the raw record when the tools returned other shapes. */
function previewParts(r: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const k of ["timestamp", "level", "message", "summary", "text", "value"]) {
    const v = r[k];
    if (v !== undefined && v !== null && String(v) !== "") parts.push(String(v));
  }
  const ignored = new Set(["_tool", "timestamp", "level", "message", "summary", "text", "value"]);
  const extra = Object.entries(r)
    .filter(([k, v]) => !ignored.has(k) && v !== undefined && v !== null)
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
  return [...parts, ...extra].join(" · ") || "";
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border-b border-nexus-border px-4 py-3 last:border-b-0">
      <h3 className="mb-2 text-[10.5px] font-semibold uppercase tracking-wider text-nexus-faint">
        {title}
      </h3>
      {children}
    </section>
  );
}

export default function IncidentRecord({ events }: { events: NexusEvent[] }) {
  const [rawOpen, setRawOpen] = useState(false);
  if (events.length === 0) return null;

  const by = (t: string) => events.find((e) => e.type === t);
  const ingest = by("ingest");
  const classified = by("classified");
  const tool = by("tool_call");
  const planEv = by("plan") ?? by("gate_open");
  const plan = planEv?.plan as Plan | null | undefined;
  const gate = by("gate_open");
  const decision = by("decision");
  const rollback = by("rollback");
  const done = by("done");
  const review = by("manual_review");
  const err = by("error");

  const evidence = (tool?.evidence as EvidenceItem[] | undefined) ?? [];
  const byTool = new Map<string, number>();
  for (const i of evidence) {
    const t = String(i._tool ?? "unknown");
    byTool.set(t, (byTool.get(t) ?? 0) + 1);
  }

  return (
    <section className="overflow-hidden rounded-lg border border-nexus-border bg-nexus-panel shadow-[0_1px_2px_rgba(16,24,40,0.05)]">
      <header className="flex items-center justify-between gap-3 border-b border-nexus-border px-4 py-2.5">
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-nexus-muted">
          Incident record
        </h2>
        <span className="font-mono text-[10.5px] text-nexus-faint">
          {events.length} events · {done ? `${pretty(done.t_resolve_ms)}ms total` : "in flight"}
        </span>
      </header>

      {ingest && (
        <Section title="Problem received">
          <K label="Service" value={<M>{pretty(ingest.service)}</M>} />
          <K label="Source" value={<M>{pretty(ingest.source)}</M>} />
          <K label="Occurred" value={<M>{pretty(ingest.occurred_at)}</M>} />
          {ingest.status_code !== undefined && (
            <K label="Status" value={<M>{pretty(ingest.status_code)}</M>} />
          )}
          {ingest.severity_hint !== undefined && ingest.severity_hint !== null && (
            <K label="Hint" value={<M>{pretty(ingest.severity_hint)}</M>} />
          )}
          {ingest.error_type ? (
            <K label="Error type" value={<M>{pretty(ingest.error_type)}</M>} />
          ) : null}
          <div className="mt-1.5 rounded-md border border-nexus-border bg-nexus-raise/50 p-2.5 text-[12.5px] leading-relaxed text-nexus-text">
            {pretty(ingest.summary ?? ingest.message)}
          </div>
          {ingest.context ? (
            <div className="mt-1.5 space-y-1">
              {Object.entries(ingest.context as Record<string, unknown>).map(([k, v]) => (
                <K key={k} label={k} value={<M>{pretty(v)}</M>} />
              ))}
            </div>
          ) : null}
        </Section>
      )}

      {classified && (
        <Section title="Classification">
          <K label="Severity" value={<M>{pretty(classified.severity)}</M>} />
          <K label="Confidence" value={<M>{pretty(classified.triage_confidence)}</M>} />
          <K label="Ambiguous" value={<M>{classified.ambiguous ? "yes" : "no"}</M>} />
          <K label="Model" value={<M>{pretty(classified.model_env)}</M>} />
          <K label="Duration" value={<M>{classified.stage_duration_ms !== undefined ? `${classified.stage_duration_ms}ms` : "—"}</M>} />
        </Section>
      )}

      {evidence.length > 0 && (
        <Section title="Evidence — MCP call results">
          <K label="Records" value={<M>{evidence.length}</M>} />
          <div className="mt-1 space-y-1">
            {[...byTool.entries()].map(([t, n]) => (
              <div key={t} className="flex items-center justify-between rounded border border-nexus-border px-2 py-1">
                <M>{t}</M>
                <M>{n} {n === 1 ? "record" : "records"}</M>
              </div>
            ))}
          </div>
          {/* D-16: the results themselves, never behind a click — each record
              the tools returned renders as a compact evidence line. */}
          <div className="mt-2 space-y-1">
            {evidence.map((r, i) => (
              <div
                key={i}
                className="rounded-md border border-nexus-border px-2.5 py-1.5"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="rounded border border-nexus-blue/40 bg-nexus-blue/10 px-1.5 py-px font-mono text-[10px] font-semibold text-nexus-blue">
                    {String(r._tool ?? "evidence")}
                  </span>
                  <span className="font-mono text-[10.5px] text-nexus-faint">#{i + 1}</span>
                </div>
                {previewParts(r) && (
                  <p className="mt-1 text-[11.5px] leading-snug text-nexus-text">{previewParts(r)}</p>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {plan && (
        <Section title="Plan">
          <div className="rounded-md border border-nexus-border bg-nexus-raise/50 p-2.5 text-[12.5px] leading-relaxed text-nexus-text">
            {pretty(plan.root_cause_hypothesis)}
          </div>
          {plan.reasoning ? (
            <div className="mt-2">
              <p className="mb-1 text-[10.5px] font-semibold uppercase tracking-wider text-nexus-faint">
                Why this plan
              </p>
              <p className="text-[12.5px] leading-relaxed text-nexus-muted">{plan.reasoning}</p>
            </div>
          ) : null}
          <K label="Confidence" value={<M>{typeof plan.confidence === "number" ? `${Math.round(plan.confidence * 100)}%` : pretty(plan.confidence)}</M>} />
          <K label="Model" value={<M>{pretty(planEv?.model_env)}</M>} />
          <K label="Approval" value={<M>{plan.requires_approval ? "required (NG-1)" : "not required"}</M>} />
          {Array.isArray(plan.evidence) && plan.evidence.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {plan.evidence.map((e, i) => (
                <span key={i} className="rounded border border-nexus-blue/40 bg-nexus-blue/10 px-1.5 py-px font-mono text-[10.5px] text-nexus-blue">
                  {e}
                </span>
              ))}
            </div>
          )}
          {(plan.remediation_steps ?? []).length > 0 && (
            <ul className="mt-2 space-y-1.5">
              {(plan.remediation_steps as RemediationStep[]).map((s, i) => (
                <li key={i} className="rounded-md border border-nexus-border px-2.5 py-1.5">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="rounded border border-nexus-blue/40 bg-nexus-blue/10 px-1.5 py-px font-mono text-[10.5px] font-semibold text-nexus-blue">
                      {String(s.action)}
                    </span>
                    <M>{pretty(s.target)}</M>
                  </span>
                  {s.reason && <p className="mt-1 text-[11.5px] leading-snug text-nexus-muted">{s.reason}</p>}
                </li>
              ))}
            </ul>
          )}
        </Section>
      )}

      {gate && (
        <Section title="Gate">
          <K label="Waited" value={<M>{decision?.gate_wait_ms !== undefined ? `${Math.round(Number(decision.gate_wait_ms))}ms` : "awaiting"}</M>} />
          {decision && (
            <>
              <K label="Decision" value={<M>{String(decision.decision)}</M>} />
              <K label="Actor" value={<M>{pretty(decision.actor)}</M>} />
              {decision.reason ? (
                <K label="Why" value={<span className="text-[11.5px] text-nexus-muted">{String(decision.reason)}</span>} />
              ) : null}
            </>
          )}
        </Section>
      )}

      {rollback && (
        <Section title="Rollback">
          <K label="Result" value={<M>{typeof rollback.rollback_result === "string" ? rollback.rollback_result : pretty((rollback.rollback_result as { tag?: string })?.tag)}</M>} />
        </Section>
      )}

      {(done || review || err) && (
        <Section title="Terminal">
          {done && <K label="State" value={<M>{pretty(done.terminal)}</M>} />}
          {done && <K label="Total" value={<M>{pretty(done.t_resolve_ms)}ms</M>} />}
          {(done?.manual_review_reason !== undefined && done?.manual_review_reason !== null
            || review?.reason !== undefined && review?.reason !== null) && (
            <K label="Reason" value={<span className="text-[11.5px] text-nexus-red">{pretty(review?.reason ?? done?.manual_review_reason)}</span>} />
          )}
          {err && <K label="Error" value={<span className="text-[11.5px] text-nexus-red">{pretty(err.error)}</span>} />}
        </Section>
      )}

      <div className="border-t border-nexus-border px-4 py-2.5">
        <button
          onClick={() => setRawOpen((v) => !v)}
          className="text-[11px] font-medium text-nexus-muted transition-colors hover:text-nexus-text"
        >
          {rawOpen ? "hide raw events" : "show raw events (JSON)"}
        </button>
        {rawOpen && (
          <pre className="mt-2 max-h-72 overflow-auto rounded-md border border-nexus-border bg-nexus-raise/60 p-3 font-mono text-[10.5px] leading-relaxed text-nexus-text">
            {JSON.stringify(events, null, 2)}
          </pre>
        )}
      </div>
    </section>
  );
}