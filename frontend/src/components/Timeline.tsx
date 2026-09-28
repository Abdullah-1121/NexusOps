import { useState, type ReactNode } from "react";
import type { EvidenceItem, NexusEvent, Plan, RemediationStep } from "../types";

/* ------------------------------------------------------------------ */
/* Stage vocabulary — D-12: plain words, one-line hint per milestone.  */
/* ------------------------------------------------------------------ */

interface StageMeta {
  label: string;
  dot: string;
  hint: string;
}

const STAGE: Record<string, StageMeta> = {
  ingest: {
    label: "Incident received",
    dot: "bg-nexus-accent",
    hint: "The alert was accepted by the webhook and queued for analysis.",
  },
  classified: {
    label: "Classified",
    dot: "bg-nexus-blue",
    hint: "Severity and triage confidence were decided.",
  },
  escalating: {
    label: "Escalating",
    dot: "bg-nexus-amber",
    hint: "Handed to the stronger model for deeper analysis.",
  },
  tool_call: {
    label: "Evidence gathered",
    dot: "bg-nexus-blue",
    hint: "Read service logs and metric series to ground the analysis in data.",
  },
  plan: {
    label: "Remediation plan",
    dot: "bg-nexus-blue",
    hint: "Root cause plus fix steps, written from the evidence.",
  },
  gate_open: {
    label: "Awaiting operator approval",
    dot: "bg-nexus-amber",
    hint: "The machine is parked. Only a human decision can proceed.",
  },
  decision: {
    label: "Operator decision",
    dot: "bg-nexus-amber",
    hint: "The human's call, recorded by the gate.",
  },
  rollback: {
    label: "Rollback executing",
    dot: "bg-nexus-red",
    hint: "Applying the approved fix.",
  },
  done: {
    label: "Terminal",
    dot: "bg-nexus-green",
    hint: "The run finished.",
  },
  manual_review: {
    label: "Human review required",
    dot: "bg-nexus-red",
    hint: "The machine could not complete the analysis safely on its own.",
  },
  error: {
    label: "Pipeline error",
    dot: "bg-nexus-red",
    hint: "Something failed loudly. Details below.",
  },
};

const TERMINAL_STYLE: Record<string, string> = {
  resolved:
    "bg-nexus-green/10 text-nexus-green border-nexus-green/30",
  rejected:
    "bg-nexus-amber/10 text-nexus-amber border-nexus-amber/30",
  manual_review: "bg-nexus-red/10 text-nexus-red border-nexus-red/30",
};

/* ------------------------------------------------------------------ */
/* Small shared atoms                                                  */
/* ------------------------------------------------------------------ */

function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-[12px] text-nexus-text">{children}</span>;
}

