"""Feature 6 acceptance tests (D-10 rollback backend + FR-9 film player).

The rollback backend must: refuse loudly when env config is missing, create
+ verify a rollback tag exactly once on approval, and map every API failure
to a loud RollbackError (never a silent "performed"). The player must derive
honest beats (including the recorded outage) and gate live without inventing
data.
"""

import json
import os
from pathlib import Path

import pytest

from app import player
from app.rollback import RollbackError, perform_github_rollback

REPO = "owner/demo"
TOKEN = "tok"


def _record():
    # Minimal checkpoint shape mirroring outG1.json.
    return {
        "results": [
            {
                "incident_id": "c-02",
                "fixture": {
                    "alert": {"service": "db", "source": "prometheus",
                              "severity_hint": "critical", "occurred_at": "2026-09-08T12:00:00Z",
                              "message": "connection pool exhausted"},
                    "ground": {"severity": "critical", "requires_gate": True, "expected_decision": "approve"},
                },
                "severity": "critical",
                "escalated": True,
                "plan": {"severity": "critical", "affected_service": "db",
                         "root_cause_hypothesis": "pool leak",
                         "confidence": 0.9,
                         "remediation_steps": [{"action": "scale"}, {"action": "rollback"}]},
                "plan_ok": True,
                "decision": {"decision": "approve", "actor": "benchmark"},
                "terminal": "resolved",
                "t_gate_ms": 15000.0,
                "t_resolve_ms": 17000.0,
            },
            {
                "incident_id": "a-01",
                "fixture": {"alert": {"service": "auth", "source": "prometheus",
                                      "severity_hint": "info", "occurred_at": "2026-09-08T12:00:00Z",
                                      "message": "something dark"},
                            "ground": {"severity": "info", "requires_gate": True, "expected_decision": "approve"}},
                "severity": "info",
                "escalated": True,
                "plan": None,
                "plan_ok": False,
                "decision": None,
                "manual_review_reason": "FF RCA unavailable: ModelError(... HTTP 503 ...)",
                "terminal": "manual_review",
                "t_gate_ms": None,
                "t_resolve_ms": 45859.0,
            },
            {
                "incident_id": "m-02",
                "fixture": {"alert": {"service": "orphan", "source": "prometheus",
                                      "severity_hint": "info", "occurred_at": "2026-09-08T12:00:00Z",
                                      "message": "gone dark"},
                            "ground": {"severity": "info", "requires_gate": True, "expected_decision": "reject"}},
                "severity": "info",
                "escalated": False,
                "plan": {"severity": "info", "affected_service": "orphan",
                         "root_cause_hypothesis": "source decommissioned",
                         "confidence": 0.95,
                         "remediation_steps": [{"action": "noop"}]},
                "plan_ok": True,
                "decision": {"decision": "reject", "actor": "benchmark"},
                "terminal": "rejected",
                "t_gate_ms": 6600.0,
                "t_resolve_ms": 6800.0,
            },
        ],
        "sink": {"model_calls": []},
        "fixtures": {},
    }


