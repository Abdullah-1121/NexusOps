"""Real scoped GitHub rollback — design D-10, FR-9, requirement §5.2.

Replaces the mock body of `trigger_github_rollback` (previously
"performed (mock)") with a REAL GitHub REST call: create a rollback TAG
(`refs/tags/rollback-<id>-<ts>`) pointing at the last-known-good commit on an
env-configured THROWAWAY repo, then verify the tag via the API. The demo claim
becomes true — "it actually rolled back" — while no production history is ever
rewritten and nothing destructive can reach a real system.

Why a TAG and not a revert COMMIT: a tag is a bookmark on an existing commit.
It marks the recovery point without changing a single file or erasing history —
reversible, verifiable, the standard CD "rollback marker". A revert commit
(a real new commit that undoes the bad release's files) is the heavier
B-roadmap upgrade: it can conflict and writes actual code. Tag first.

Stdlib `urllib` only (zero new dependencies; ponytail). Every failure is LOUD —
RollbackError surfaces as ToolError in the MCP server and routes to D-6
manual_review. Never a silent "performed".

Env (gitignored `.env`, loaded via app.models._load_env_file):
  NEXUSOPS_GITHUB_REPO            required  "owner/repo" of the throwaway repo
  NEXUSOPS_GITHUB_TOKEN           required  fine-grained PAT scoped to that repo
  NEXUSOPS_GITHUB_LAST_GOOD_SHA   optional  default = repo default-branch tip
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.models import _load_env_file

_load_env_file()  # idempotent (setdefault); harmless if already loaded

API_BASE = "https://api.github.com"
TIMEOUT_S = 15


class RollbackError(Exception):
    """Loud, anticipated rollback failure. Mapped to ToolError by mcp_server."""


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RollbackError(f"missing required env {name} — refusing to roll back silently")
    return value


def _request(method: str, path: str, *, token: str, body: dict[str, Any] | None = None) -> tuple[int, dict]:
    """One GitHub REST call. Raw HTTP status surfaces to the caller (ponytail:
    no SDK layer between us and the failure shape we must map loud)."""
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "nexusops-demo",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        parsed: dict = {}
        try:
            parsed = json.loads(exc.read())
        except Exception:
            pass
        return exc.code, parsed
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RollbackError(f"GitHub API unreachable ({method} {path}): {exc}") from exc


def _last_known_good_sha(repo: str, token: str) -> str:
    """Recovery point for the tag: env override, else the repo's live
    default-branch tip (a real, verifiable commit — the honest fallback)."""
    pinned = os.environ.get("NEXUSOPS_GITHUB_LAST_GOOD_SHA", "").strip()
    if pinned:
        return pinned
    status, repo_info = _request("GET", f"/repos/{repo}", token=token)
    if status != 200:
        raise RollbackError(f"could not resolve repo {repo} (HTTP {status}): {repo_info}")
    branch = repo_info.get("default_branch")
    status, ref = _request("GET", f"/repos/{repo}/git/ref/heads/{branch}", token=token)
    if status != 200:
        raise RollbackError(f"could not resolve default branch {branch!r} (HTTP {status}): {ref}")
    sha = ref.get("object", {}).get("sha")
    if not sha:
        raise RollbackError(f"no commit sha for branch {branch!r}: {ref}")
    return sha


def perform_github_rollback(commit_sha: str, tag: str | None = None) -> dict[str, Any]:
    """Create and verify a rollback tag on the configured throwaway repo.

    `commit_sha` is the gate key (the sha the human approved, NG-1) and, when
    no explicit `tag` is given, the tag-name seed. The tag itself points at
    the last-known-good commit — the recovery point, never a destructive write.

    Returns §5.2 performed result extended with the created tag's details.
    """
    repo = _env("NEXUSOPS_GITHUB_REPO")
    token = _env("NEXUSOPS_GITHUB_TOKEN")
    owner, _, repo_name = repo.partition("/")
    if not owner or not repo_name:
        raise RollbackError(f"NEXUSOPS_GITHUB_REPO must be 'owner/repo', got {repo!r}")

    # Gate is NOT here: it lives in mcp_server before this is ever called (NG-1).
    target = _last_known_good_sha(repo, token)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = tag or f"rollback-{commit_sha[:10]}-{stamp}"

    status, body = _request(
        "POST",
        f"/repos/{repo}/git/refs",
        token=token,
        body={"ref": f"refs/tags/{tag}", "sha": target},
    )
    if status != 201:
        raise RollbackError(
            f"rollback tag create failed (HTTP {status}): {body.get('message', body)}"
        )

    status, ref = _request("GET", f"/repos/{repo}/git/ref/tags/{tag}", token=token)
    if status != 200:
        raise RollbackError(f"rollback tag verification failed (HTTP {status}): {ref}")

    return {
        "status": "performed",
        "message": f"rollback tag {tag} created on {repo} at {target[:10]}",
        "tag": tag,
        "sha": target,
        "verified": True,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }