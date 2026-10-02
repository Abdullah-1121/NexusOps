"""Guard (scripts/run_guard.py) behaviour, proven against a fake console.

The guard is tested as a plain process manager — no Docker required. Each
case spawns `python -m scripts.run_guard <fake console>` with fast knobs
(NEXUSOPS_GUARD_*) and asserts exit codes + supervision-log lines. Docker's
restart policy itself is the supervisor and is verified live in the
acceptance run, not here.

Cases (D-19 / T-8.1):
  * crash(17)    -> the guard mirrors the console's exit code (17)
  * crash(0)     -> voluntary clean stop is mirrored too (0)
  * wedged       -> never answers AND ignores SIGTERM -> the guard escalates
                    SIGTERM -> SIGKILL and exits nonzero, so the container
                    restart policy kicks in
  * manual stop  -> SIGTERM to the guard is forwarded to the console, which
                    drains cleanly; the guard exits 0 (graceful-first)
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAST = {
    # Boot grace stays > 0 on purpose: with zero grace the guard starts
    # tapping before the fake console has bound its socket and force-kills a
    # perfectly healthy process while it is still booting — the exact failure
    # the grace window exists to prevent. 1.5 s covers the fake's import+bind.
    "NEXUSOPS_GUARD_INTERVAL": "0.1",
    "NEXUSOPS_GUARD_PROBE_TIMEOUT": "0.2",
    "NEXUSOPS_GUARD_MAX_MISSES": "2",
    "NEXUSOPS_GUARD_TERM_WAIT": "0.5",
    "NEXUSOPS_GUARD_BOOT_GRACE": "1.5",
}


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def run_guard(fake_args: list[str], timeout: float = 30.0):
    env = dict(os.environ)
    env.update(FAST)
    port = free_port()
    cmd = [
        sys.executable, "-m", "scripts.run_guard",
        "tests.fake_console", "--port", str(port), *fake_args,
    ]
    return subprocess.run(cmd, cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=timeout)


def test_crash_mirrors_exit_code():
    result = run_guard(["--mode", "crash", "--crash-code", "17"])
    assert result.returncode == 17
    assert "console exited with code 17" in result.stdout


def test_voluntary_clean_exit_mirrored():
    result = run_guard(["--mode", "crash", "--crash-code", "0"])
    assert result.returncode == 0


def test_wedged_console_is_escalated_to_sigkill():
    # Never answers (serve_hits=0) and ignores SIGTERM: the only way out is
    # the guard's escalation, and the exit code must be nonzero so the
    # container restart policy sees an abnormal stop.
    result = run_guard(["--mode", "wedged", "--serve-hits", "0"])
    assert result.returncode != 0
    assert "not serving" in result.stdout
    assert "SIGKILL" in result.stdout
    assert "console exited with code" in result.stdout


def test_manual_stop_is_forwarded_and_clean():
    port = free_port()
    env = dict(os.environ)
    env.update(FAST)
    cmd = [
        sys.executable, "-m", "scripts.run_guard",
        "tests.fake_console", "--port", str(port), "--mode", "serve",
    ]
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), 0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("fake console never came up")
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, out
    assert "SIGTERM/SIGINT" in out
    assert "console exited with code 0" in out