function Chip({
  children,
  cls = "border-nexus-border text-nexus-muted",
}: {
  children: ReactNode;
  cls?: string;
}) {
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10.5px] font-medium ${cls}`}
    >
      {children}
    </span>
  );
}

function KV({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="w-28 shrink-0 text-[11px] font-medium uppercase tracking-wide text-nexus-faint">
        {label}
      </span>
      <span className="min-w-0 break-all">{value}</span>
    </div>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-64 overflow-auto rounded-md border border-nexus-border bg-nexus-raise/60 p-3 font-mono text-[11.5px] leading-relaxed text-nexus-text">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function pretty(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function fmtMs(ms: unknown): string {
  return typeof ms === "number" ? `${Math.round(ms)} ms` : "—";
}

/* ------------------------------------------------------------------ */
/* Run-state pill for the card header                                  */
/* ------------------------------------------------------------------ */

function SevChip({ severity }: { severity: string }) {
  const sv = String(severity ?? "?");
  const cls =
    sv === "critical"
      ? "border-nexus-red/40 bg-nexus-red/10 text-nexus-red"
      : sv === "warning"
        ? "border-nexus-amber/40 bg-nexus-amber/10 text-nexus-amber"
        : sv === "info"
          ? "border-nexus-blue/40 bg-nexus-blue/10 text-nexus-blue"
          : "border-nexus-border text-nexus-muted";
  return (
    <Chip cls={cls}>{sv.toUpperCase()}</Chip>
  );
}

function RunState({ events }: { events: NexusEvent[] }) {
  const last = events[events.length - 1];
  if (!last) return null;
  if (last.type === "done") {
    const cls = TERMINAL_STYLE[String(last.terminal)] ?? "border-nexus-border text-nexus-muted";
    return <Chip cls={cls}>{String(last.terminal ?? "done").toUpperCase()}</Chip>;
  }
  if (last.type === "gate_open") {
    return (
      <Chip cls="border-nexus-amber/40 bg-nexus-amber/10 font-semibold text-nexus-amber">
        AWAITING DECISION
      </Chip>
    );
  }
  if (["error", "manual_review"].includes(last.type)) {
    return <Chip cls="border-nexus-red/40 bg-nexus-red/10 text-nexus-red">ATTENTION</Chip>;
  }
  return (
    <Chip cls="border-nexus-blue/40 bg-nexus-blue/10 text-nexus-blue">
      PROCESSING
    </Chip>
  );
}

/* ------------------------------------------------------------------ */
/* Expanded detail per event type — the data was always there, this    */
/* renderer simply stops hiding it (D-12 batch A).                     */
/* ------------------------------------------------------------------ */

function StageDetail({ ev }: { ev: NexusEvent }) {
  switch (ev.type) {
    case "ingest": {
      return (
        <div className="space-y-1.5">
          <KV label="Service" value={<Mono>{pretty(ev.service)}</Mono>} />
          <KV label="Message" value={<span className="text-[12.5px]">{pretty(ev.message)}</span>} />
          <KV label="Source" value={<Mono>{pretty(ev.source)}</Mono>} />
          <KV label="Occurred at" value={<Mono>{pretty(ev.occurred_at)}</Mono>} />
          <KV label="Status code" value={<Mono>{pretty(ev.status_code)}</Mono>} />
        </div>
      );
    }
    case "classified": {
      const sev = String(ev.severity ?? "?");
      const sevCls =
        sev === "critical"
          ? "border-nexus-red/40 bg-nexus-red/10 text-nexus-red"
          : sev === "warning"
            ? "border-nexus-amber/40 bg-nexus-amber/10 text-nexus-amber"
            : sev === "info"
              ? "border-nexus-blue/40 bg-nexus-blue/10 text-nexus-blue"
              : "border-nexus-border text-nexus-muted";
      return (
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <KV
              label="Severity"
              value={
                <span className="flex items-center gap-2">
                  <Chip cls={sevCls}>{sev.toUpperCase()}</Chip>
                  {ev.ambiguous ? (
                    <Chip cls="border-nexus-amber/40 bg-nexus-amber/10 text-nexus-amber">
                      AMBIGUOUS
                    </Chip>
                  ) : null}
                </span>
              }
            />
          </div>
          <KV label="Triage confidence" value={<Mono>{pretty(ev.triage_confidence)}</Mono>} />
          <KV label="Model" value={<Mono>{pretty(ev.model_env)}</Mono>} />
          <KV label="This stage took" value={<Mono>{fmtMs(ev.stage_duration_ms)}</Mono>} />
        </div>
      );
    }
    case "escalating": {
      return (
        <div className="space-y-1.5">
          <KV label="Rule" value={<span className="text-[12.5px]">D-4 escalation rule fired — deeper analysis engaged.</span>} />
          {ev.depth_model ? (
            <KV label="Depth model" value={<Mono>{String(ev.depth_model)}</Mono>} />
          ) : null}
          <KV label="This stage took" value={<Mono>{fmtMs(ev.stage_duration_ms)}</Mono>} />
        </div>
      );
    }
    case "tool_call": {
      const items = (ev.evidence as EvidenceItem[] | undefined) ?? [];
      if (items.length === 0) {
        return <p className="text-[12.5px] text-nexus-muted">No evidence records returned.</p>;
      }
      // Group by originating tool (B1: app/evidence.py tags every record):
      // the operator sees FUNCTION CALLS, not an anonymous blob.
      const byTool = new Map<string, EvidenceItem[]>();
      for (const item of items) {
        const t = item._tool ?? "unknown";
        byTool.set(t, [...(byTool.get(t) ?? []), item]);
      }
      return (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <span className="text-[10.5px] font-semibold uppercase tracking-wide text-nexus-faint">
              Function calls
            </span>
            <span className="font-mono text-[11px] text-nexus-muted">
              {items.length} records · {fmtMs(ev.stage_duration_ms)}
            </span>
          </div>
          <div className="space-y-2">
            {[...byTool.entries()].map(([tool, recs]) => (
              <FunctionCallRow key={tool} tool={tool} records={recs} />
            ))}
          </div>
        </div>
      );
    }
    case "plan":
    case "gate_open": {
      const plan = ev.plan as Plan | null | undefined;
      if (!plan) return <JsonBlock value={ev} />;
      const conf = typeof plan.confidence === "number" ? Math.round(plan.confidence * 100) : null;
      return (
        <div className="space-y-3">
          <div>
            <p className="mb-0.5 text-[11px] font-medium uppercase tracking-wide text-nexus-faint">
              Root-cause hypothesis
            </p>
            <p className="text-[13px] font-medium text-nexus-text">
              {plan.root_cause_hypothesis ?? "(none offered)"}
            </p>
          </div>
          {conf != null && (
            <div>
              <p className="mb-1 flex items-baseline justify-between text-[11px] font-medium uppercase tracking-wide text-nexus-faint">
                <span>Confidence</span>
                <span className="font-mono text-[11px] text-nexus-muted">{conf}%</span>
              </p>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-nexus-raise">
                <div
                  className="h-full rounded-full bg-nexus-accent"
                  style={{ width: `${conf}%` }}
                />
              </div>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            {plan.severity && <Chip cls="border-nexus-border text-nexus-muted">severity {plan.severity}</Chip>}
            {plan.affected_service && (
              <Chip cls="border-nexus-border text-nexus-muted">service {plan.affected_service}</Chip>
            )}
            <Chip
              cls={
                plan.requires_approval
                  ? "border-nexus-amber/40 bg-nexus-amber/10 text-nexus-amber"
                  : "border-nexus-green/40 bg-nexus-green/10 text-nexus-green"
              }
            >
              {plan.requires_approval ? "REQUIRES HUMAN APPROVAL" : "AUTO-CLEARED"}
            </Chip>
            {ev.model_env ? (
              <Chip cls="border-nexus-border text-nexus-muted">{String(ev.model_env)}</Chip>
            ) : null}
            <KV label="This stage took" value={<Mono>{fmtMs(ev.stage_duration_ms)}</Mono>} />
          </div>
          <div>
            <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-nexus-faint">
              Remediation steps ({plan.remediation_steps?.length ?? 0})
            </p>
            {plan.remediation_steps?.length ? (
              <ul className="space-y-1.5">
                {plan.remediation_steps.map((s, i) => (
                  <StepRow key={i} step={s} />
                ))}
              </ul>
            ) : (
              <p className="text-[12.5px] text-nexus-muted">No steps offered.</p>
            )}
          </div>
        </div>
      );
    }
    case "decision": {
      return (
        <div className="space-y-1.5">
          <KV
            label="Decision"
            value={
              <Chip
                cls={
                  ev.decision === "approve"
                    ? "border-nexus-green/40 bg-nexus-green/10 text-nexus-green"
                    : "border-nexus-amber/40 bg-nexus-amber/10 text-nexus-amber"
                }
              >
                {String(ev.decision ?? "?").toUpperCase()}
              </Chip>
            }
          />
          <KV label="Actor" value={<Mono>{pretty(ev.actor)}</Mono>} />
          <KV label="Waiting time" value={<Mono>{fmtMs(ev.gate_wait_ms)}</Mono>} />
        </div>
      );
    }
    case "rollback": {
      const r = ev.rollback_result as Record<string, unknown> | string | null | undefined;
      return (
        <div className="space-y-1.5">
          {typeof r === "string" ? (
            <>
              <KV label="Result" value={<span className="text-[12.5px]">{r}</span>} />
              <JsonBlock value={ev} />
            </>
          ) : (
            <>
              <KV label="Status" value={<Mono>{typeof r?.status === "string" ? r.status : "executed"}</Mono>} />
              <KV label="Tag" value={<Mono>{pretty(r?.tag)}</Mono>} />
              <KV label="This stage took" value={<Mono>{fmtMs(ev.stage_duration_ms)}</Mono>} />
              {r ? <JsonBlock value={r} /> : <JsonBlock value={ev} />}
            </>
          )}
        </div>
      );
    }
    case "done": {
      const terminal = String(ev.terminal ?? "?");
      return (
        <div className="space-y-1.5">
          <KV
            label="Terminal state"
            value={
              <Chip cls={TERMINAL_STYLE[terminal] ?? "border-nexus-border text-nexus-muted"}>
                {terminal.toUpperCase()}
              </Chip>
            }
          />
          <KV label="Total run time" value={<Mono>{fmtMs(ev.t_resolve_ms)}</Mono>} />
          <KV label="Reason" value={<span className="text-[12.5px]">{pretty(ev.manual_review_reason)}</span>} />
        </div>
      );
    }
    case "manual_review": {
      return (
        <div className="space-y-1.5">
          <KV label="Reason" value={<span className="text-[12.5px]">{pretty(ev.reason)}</span>} />
          <KV label="Suggested path" value={<span className="text-[12.5px]">{pretty(ev.suggested)}</span>} />
        </div>
      );
    }
    case "error": {
      return (
        <div className="space-y-1.5">
          <KV label="Error" value={<span className="text-[12.5px] text-nexus-red">{pretty(ev.error)}</span>} />
          <JsonBlock value={ev} />
        </div>
      );
    }
    default:
      return <JsonBlock value={ev} />;
  }
}

function StepRow({ step }: { step: RemediationStep }) {
  const action = String(step.action ?? "?");
  const actionCls =
    action === "rollback"
      ? "border-nexus-red/40 bg-nexus-red/10 text-nexus-red"
      : action === "restart"
        ? "border-nexus-amber/40 bg-nexus-amber/10 text-nexus-amber"
        : "border-nexus-blue/40 bg-nexus-blue/10 text-nexus-blue";
  return (
    <li className="flex items-start gap-2">
      <Chip cls={actionCls}>{action}</Chip>
      <span className="min-w-0 flex-1 text-[12.5px] leading-snug text-nexus-text">
        {step.target && <code className="font-mono text-[11.5px] text-nexus-muted">{step.target}</code>}
        {step.reason && (
          <span className="block text-nexus-muted">{step.reason}</span>
        )}
      </span>
    </li>
  );
}

function EvidenceCard({ item, index }: { item: EvidenceItem; index: number }) {
  const [open, setOpen] = useState(false);
  const tool = item._tool ?? "evidence";
  return (
    <div className="rounded-md border border-nexus-border">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left hover:bg-nexus-raise/60"
      >
        <span className="flex items-center gap-2">
          <Chip cls="border-nexus-blue/40 bg-nexus-blue/10 text-nexus-blue">{tool}</Chip>
          <span className="font-mono text-[11px] text-nexus-faint">#{index + 1}</span>
        </span>
        <span className="font-mono text-[11px] text-nexus-faint">{open ? "hide" : "show"}</span>
      </button>
      {open && (
        <div className="border-t border-nexus-border p-2.5">
          <JsonBlock value={item} />
        </div>
      )}
    </div>
  );
}

/* One visible function-call row: tool name, return count, and one-line
 * preview per record — the call is never hidden behind expansion (T-7.10). */
function FunctionCallRow({ tool, records }: { tool: string; records: EvidenceItem[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="overflow-hidden rounded-md border border-nexus-border">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 bg-nexus-raise/40 px-2.5 py-2 text-left hover:bg-nexus-raise/70"
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="rounded border border-nexus-blue/40 bg-nexus-blue/10 px-1.5 py-0.5 font-mono text-[10.5px] font-semibold text-nexus-blue">
            {tool}
          </span>
          <span className="font-mono text-[11px] text-nexus-muted">
            → {records.length} {records.length === 1 ? "record" : "records"}
          </span>
        </span>
        <span className="shrink-0 font-mono text-[11px] text-nexus-faint">
          {open ? "hide" : "show records"}
        </span>
      </button>
      {!open && records.length > 0 && (
        <p className="truncate border-t border-nexus-border px-2.5 py-1.5 text-[11.5px] text-nexus-muted">
          {preview(records[0])}
        </p>
      )}
      {open && (
        <div className="space-y-1.5 border-t border-nexus-border p-2.5">
          {records.map((item, i) => (
            <EvidenceCard key={i} item={item} index={i} />
          ))}
        </div>
      )}
    </div>
  );
}

function preview(item: EvidenceItem): string {
  const parts: string[] = [];
  for (const k of ["timestamp", "level", "message", "summary"]) {
    const v = item[k];
    if (v !== undefined && v !== null) parts.push(String(v));
  }
  return parts.join(" · ") || JSON.stringify(item).slice(0, 200);
}

/* ------------------------------------------------------------------ */
/* Timeline card                                                       */
/* ------------------------------------------------------------------ */

export default function Timeline({
  incidentId,
  events,
  queued = null,
}: {
  incidentId: string;
  events: NexusEvent[];
  queued?: { blockedBy: string; depth: number } | null;
}) {
  const [openSeq, setOpenSeq] = useState<number | null>(null);

  const ingest = events.find((e) => e.type === "ingest");
  const classified = events.find((e) => e.type === "classified");
  const severity = classified?.severity as string | undefined;

  return (
    <section className="overflow-hidden rounded-lg border border-nexus-border bg-nexus-panel shadow-[0_1px_2px_rgba(16,24,40,0.05)]">
      <header className="flex items-center justify-between gap-3 border-b border-nexus-border px-4 py-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <div className="flex min-w-0 items-center gap-2.5">
            <span className="truncate font-mono text-[13px] font-semibold text-nexus-text">
              {incidentId}
            </span>
            {severity && <SevChip severity={severity} />}
            <RunState events={events} />
          </div>
          {ingest && (
            <p className="truncate text-[11.5px] text-nexus-muted">
              <span className="font-mono text-[11px] text-nexus-faint">
                {String(ingest.service ?? "")}
              </span>
              {ingest.message ? ` — ${String(ingest.message)}` : ""}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-3 text-[11px] text-nexus-muted">
          <span>
            {events.length} {events.length === 1 ? "stage" : "stages"}
          </span>
          <span className="hidden text-nexus-faint sm:inline">
            click a stage for its full detail
          </span>
        </div>
      </header>

      {queued && (
        <div className="flex items-center justify-between gap-3 border-b border-nexus-amber/30 bg-nexus-amber/8 px-4 py-2">
          <span className="flex items-center gap-2 text-[12px] font-medium text-nexus-amber">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-nexus-amber" />
            QUEUED behind {queued.blockedBy} — that gate needs a human decision
            before this run starts
          </span>
          <span className="font-mono text-[11px] text-nexus-muted">{queued.depth} queued</span>
        </div>
      )}

      <ol className="px-4 py-3">
        {events.map((ev) => {
          const meta = STAGE[ev.type] ?? {
            label: ev.type,
            dot: "bg-nexus-faint/50",
            hint: "Unknown event type — shown raw.",
          };
          const expanded = openSeq === ev.seq;
          return (
            <li key={ev.seq} className="relative">
              <div className="flex gap-3 pb-1.5">
                <div className="flex flex-col items-center">
                  <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${meta.dot}`} />
                  <span className="mt-1 w-px flex-1 bg-nexus-border" />
                </div>

                <button
                  onClick={() => setOpenSeq(expanded ? null : ev.seq)}
                  className="group -mx-2 my-1 flex min-w-0 flex-1 items-start gap-3 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-nexus-raise/70"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-[13px] font-medium text-nexus-text">
                        {meta.label}
                      </span>
                      <span className="shrink-0 font-mono text-[10.5px] text-nexus-faint">
                        #{ev.seq}
                      </span>
                    </div>
                    <Summary ev={ev} />
                    {expanded && (
                      <div className="mt-2 rounded-md border border-nexus-border bg-nexus-panel p-3">
                        <p className="mb-2.5 text-[11px] leading-snug text-nexus-muted">
                          {meta.hint}
                        </p>
                        <StageDetail ev={ev} />
                      </div>
                    )}
                  </div>
                  <span
                    className={`mt-1 shrink-0 text-[11px] text-nexus-faint transition-transform ${
                      expanded ? "rotate-90" : ""
                    }`}
                  >
                    ▸
                  </span>
                </button>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* One-line summary per stage (what the row shows collapsed)           */
