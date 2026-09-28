/** Shared types for the console — mirrors the serve-process event vocabulary
 *  (D-11: ingest, classified, escalating, tool_call, plan, gate_open,
 *  decision, rollback, done, manual_review, error). */

export interface FixtureIncident {
  incident_id: string;
  occurred_at: string;
  source: string;
  service: string;
  message: string;
  severity_hint: "critical" | "warning" | "info" | null;
}

export interface SystemStatus {
  redis: "ok" | "unreachable";
  queue_depth: number | null;
  gates_waiting: string[];
  /** runtime label: "smoke" = deterministic fakes, "real" = live model quota (B1) */
  mode?: string;
}

export interface RemediationStep {
  action: string;
  target?: string;
  reason?: string;
}

export interface Plan {
  root_cause_hypothesis?: string;
  confidence?: number;
  severity?: string;
  affected_service?: string;
  remediation_steps?: RemediationStep[];
  requires_approval?: boolean;
}

export interface NexusEvent {
  type: string;
  incident_id: string;
  seq: number;
  [key: string]: unknown;
}

/** One record returned by an evidence tool (D-12/B1: `app/evidence.py` tags
 *  each record with its origin tool; the payload rides the `tool_call` event
 *  in full — the console merely stopped hiding it). */
export interface EvidenceItem {
  /** origin tool name, e.g. "fetch_service_logs" (absent in older runs) */
  _tool?: string;
  [key: string]: unknown;
}

/** Stage timing + model provenance stamped by serve onto milestones (B1). */
export interface StageMetaFields {
  /** wall time for this stage, computed from chunk arrival deltas */
  stage_duration_ms?: number;
  /** model env used for model-bearing stages (SLM "NEXUSOPS_SLM_MODEL" / FF) */
  model_env?: string;
}

export type Tab = "live" | "history";