import type { SystemStatus } from "../types";

/** System health strip: broker status, queue depth, gates waiting — the
 *  operator's at-a-glance reality (failures are loud and visible, NFR-4). */
export default function StatusStrip({ status }: { status: SystemStatus | null }) {
  const redisOk = status?.redis === "ok";
  return (
    <section className="rounded-lg border border-nexus-border bg-nexus-panel shadow-[0_1px_2px_rgba(16,24,40,0.05)]">
      <h2 className="border-b border-nexus-border px-3.5 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-nexus-muted">
        System
      </h2>
      <div className="divide-y divide-nexus-border text-[12.5px]">
        <div className="flex items-center justify-between px-3.5 py-2">
          <span className="text-nexus-muted">Backend queue</span>
          <span className="flex items-center gap-2">
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                redisOk ? "bg-nexus-green" : "animate-pulse bg-nexus-red"
              }`}
            />
            <span className="font-mono text-[13px] font-semibold text-nexus-text">
              {status ? status.redis : "…"}
            </span>
          </span>
        </div>
        <div className="flex items-center justify-between px-3.5 py-2">
          <span className="text-nexus-muted">Queued incidents</span>
          <span className="font-mono text-[13px] font-semibold text-nexus-text">
            {status?.queue_depth ?? "…"}
          </span>
        </div>
        <div className="flex items-center justify-between px-3.5 py-2">
          <span className="text-nexus-muted">Gates waiting</span>
          <span className="font-mono text-[13px] font-semibold text-nexus-text">
            {status ? status.gates_waiting.length : "…"}
          </span>
        </div>
      </div>
    </section>
  );
}