class TestRollbackBackend:
    def test_missing_env_is_loud_not_silent(self, monkeypatch):
        monkeypatch.delenv("NEXUSOPS_GITHUB_REPO", raising=False)
        monkeypatch.delenv("NEXUSOPS_GITHUB_TOKEN", raising=False)
        with pytest.raises(RollbackError, match="missing required env"):
            perform_github_rollback("abc123")

    def test_repo_must_be_owner_slash_repo(self, monkeypatch):
        monkeypatch.setenv("NEXUSOPS_GITHUB_REPO", "not-a-slash")
        monkeypatch.setenv("NEXUSOPS_GITHUB_TOKEN", TOKEN)
        with pytest.raises(RollbackError, match="must be 'owner/repo'"):
            perform_github_rollback("abc123")

    def test_tag_create_failure_maps_loud(self, monkeypatch):
        monkeypatch.setenv("NEXUSOPS_GITHUB_REPO", REPO)
        monkeypatch.setenv("NEXUSOPS_GITHUB_TOKEN", TOKEN)
        monkeypatch.setenv("NEXUSOPS_GITHUB_LAST_GOOD_SHA", "t0good")

        def fake_request(method, path, **kw):
            return 422, {"message": "Reference already exists"}

        monkeypatch.setattr("app.rollback._request", fake_request)
        with pytest.raises(RollbackError, match="failed"):
            perform_github_rollback("abc123")

    def test_happy_path_creates_and_verifies_tag(self, monkeypatch):
        monkeypatch.setenv("NEXUSOPS_GITHUB_REPO", REPO)
        monkeypatch.setenv("NEXUSOPS_GITHUB_TOKEN", TOKEN)
        calls = []

        def fake_request(method, path, **kw):
            calls.append((method, path))
            if method == "POST" and path.endswith("/git/refs"):
                return 201, {"ref": "refs/tags/x", "object": {"sha": "t0"}}
            if method == "GET" and "/git/ref/tags/" in path:
                return 200, {"ref": path, "object": {"sha": "t0"}}
            if path == f"/repos/{REPO}":
                return 200, {"default_branch": "main"}
            if path == f"/repos/{REPO}/git/ref/heads/main":
                return 200, {"object": {"sha": "t0"}}
            raise AssertionError(f"unexpected {method} {path}")

        monkeypatch.setattr("app.rollback._request", fake_request)
        result = perform_github_rollback("abc123", tag="rollback-c-02-live")
        assert result["status"] == "performed"
        assert result["verified"] is True
        assert result["tag"] == "rollback-c-02-live"
        assert result["sha"] == "t0"
        assert [m for m, _ in calls] == ["GET", "GET", "POST", "GET"]