/* ------------------------------------------------------------------ */

function Summary({ ev }: { ev: NexusEvent }) {
  switch (ev.type) {
    case "ingest":
      return (
        <p className="mt-0.5 truncate text-[12px] text-nexus-muted">
          {String(ev.service)} · {String(ev.message)}
        </p>
      );
    case "classified":
      return (
        <p className="mt-0.5 truncate text-[12px] text-nexus-muted">
          severity={String(ev.severity)} confidence={String(ev.triage_confidence)}
          {ev.ambiguous ? " ambiguous" : ""}
          {" · "}
          {fmtMs(ev.stage_duration_ms)}
        </p>
      );
    case "escalating":
      return (
        <p className="mt-0.5 truncate text-[12px] text-nexus-muted">
          {ev.depth_model ? `${String(ev.depth_model)}` : "depth model"} engaged
          {typeof ev.stage_duration_ms === "number" ? ` · ${fmtMs(ev.stage_duration_ms)}` : ""}
        </p>
      );
    case "tool_call": {
      const items = (ev.evidence as EvidenceItem[] | undefined) ?? [];
      const byTool = new Map<string, number>();
      for (const i of items) {
        const t = String(i._tool ?? "unknown");
        byTool.set(t, (byTool.get(t) ?? 0) + 1);
      }
      const perTool = [...byTool.entries()].map(([t, n]) => `${t}→${n}`).join(" ");
      return (
        <p className="mt-0.5 truncate text-[12px] text-nexus-muted">
          {items.length} {items.length === 1 ? "record" : "records"}
          {perTool ? ` · ${perTool}` : ""}
          {" · "}
          {fmtMs(ev.stage_duration_ms)}
        </p>
      );
    }
    case "plan": {
      const plan = ev.plan as Plan | undefined;
      if (!plan) return <p className="mt-0.5 text-[12px] text-nexus-muted">{clip(JSON.stringify(ev.plan))}</p>;
      return (
        <p className="mt-0.5 truncate text-[12px] text-nexus-muted">
          {plan.root_cause_hypothesis ?? "?"} · confidence {String(plan.confidence ?? "?")} ·{" "}
          {(plan.remediation_steps ?? []).length} steps
          {plan.requires_approval ? " · approval" : ""}
          {ev.model_env ? ` · ${String(ev.model_env)}` : ""}
          {" · "}
          {fmtMs(ev.stage_duration_ms)}
        </p>
      );
    }
    case "gate_open":
      return <p className="mt-0.5 truncate text-[12px] text-nexus-amber">plan locked in — your call at the gate</p>;
    case "decision":
      return (
        <p className="mt-0.5 truncate text-[12px] text-nexus-muted">
          {String(ev.decision)} by {String(ev.actor)}
        </p>
      );
    case "rollback": {
      const r = ev.rollback_result as { tag?: string } | undefined;
      return (
        <p className="mt-0.5 truncate text-[12px] text-nexus-muted">
          {r?.tag ? `tag ${r.tag}` : clip(JSON.stringify(ev.rollback_result))}
        </p>
      );
    }
    case "done":
      return (
        <p className="mt-0.5 truncate text-[12px] text-nexus-muted">
          terminal={String(ev.terminal)}
          {typeof ev.t_resolve_ms === "number" ? ` · ${ev.t_resolve_ms}ms` : ""}
          {ev.manual_review_reason ? ` · ${String(ev.manual_review_reason)}` : ""}
        </p>
      );
    case "manual_review":
      return <p className="mt-0.5 truncate text-[12px] text-nexus-muted">{String(ev.reason)}</p>;
    case "error":
      return <p className="mt-0.5 truncate text-[12px] text-nexus-red">{String(ev.error)}</p>;
    default:
      return <p className="mt-0.5 truncate text-[12px] text-nexus-muted">{clip(JSON.stringify(ev))}</p>;
  }
}

function clip(json: string, max = 320): string {
  return json.length > max ? `${json.slice(0, max)}…` : json;
}