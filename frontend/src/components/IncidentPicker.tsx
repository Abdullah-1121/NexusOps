import type { FixtureIncident, NexusEvent } from "../types";

const SEVERITY_STYLE: Record<string, { label: string; cls: string }> = {
  critical: { label: "CRITICAL", cls: "border-nexus-red text-nexus-red" },
  warning: { label: "WARNING", cls: "border-nexus-amber text-nexus-amber" },
  info: { label: "INFO", cls: "border-nexus-blue text-nexus-blue" },
};

interface Props {
  fixtures: FixtureIncident[];
  firing: string | null;
  fired: { id: string; outcome: "added" | "duplicate" | "rejected" } | null;
  onFire: (f: FixtureIncident) => void;
  activeId: string | null;
  byIncident: Map<string, NexusEvent[]>;
  onSelect: (id: string) => void;
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
}: Props) {
  const sev = (f: FixtureIncident) =>
    SEVERITY_STYLE[f.severity_hint ?? "info"] ?? SEVERITY_STYLE.info;

  return (
    <section className="flex min-h-0 flex-1 flex-col rounded-lg border border-nexus-border bg-nexus-panel">
      <h2 className="border-b border-nexus-border px-3 py-2 text-[11px] font-medium uppercase tracking-widest text-nexus-muted">
        Run an incident
      </h2>
      <div className="min-h-0 flex-1 divide-y divide-nexus-border overflow-y-auto">
        {fixtures.map((f) => {
          const s = sev(f);
          const hasRun = byIncident.has(f.incident_id);
          const isFiring = firing === f.incident_id;
          return (
            <div
              key={f.incident_id}
              className={`border-l-2 px-3 py-2.5 transition-colors ${
                activeId === f.incident_id ? "bg-nexus-raise" : "hover:bg-nexus-raise/60"
              } ${s.cls}`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-[11px] text-nexus-muted">
                  {f.incident_id}
                </span>
                <span
                  className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold ${s.cls} border-current`}
                >
                  {s.label}
                </span>
              </div>
              <div className="mt-1 truncate text-xs text-nexus-text" title={f.message}>
                {f.service} — {f.message}
              </div>
              <div className="mt-2 flex items-center gap-2">
                <button
                  onClick={() => onFire(f)}
                  disabled={isFiring}
                  className="rounded border border-nexus-accent/50 px-2 py-1 text-[11px] font-medium text-nexus-accent transition-colors hover:bg-nexus-accent/10 disabled:opacity-50"
                >
                  {isFiring ? "firing…" : hasRun ? "fire again" : "fire live"}
                </button>
                {hasRun && (
                  <button
                    onClick={() => onSelect(f.incident_id)}
                    className={`text-[11px] ${
                      activeId === f.incident_id
                        ? "text-nexus-text"
                        : "text-nexus-muted hover:text-nexus-text"
                    }`}
                  >
                    view run
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {fired && (
        <div
          className={`border-t border-nexus-border px-3 py-2 text-[11px] font-mono ${
            fired.outcome === "added"
              ? "text-nexus-green"
              : fired.outcome === "duplicate"
                ? "text-nexus-amber"
                : "text-nexus-red"
          }`}
        >
          {fired.outcome === "added" &&
            `accepted & enqueued ${fired.id} — watch it stream`}
          {fired.outcome === "duplicate" &&
            `duplicate ${fired.id} — already known to the seen-set, nothing enqueued`}
          {fired.outcome === "rejected" &&
            `rejected ${fired.id} — check serve/Redis`}
        </div>
      )}
    </section>
  );
}