class TestFilmPlayer:
    def test_beats_are_honest_for_manual_review(self):
        raw = [r for r in _record()["results"] if r["incident_id"] == "a-01"][0]
        assert raw["plan"] is None
        beats = player.derive_beats(raw)
        phases = [b["phase"] for b in beats]
        assert phases == ["alert", "severity", "evidence", "plan", "terminal"]
        plan_beat = beats[3]
        assert "FAILED" in plan_beat["title"]
        assert "HTTP 503" in plan_beat["detail"]
        # honest beat: the system admits producing no plan — it never claims success
        assert "no plan" in plan_beat["body"].lower()

    def test_beats_include_gate_and_rollback_phases_for_star(self):
        raw = [r for r in _record()["results"] if r["incident_id"] == "c-02"][0]
        beats = player.derive_beats(raw)
        phases = [b["phase"] for b in beats]
        assert "gate" in phases
        assert "decision" in phases
        assert player.plan_needs_rollback(raw) is True

    def test_player_api_gate_and_reject(self):
        raw = json.loads(json.dumps(_record()))

        from fastapi.testclient import TestClient

        client = TestClient(player.create_player_app(raw))

        r = client.post("/api/film/m-02/decision", json={"decision": "reject", "actor": "tester"})
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"
        # no rollback_plan ⇒ approve is harmless but honest
        r = client.post("/api/film/m-02/decision", json={"decision": "approve", "actor": "tester"})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"
        assert "nothing to revert" in r.json()["message"]

        # manual-review incident has no gate ⇒ 409, no decision possible
        r = client.post("/api/film/a-01/decision", json={"decision": "approve", "actor": "tester"})
        assert r.status_code == 409

    def test_player_api_approve_executes_real_rollback(self, monkeypatch):
        raw = json.loads(json.dumps(_record()))
        monkeypatch.setenv("NEXUSOPS_GITHUB_REPO", REPO)
        monkeypatch.setenv("NEXUSOPS_GITHUB_TOKEN", TOKEN)
        from app.mcp_server import _APPROVED

        _APPROVED.clear()
        created = []

        def fake_request(method, path, **kw):
            if method == "POST" and path.endswith("/git/refs"):
                created.append(path)
                return 201, {"ref": "refs/tags/x", "object": {"sha": "t0"}}
            if method == "GET" and "/git/ref/tags/" in path:
                return 200, {"ref": path, "object": {"sha": "t0"}}
            if path == f"/repos/{REPO}":
                return 200, {"default_branch": "main"}
            if path == f"/repos/{REPO}/git/ref/heads/main":
                return 200, {"object": {"sha": "t0"}}
            return 404, {}

        monkeypatch.setattr("app.rollback._request", fake_request)

        from fastapi.testclient import TestClient

        client = TestClient(player.create_player_app(raw))
        r = client.post("/api/film/c-02/decision", json={"decision": "approve", "actor": "tester"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "performed"
        assert body["verified"] is True
        assert created, "approve on a rollback-worthy plan must hit the API"
        # the film used the SAME approval chain as production (NG-1 registration)
        assert "demo-c-02" in _APPROVED

    def test_player_api_missing_config_is_loud(self, monkeypatch):
        raw = json.loads(json.dumps(_record()))
        monkeypatch.delenv("NEXUSOPS_GITHUB_REPO", raising=False)

        from fastapi.testclient import TestClient

        client = TestClient(player.create_player_app(raw))
        r = client.post("/api/film/c-02/decision", json={"decision": "approve", "actor": "tester"})
        assert r.status_code == 502
        assert "missing required env" in r.json()["detail"]

    def test_player_works_mounted_under_film_history_tab(self):
        """Regression: serve.py mounts the player at /film, so its API lives at
        /film/api/... The embedded JS must use RELATIVE fetch paths — absolute
        /api/... resolved to the server root and 404'd, crashing the History
        tab with 'films.map is not a function'."""
        raw = json.loads(json.dumps(_record()))

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        host = FastAPI()
        host.mount("/film", player.create_player_app(raw))
        client = TestClient(host)

        # the film page is served under the mount...
        page = client.get("/film/")
        assert page.status_code == 200
        assert "demonstration film" in page.text
        # ...and every fetch the page makes is relative (resolves under /film)
        for bad in ('fetch("/api/', "fetch(`/api/"):
            assert bad not in page.text, f"absolute API fetch regressed: {bad}"

        # the API is reachable at the mounted prefix, not the server root
        root = client.get("/api/films")
        assert root.status_code == 404
        films = client.get("/film/api/films")
        assert films.status_code == 200
        assert any(f["incident_id"] == "c-02" for f in films.json())

        # decisions flow through the mounted prefix too
        detail = client.get("/film/api/film/c-02")
        assert detail.status_code == 200
        r = client.post(
            "/film/api/film/m-02/decision", json={"decision": "reject", "actor": "tester"}
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"


def test_importing_player_without_checkpoint_never_crashes(tmp_path):
    """D-19 container surfaced a latent bug: app.player created its default
    app at IMPORT time, and create_player_app() reads outG1.json — so merely
    importing the module crashed on any machine without the dev-produced
    checkpoint (the Docker image, a clean checkout). serve.py imports
    app.player to mount /film, so one missing consumable killed the whole
    console. The module-level app must degrade to an honest empty stub.

    Regression pinned in a subprocess with a throwaway CWD and no checkpoint
    env — the exact container condition — not a monkeypatched import (the
    module is already imported at the top of this file)."""
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "NEXUSOPS_CHECKPOINT"}
    code = (
        "import sys; sys.path.insert(0, {root!r})\n"
        "import app.player\n"
        "assert app.player._checkpoint_available() is False\n"
        "from fastapi.testclient import TestClient\n"
        "r = TestClient(app.player.app).get('/')\n"
        "assert r.status_code == 200, r.status_code\n"
        "assert 'No recorded checkpoint yet' in r.text\n"
        "print('player import ok')\n"
    ).format(root=str(repo_root))
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "player import ok" in result.stdout