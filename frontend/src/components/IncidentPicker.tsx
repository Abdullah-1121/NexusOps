import type { FixtureIncident, NexusEvent } from "../types";

const SEVERITY_STYLE: Record<string, { label: string; cls: string }> = {
  critical: {
    label: "CRITICAL",
    cls: "border-nexus-red/40 bg-nexus-red/10 text-nexus-red",
  },
  warning: {
    label: "WARNING",
    cls: "border-nexus-amber/40 bg-nexus-amber/10 text-nexus-amber",
  },
  info: {
    label: "INFO",
    cls: "border-nexus-blue/40 bg-nexus-blue/10 text-nexus-blue",
  },
};

interface Props {
  fixtures: FixtureIncident[];
  firing: string | null;
  fired: { id: string; outcome: "added" | "duplicate" | "rejected" } | null;
  onFire: (f: FixtureIncident) => void;
  activeId: string | null;
  byIncident: Map<string, NexusEvent[]>;
  onSelect: (id: string) => void;
  gatesWaiting: string[];
  queueDepth: number;
}

/** "Run an incident" — fire one synthetic alert through the real webhook.
 *  Only alert fields are offered (ground truth never leaks; NG-1 the operator
 *  truly decides). Already-fired incidents appear as selectable runs. */
export default function IncidentPicker({
  fixtures,
  firing,
  fired,
  onFire,
  activeId,
  byIncident,
  onSelect,
  gatesWaiting,
  queueDepth,
}: Props) {
  const sev = (f: FixtureIncident) =>
    SEVERITY_STYLE[f.severity_hint ?? "info"] ?? SEVERITY_STYLE.info;

  return (
    <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-nexus-border bg-nexus-panel shadow-[0_1px_2px_rgba(16,24,40,0.05)]">
      <h2 className="border-b border-nexus-border px-3.5 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-nexus-muted">
        Run an incident
      </h2>
      <div className="min-h-0 flex-1 divide-y divide-nexus-border overflow-y-auto">
        {fixtures.map((f) => {
          const s = sev(f);
          const hasRun = byIncident.has(f.incident_id);
          const isFiring = firing === f.incident_id;
          const isActive = activeId === f.incident_id;
          return (
            <div
              key={f.incident_id}
              className={`px-3.5 py-2.5 ${isActive ? "bg-nexus-raise/50" : ""}`}
            >
              <div className="flex items-center gap-2">
                <span className="font-mono text-[12px] font-semibold text-nexus-text">
                  {f.incident_id}
                </span>
                <span
                  className={`rounded border px-1.5 py-px text-[9.5px] font-bold tracking-wide ${s.cls}`}
                >
                  {s.label}
                </span>
              </div>
              <p className="mt-0.5 truncate text-[12px] text-nexus-muted">
                {f.service} — {f.message}
              </p>
              <div className="mt-1.5 flex items-center justify-between">
                <span className="font-mono text-[10.5px] text-nexus-faint">
                  {f.source}
                </span>
                {hasRun ? (
                  <span className="flex gap-1.5">
                    <button
                      onClick={() => onFire(f)}
                      disabled={isFiring}
                      className="rounded border border-nexus-border px-2 py-0.5 text-[11px] font-medium text-nexus-muted transition-colors hover:bg-nexus-raise hover:text-nexus-text disabled:opacity-50"
                    >
                      {isFiring ? "firing…" : "fire again"}
                    </button>
                    <button
                      onClick={() => onSelect(f.incident_id)}
                      className="rounded border border-nexus-border px-2 py-0.5 text-[11px] font-medium text-nexus-muted transition-colors hover:bg-nexus-raise hover:text-nexus-text"
                    >
                      view run
                    </button>
                  </span>
                ) : (
                  <button
                    onClick={() => onFire(f)}
                    disabled={isFiring || firing !== null}
                    className="rounded bg-nexus-accent px-2.5 py-0.5 text-[11px] font-semibold text-white transition-colors hover:bg-nexus-blue disabled:opacity-50"
                  >
                    {isFiring ? "firing…" : "fire live"}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {fired && (() => {
        // Queue honesty (D-11 postmortem #5): accepted ≠ running. If a gate is
        // parked the moment you fire, your incident is queued — name the blocker
        // so the operator never reads silence as a dead machine. Note: we do NOT
        // exclude the fired id here — re-firing c-02 while an earlier c-02 run is
        // parked is still a queued run, and the message must say so (the parked
        // gate always belongs to some prior run, never the one being fired).
        const blockedBy = fired.outcome === "added" ? gatesWaiting : [];
        const queued = blockedBy.length > 0;
        const sameName = queued && blockedBy.includes(fired.id);
        const cls = fired.outcome === "added"
          ? queued ? "text-nexus-amber" : "text-nexus-green"
          : fired.outcome === "duplicate"
            ? "text-nexus-amber"
            : "text-nexus-red";
        return (
          <div
            className={`border-t border-nexus-border px-3.5 py-2 text-[11.5px] font-medium ${cls}`}
          >
            {fired.outcome === "added" && queued && sameName &&
              `queued ${fired.id} — an earlier ${fired.id} run is parked at the gate; decide it first (queue ${queueDepth})`}
            {fired.outcome === "added" && queued && !sameName &&
              `queued ${fired.id} — ${blockedBy.join(" + ")} parked at the gate; decide it first (queue ${queueDepth})`}
            {fired.outcome === "added" && !queued &&
              `accepted & enqueued ${fired.id} — watch it stream`}
            {fired.outcome === "duplicate" &&
              `duplicate ${fired.id} — already known to the seen-set, nothing enqueued`}
            {fired.outcome === "rejected" &&
              `rejected ${fired.id} — check serve/Redis`}
          </div>
        );
      })()}
    </section>
  );
}