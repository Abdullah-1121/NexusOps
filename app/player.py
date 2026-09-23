"""Feature 6 — Demonstration film: replay player over a recorded checkpoint.

Design D-10 / FR-9. The film is a PLAYER: it reads a recorded benchmark run
(`outG1.json` shape) and derives a per-incident beat timeline
(alert → severity → escalation → evidence → plan → gate → decision →
rollback/terminal) without invoking a single model — deterministic, offline,
zero quota, identical every time.

What is HONEST and what is not:
- The beats are derived from what the checkpoint actually stored
  (fixture.alert, recorded severity, recorded plan, timings, terminal state,
  manual-review reason). Nothing is invented: a recorded outage renders as
  `manual_review` with the recorded 503 reason, never dressed up as resolved.
- The evidence stage ran in the pipeline (MCP tools, FR-2) and is recorded in
  the OpenTelemetry TRACE (FR-7) — not in the checkpoint. The player says so
  instead of fabricating per-call args.
- The GATE beat is the film's live thesis moment (NG-1): the player PAUSES and
  the operator approves/rejects through the same decision contract as
  production (§5.4). On an approved rollback-worthy plan, the REAL scoped
  GitHub rollback (D-10) executes and the created tag is shown. No approval ⇒
  no API call, ever.

Run: `uvicorn app.player:app` (reads `NEXUSOPS_CHECKPOINT` or `outG1.json`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from app.mcp_server import approve_rollback, trigger_github_rollback
from app.rollback import RollbackError

CHECKPOINT_DEFAULT = "outG1.json"

# The pipeline's evidence tools (FR-2/§5.2) — shown as a named stage, not
# fabricated per-call arguments the checkpoint does not store.
EVIDENCE_TOOLS = ("fetch_service_logs", "query_prometheus_metrics")


def load_checkpoint(path: str | None = None) -> dict:
    path = path or os.environ.get("NEXUSOPS_CHECKPOINT", CHECKPOINT_DEFAULT)
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise RuntimeError(
            f"checkpoint {path!r} not found — record a run first (retry_record.sh) "
            f"or set NEXUSOPS_CHECKPOINT"
        )


def _fmt_ms(ms: float | None) -> str:
    return f"{ms/1000:.1f}s" if ms is not None else "—"


def derive_beats(incident: dict) -> list[dict]:
    """Derive the honest beat timeline for one recorded incident."""
    fixture = incident.get("fixture", {})
    alert = fixture.get("alert", {})
    ground = fixture.get("ground", {})
    plan = incident.get("plan")
    beats: list[dict] = []

    beats.append(
        {
            "phase": "alert",
            "title": "Alert lands",
            "body": f"{alert.get('service')} / {alert.get('source')} — {alert.get('message')}",
            "detail": f"incident {incident.get('incident_id')} at {alert.get('occurred_at')} "
                      f"(severity hint: {alert.get('severity_hint')})",
        }
    )

    esc = "escalated to the frontier model (D-4)" if incident.get("escalated") else "handled by the SLM, no escalation (D-4)"
    beats.append(
        {
            "phase": "severity",
            "title": "Severity classified",
            "body": f"recorded severity: {incident.get('severity')} — {esc}",
            "detail": f"ground truth expected: {ground.get('severity')}",
        }
    )

    beats.append(
        {
            "phase": "evidence",
            "title": "Evidence gathered",
            "body": f"pipeline called MCP tools {', '.join(EVIDENCE_TOOLS)} (FR-2)",
            "detail": "tool calls + results are recorded in the OpenTelemetry trace (FR-7); "
                      "this checkpoint stores outcomes, not per-call arguments.",
        }
    )

    if plan is not None:
        steps = plan.get("remediation_steps", [])
        actions = ", ".join(s.get("action", "?") for s in steps) or "—"
        beats.append(
            {
                "phase": "plan",
                "title": "Analysis — remediation plan",
                "body": plan.get("root_cause_hypothesis", ""),
                "detail": f"confidence {plan.get('confidence')} · actions: {actions} "
                          f"(plan_ok={incident.get('plan_ok')})",
            }
        )
        beats.append(
            {
                "phase": "gate",
                "title": "Human gate: parked for approval",
                "body": "Nothing executes without an explicit human decision (NG-1 / FR-5).",
                "detail": f"staged at the gate in {_fmt_ms(incident.get('t_gate_ms'))} "
                          f"(gate required: {ground.get('requires_gate')})",
            }
        )
    else:
        reason = incident.get("manual_review_reason") or "no plan was produced"
        beats.append(
            {
                "phase": "plan",
                "title": "Analysis FAILED — downgrade to manual review",
                "body": "The frontier model was unavailable, so the system produced NO plan and never guessed.",
                "detail": f"recorded reason: {reason}",
            }
        )

    decision = incident.get("decision") or {}
    if decision:
        beats.append(
            {
                "phase": "decision",
                "title": f"Decision recorded: {decision.get('decision', '?')}",
                "body": f"operator decision was {decision.get('decision')} (actor: {decision.get('actor')}).",
                "detail": f"expected by fixture: {ground.get('expected_decision')}",
            }
        )

    terminal = incident.get("terminal", "?")
    if terminal == "manual_review":
        beats.append(
            {
                "phase": "terminal",
                "title": "Terminal: manual review (honest outage)",
                "body": "The incident was parked for a human — never faked as resolved.",
                "detail": f"t_resolve {_fmt_ms(incident.get('t_resolve_ms'))}",
            }
        )
    else:
        beats.append(
            {
                "phase": "terminal",
                "title": f"Terminal: {terminal}",
                "body": f"recorded terminal state: {terminal}",
                "detail": f"t_resolve {_fmt_ms(incident.get('t_resolve_ms'))}",
            }
        )
    return beats


def plan_needs_rollback(incident: dict) -> bool:
    plan = incident.get("plan")
    if not plan:
        return False
    return any(s.get("action") == "rollback" for s in plan.get("remediation_steps", []))


class _DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    actor: str = "demo-operator"


def create_player_app(checkpoint: dict | None = None) -> FastAPI:
    data = checkpoint if checkpoint is not None else load_checkpoint()
    results = {r["incident_id"]: r for r in data["results"]}
    app = FastAPI(title="NexusOps — Demonstration Film")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return PLAYER_HTML

    @app.get("/api/films")
    async def films() -> list[dict]:
        out = []
        for r in data["results"]:
            fixture = r.get("fixture", {})
            ground = fixture.get("ground", {})
            out.append(
                {
                    "incident_id": r["incident_id"],
                    "service": fixture.get("alert", {}).get("service"),
                    "severity": r.get("severity"),
                    "source": fixture.get("alert", {}).get("source"),
                    "terminal": r.get("terminal"),
                    "decision": (r.get("decision") or {}).get("decision"),
                    "expected": ground.get("expected_decision"),
                    "requires_gate": ground.get("requires_gate"),
                    "rollback_plan": plan_needs_rollback(r),
                    "t_gate_ms": r.get("t_gate_ms"),
                    "t_resolve_ms": r.get("t_resolve_ms"),
                }
            )
        return sorted(out, key=lambda f: f["incident_id"])

    @app.get("/api/film/{incident_id}")
    async def film(incident_id: str) -> dict:
        r = results.get(incident_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"no incident {incident_id!r} in checkpoint")
        return {
            "incident_id": incident_id,
            "beats": derive_beats(r),
            "terminal": r.get("terminal"),
            "can_approve": plan_needs_rollback(r) or r.get("plan") is not None,
            "rollback_plan": plan_needs_rollback(r),
        }

    @app.post("/api/film/{incident_id}/decision")
    async def decide(incident_id: str, body: _DecisionIn) -> dict:
        """Live §5.4 gate: the operator's decision NOW, through the SAME
        contract as production — approve_rollback() records the human approval
        (NG-1), and only then trigger_github_rollback() executes the real
        scoped GitHub rollback (D-10). Reject never calls any API (NG-1)."""
        r = results.get(incident_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"no incident {incident_id!r} in checkpoint")
        if r.get("plan") is None:
            raise HTTPException(
                status_code=409,
                detail="incident ended in manual review — there was no plan, so no gate to decide",
            )
        if body.decision == "reject":
            return {"status": "rejected", "message": "operator rejected — no rollback executed", "actor": body.actor}

        # approve
        if not plan_needs_rollback(r):
            return {"status": "approved", "message": "plan has no rollback action — nothing to revert", "actor": body.actor}
        # Same chain production uses (mcp_server): human approval recorded in the
        # _APPROVED registry (NG-1), then the gated rollback tool runs.
        commit_sha = os.environ.get("NEXUSOPS_GITHUB_ROLLBACK_SHA", f"demo-{incident_id}")
        try:
            approve_rollback(commit_sha)
            result = trigger_github_rollback(commit_sha)
            result["actor"] = body.actor
            return result
        except ToolError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except RollbackError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

    return app


PLAYER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>NexusOps — Demonstration Film</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3}
  header{padding:18px 24px;border-bottom:1px solid #21262d;display:flex;align-items:baseline;gap:12px}
  header h1{font-size:18px;margin:0}
  header small{color:#8b949e}
  main{display:grid;grid-template-columns:280px 1fr;gap:0;min-height:calc(100vh - 64px)}
  #list{border-right:1px solid #21262d;padding:16px;overflow:auto}
  #list h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#8b949e;margin:0 0 10px}
  .film-item{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:9px 10px;border-radius:6px;cursor:pointer;border:1px solid transparent;margin-bottom:6px}
  .film-item:hover{background:#161b22}
  .film-item.active{background:#1f6feb22;border-color:#1f6feb}
  .film-item .id{font-weight:600;color:#79c0ff}
  .chip{font-size:11px;padding:2px 7px;border-radius:10px}
  .chip.resolved{background:#23863633;color:#3fb950}
  .chip.manual_review{background:#da363322;color:#f85149}
  .chip.rejected{background:#8b949e22;color:#8b949e}
  .chip.star{background:#d2992222;color:#d29922}
  #stage{padding:24px 28px;max-width:860px}
  #controls{display:flex;gap:10px;align-items:center;margin:14px 0}
  button{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:7px 14px;cursor:pointer;font-size:14px}
  button:hover{background:#30363d}
  button:disabled{opacity:.4;cursor:default}
  button.primary{background:#238636;border-color:#2ea043}
  button.danger{background:#da3633;border-color:#f85149}
  #beats{display:flex;flex-direction:column;gap:10px}
  .beat{border:1px solid #21262d;border-radius:8px;padding:12px 14px;background:#161b22;display:none}
  .beat.visible{display:block}
  .beat .phase{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#79c0ff;margin-bottom:4px}
  .beat .title{font-weight:600;margin-bottom:4px}
  .beat .detail{color:#8b949e;font-size:13px}
  .beat.gate{border-color:#d29922}
  .square{font-family:ui-monospace,SFMono-Regular,monospace;color:#e6edf3;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px 10px;margin-top:8px;font-size:12px;white-space:pre-wrap}
  #result{margin-top:14px}
  #progress{font-size:12px;color:#8b949e}
</style>
</head>
<body>
<header>
  <h1>NexusOps — demonstration film</h1>
  <small>a deterministic replay of a recorded run · live human gate · real scoped rollback</small>
</header>
<main>
  <div id="list"><h2>Recorded incidents</h2><div id="items"></div></div>
  <div id="stage">
    <div id="controls">
      <button id="btn-first" disabled>⏮</button>
      <button id="btn-prev" disabled>◀</button>
      <button id="btn-play">▶ Play</button>
      <button id="btn-next" disabled>▶</button>
      <button id="btn-last" disabled>⏭</button>
      <span id="progress"></span>
    </div>
    <div id="beats"></div>
    <div id="result"></div>
  </div>
</main>
<script>
const state={incident:null,beats:[],index:-1,playing:false,decided:false};
const $=id=>document.getElementById(id);
const chip=t=>`<span class="chip ${t}">${t}</span>`;

async function loadList(){
  const res=await fetch("/api/films");
  const films=await res.json();
  $("items").innerHTML=films.map(f=>`
    <div class="film-item" data-id="${f.incident_id}" onclick="select('${f.incident_id}')">
      <span class="id">${f.incident_id}</span>
      <span>${f.service}</span>
      ${f.rollback_plan?'<span class="chip star">rollback</span>':''}
      ${chip(f.terminal)}
    </div>`).join("");
}
async function select(id){
  state.incident=id;state.decided=false;state.playing=false;
  document.querySelectorAll(".film-item").forEach(e=>e.classList.toggle("active",e.dataset.id===id));
  const res=await fetch(`/api/film/${id}`);
  const f=await res.json();
  state.beats=f.beats;state.index=-1;$("result").innerHTML="";
  render();
  play();
}
function render(){
  $("beats").innerHTML=state.beats.map((b,i)=>`
    <div class="beat ${b.phase==='gate'?'gate':''}" data-i="${i}">
      <div class="phase">${b.phase}</div>
      <div class="title">${b.title}</div>
      <div class="body">${b.body}</div>
      <div class="detail">${b.detail}</div>
    </div>`).join("");
  $("beats").querySelectorAll(".beat").forEach(e=>e.classList.remove("visible"));
  if(state.index>=0)document.querySelector(`.beat[data-i="${state.index}"]`).classList.add("visible");
  $("btn-prev").disabled=$("btn-first").disabled=state.index<=0;
  $("btn-next").disabled=$("btn-last").disabled=state.index>=state.beats.length-1;
  $("progress").textContent=`beat ${Math.max(0,state.index)+1} / ${state.beats.length}`;
  const gate=state.beats[state.index];
  if(state.index>=0 && state.beats[state.index].phase==="decision" && !state.decided) showLiveGate();
}
function play(){ state.playing=true; $("btn-play").textContent="⏸ Pause"; step(); }
function pause(){ state.playing=false; $("btn-play").textContent="▶ Play"; }
function step(){
  if(!state.playing)return;
  if(state.index>=state.beats.length-1){pause();return;}
  state.index++;
  render();
  const speed=state.beats[state.index].phase==="gate"?2500:1400;
  setTimeout(()=>step(),speed);
}
function showLiveGate(){
  pause();
  const host=`<div class="square">THE GATE IS LIVE · NG-1</div>
    <div style="margin:10px 0">This incident's plan is parked for human approval. You are the operator.</div>
    <button class="primary" id="btn-approve">Approve</button>
    <button class="danger" id="btn-reject">Reject</button>`;
  $("result").innerHTML=host;
  $("btn-approve").onclick=()=>decide("approve");
  $("btn-reject").onclick=()=>decide("reject");
}
async function decide(decision){
  state.decided=true;
  const res=await fetch(`/api/film/${state.incident}/decision`,{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({decision,actor:"demo-operator"})});
  const out=await res.json();
  const src=res.ok?"#3fb950":"#f85149";
  $("result").innerHTML=`<div class="square" style="border-color:${src}">${JSON.stringify(out,null,2)}</div>`;
  $("btn-next").disabled=false;
}
["btn-first","btn-prev","btn-next","btn-last"].forEach(id=>$(id).addEventListener("click",()=>{
  pause();
  const to=id==="btn-first"?0:id==="btn-prev"?state.index-1:id==="btn-last"?state.beats.length-1:state.index+1;
  state.index=Math.max(0,Math.min(to,state.beats.length-1));render();
}));
$("btn-play").addEventListener("click",()=>state.playing?pause():play());
loadList();
</script>
</body>
</html>
"""

app = create_player_app()