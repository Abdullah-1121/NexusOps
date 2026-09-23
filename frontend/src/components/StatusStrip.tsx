import type { SystemStatus } from "../types";

/** System health strip: broker status, queue depth, gates waiting — the
 *  operator's at-a-glance reality (failures are loud and visible, NFR-4). */
export default function StatusStrip({ status }: { status: SystemStatus | null }) {
  const redisOk = status?.redis === "ok";
  return (
    <section className="rounded-lg border border-nexus-border bg-nexus-panel p-3">
      <h2 className="mb-2 text-[11px] font-medium uppercase tracking-widest text-nexus-muted">
        System
      </h2>
      <div className="flex flex-col gap-2 text-xs">
        <div className="flex items-center justify-between">
          <span className="text-nexus-muted">Redis</span>
          <span className="flex items-center gap-1.5">
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                redisOk ? "bg-nexus-green" : "bg-nexus-red animate-pulse"
              }`}
            />
            {status ? status.redis : "…"}
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-nexus-muted">Queue depth</span>
          <span className="font-mono text-nexus-text">
            {status?.queue_depth ?? "…"}
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-nexus-muted">Gates waiting</span>
          <span className="font-mono text-nexus-text">
            {status ? status.gates_waiting.length : "…"}
          </span>
        </div>
      </div>
    </section>
  );
}