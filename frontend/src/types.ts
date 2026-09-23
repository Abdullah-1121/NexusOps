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

export type Tab = "live" | "history";