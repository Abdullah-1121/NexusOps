import { useCallback, useEffect, useMemo, useState } from "react";
import GatePanel from "./components/GatePanel";
import IncidentPicker from "./components/IncidentPicker";
import StatusStrip from "./components/StatusStrip";
import Timeline from "./components/Timeline";
import { useNexusSocket } from "./useSocket";
import type { FixtureIncident, NexusEvent, SystemStatus, Tab } from "./types";

/** NexusOps — Live Operations Console (D-11).
 *
 * One screen shows the whole promise: pick an incident, fire it through the
 * real pipeline (smoke fakes or real Gemini — the serve worker decides), watch
 * every stage stream in, then be the gate: approve and see the rollback
 * execute, or reject and see nothing fire (NG-1). */
export default function App() {
  const [tab, setTab] = useState<Tab>("live");
  const { connected, events, decide } = useNexusSocket();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [fixtures, setFixtures] = useState<FixtureIncident[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [firing, setFiring] = useState<string | null>(null);
  const [fired, setFired] = useState<
    { id: string; outcome: "added" | "duplicate" | "rejected" } | null
  >(null);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const r = await fetch("/api/status");
        if (alive) setStatus(await r.json());
      } catch {
        /* serve not up yet in dev — keep last known */
      }
    };
    poll();
    const t = setInterval(poll, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  useEffect(() => {
    fetch("/api/fixtures")
      .then((r) => r.json())
      .then(setFixtures)
      .catch(() => {});
  }, []);

  const byIncident = useMemo(() => {
    const m = new Map<string, NexusEvent[]>();
    for (const ev of events) {
      const arr = m.get(ev.incident_id) ?? [];
      arr.push(ev);
      m.set(ev.incident_id, arr);
    }
    return m;
  }, [events]);

  const incidentIds = useMemo(() => [...byIncident.keys()].reverse(), [byIncident]);
  const active = activeId ?? incidentIds[0] ?? null;
  const activeEvents = active ? byIncident.get(active) ?? [] : [];

  // Queue honesty (D-11 postmortem #5): an incident that fired while another
  // is parked at the human gate is QUEUED, not broken. If the active run has
  // no stage beyond `ingest` and someone else holds an open gate, say so.
  const queued = useMemo(() => {
    if (!active || activeEvents.length === 0) return null;
    const gates = status?.gates_waiting ?? [];
    if (gates.length === 0 || gates.includes(active)) return null;
    const started = activeEvents.some((e) =>
      ["classified", "tool_call", "plan", "gate_open", "decision", "rollback", "done", "manual_review", "error"].includes(e.type),
    );
    if (started) return null;
    return { blockedBy: gates.join(", "), depth: status?.queue_depth ?? 0 };
  }, [active, activeEvents, status]);

  const fire = useCallback(async (f: FixtureIncident) => {
    setFiring(f.incident_id);
    try {
      // `force=1`: the console is the operator's rehearsal surface — a fixture
      // must be fireable again in one session even if the seen-set already
      // knows its id (postmortem 2026-09-23). The Feature-1 alert contract
      // stays dedupe-by-default; only this surface passes force.
      const r = await fetch("/webhook/incident?force=1", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          incident_id: f.incident_id,
          occurred_at: f.occurred_at,
          source: f.source,
          service: f.service,
          message: f.message,
          severity_hint: f.severity_hint,
          status_code: 0,
        }),
      });
      if (r.ok) {
        setActiveId(f.incident_id);
        const body = (await r.json().catch(() => null)) as {
          duplicate?: boolean;
        } | null;
        setFired({
          id: f.incident_id,
          outcome: body?.duplicate ? "duplicate" : "added",
        });
      } else {
        setFired({ id: f.incident_id, outcome: "rejected" });
      }
    } catch {
      setFired({ id: f.incident_id, outcome: "rejected" });
    } finally {
      setFiring(null);
    }
  }, []);

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between gap-4 border-b border-nexus-border bg-nexus-panel px-5 py-2.5">
        <div className="flex items-baseline gap-3">
          <h1 className="text-[14px] font-bold tracking-tight text-nexus-text">
            NexusOps
          </h1>
          <span className="text-[12px] text-nexus-muted">
            incident response console
          </span>
        </div>
        <nav className="flex gap-1 rounded-lg border border-nexus-border bg-nexus-bg p-0.5">
          {(["live", "history"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`rounded-md px-4 py-1.5 text-[12px] font-medium transition-colors ${
                tab === t
                  ? "bg-nexus-panel text-nexus-text shadow-sm"
                  : "text-nexus-muted hover:text-nexus-text"
              }`}
            >
              {t === "live" ? "Live" : "History"}
            </button>
          ))}
        </nav>
        <span
          className={`flex items-center gap-2 text-[12px] font-medium ${
            connected ? "text-nexus-green" : "text-nexus-amber"
          }`}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              connected ? "bg-nexus-green" : "animate-pulse bg-nexus-amber"
            }`}
          />
          {connected ? "live" : "reconnecting…"}
          {status?.mode && (
            <span
              className={`ml-1 rounded border px-1.5 py-px text-[9.5px] font-bold tracking-wide ${
                status.mode === "smoke"
                  ? "border-nexus-amber/40 bg-nexus-amber/10 text-nexus-amber"
                  : "border-nexus-green/40 bg-nexus-green/10 text-nexus-green"
              }`}
            >
              {status.mode === "smoke" ? "SMOKE" : "REAL"}
            </span>
          )}
        </span>
      </header>

      {tab === "live" ? (
        <div className="grid min-h-0 flex-1 grid-cols-[320px_1fr] gap-4 p-4">
          <aside className="flex min-h-0 flex-col gap-4">
            <StatusStrip status={status} />
            <IncidentPicker
              fixtures={fixtures}
              firing={firing}
              fired={fired}
              onFire={fire}
              activeId={active}
              byIncident={byIncident}
              onSelect={setActiveId}
              gatesWaiting={status?.gates_waiting ?? []}
              queueDepth={status?.queue_depth ?? 0}
            />
          </aside>
          <main className="min-h-0 overflow-y-auto">
            {active ? (
              <>
                <Timeline incidentId={active} events={activeEvents} queued={queued} />
                <GatePanel
                  incidentId={active}
                  events={activeEvents}
                  decide={decide}
                />
              </>
            ) : (
              <div className="grid h-full place-items-center">
                <div className="max-w-sm text-center">
                  <p className="text-[14px] font-medium text-nexus-text">
                    No incident is running.
                  </p>
                  <p className="mt-1 text-[12.5px] leading-relaxed text-nexus-muted">
                    Pick an alert from the list on the left and press{" "}
                    <span className="font-semibold text-nexus-text">fire live</span>{" "}
                    to drive one cycle end to end — you hold the gate.
                  </p>
                </div>
              </div>
            )}
          </main>
        </div>
      ) : (
        <div className="min-h-0 flex-1 p-4">
          <iframe
            src="/film/"
            title="NexusOps — recorded runs (history)"
            className="h-full w-full rounded-lg border border-nexus-border bg-nexus-panel"
          />
        </div>
      )}
    </div>
  );
}