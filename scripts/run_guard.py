"""The lifeguard for the live console (production-hardening T-8.1, D-19).

    python -m scripts.run_guard scripts.run_console --port 8137 --host 0.0.0.0 --slm-rca

Contract: the FIRST argument after ``scripts.run_guard`` is the child module
to supervise, and every argument after that is passed to the child verbatim.
The child is always spawned as ``python -m <module> <rest...>`` — the same
shape in tests and production (tests supervise ``tests.fake_console``).

Why the module is explicit (Phase-4 finding 5, live-demo-caught): the first
cut passed *everything* through, assuming argv[1] was already the child
invocation. That held for a script path in tests but silently produced
``python --port 8137 ...`` in production — ``-m scripts.run_guard`` consumes
the module form, so the child's own ``-m`` must be re-spelled. Explicit
module = identical shape everywhere + zero guessing.

What it does, in plain words:
  1. Starts the console as a child process (same environment — secrets flow
     through untouched; the guard never reads or logs them).
  2. Every tick, taps ``GET /api/status`` with a hard timeout and asks ONE
     question: "did the console answer?" ANY HTTP response counts as healthy —
     including a truthful ``{"redis": "unreachable"}`` 200 — because an app
     that answers honestly is alive, and the Redis sidecar in the compose
     stack self-heals. Only a TIMEOUT (event loop wedged, nothing answering)
     counts as a miss.
  3. After N consecutive misses the guard tells the console to stop
     (SIGTERM), waits for it to drain (the app's lifespan budget is 10 s),
     then force-kills it (SIGKILL) if it is still stuck — the same two-step
     escalation used by humans on the D-11 postmortem-2 PIDs, now automated.
  4. The guard mirrors the console's exit code and exits. It NEVER restarts
     in-process: restarting is the supervisor's job (Docker ``restart:
     unless-stopped`` here, a dev-shell loop, or a systemd unit later). The
     container exits because the guard does, and the policy brings it back.

One hard rule, for future readers: NEVER run more than one console worker.
The queue consumer lives in-process on the FastAPI lifespan (serve.py), so
two workers would double-consume Redis. Availability comes from the restart
policy, never from scaling.

Behaviour knobs are environment variables so tests can run the whole
escalation in under a second:
  NEXUSOPS_GUARD_INTERVAL     seconds between probes          (default 5.0)
  NEXUSOPS_GUARD_PROBE_TIMEOUT seconds before a tap times out (default 3.0)
  NEXUSOPS_GUARD_MAX_MISSES   consecutive misses before killing (default 3)
  NEXUSOPS_GUARD_TERM_WAIT    seconds to wait for a graceful drain
                              before SIGKILL                    (default 10.0)
  NEXUSOPS_GUARD_BOOT_GRACE   seconds after start during which misses are
                              not counted (console import + boot is slow,
                              nothing is killed while it is still coming up)
                                                                (default 30.0)
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip())
    except ValueError:
        return default


def _probe_port(argv: list[str]) -> int:
    """The probe taps 127.0.0.1:<port> where <port> is the console's own
    `--port` (reflected from the passthrough args, so tests can run against a
    dummy console on an ephemeral port instead of the live :8137). Host is
    always loopback: the guard lives in the same container/namespace as the
    console, so `localhost` is correct regardless of the app's bind host.
    """
    try:
        return int(argv[argv.index("--port") + 1])
    except (ValueError, IndexError):
        return 8137


INTERVAL = _env_float("NEXUSOPS_GUARD_INTERVAL", 5.0)
PROBE_TIMEOUT = _env_float("NEXUSOPS_GUARD_PROBE_TIMEOUT", 3.0)
MAX_MISSES = int(_env_float("NEXUSOPS_GUARD_MAX_MISSES", 3.0))
TERM_WAIT = _env_float("NEXUSOPS_GUARD_TERM_WAIT", 10.0)
BOOT_GRACE = _env_float("NEXUSOPS_GUARD_BOOT_GRACE", 30.0)


def log(msg: str) -> None:
    print(f"[guard {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def probe_ok(url: str, timeout: float) -> bool:
    """ONE question: did the console answer within the timeout?

    Any HTTP response — including a 4xx/5xx — means the app is serving and
    honest (serve.py returns 200 even when Redis is down, but a 503 would be
    equally truthful). Only a failure to receive ANY response (timeout,
    connection refused, DNS) means the event loop is wedged and not serving.
    The broad except is intentional: every way to receive NO response is a
    miss — there is no exception we would want to treat differently.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as _resp:
            return True
    except urllib.error.HTTPError:
        return True  # an HTTP response arrived — the app is serving
    except Exception:
        return False  # no response at all — not serving


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        log(__doc__)
        sys.exit(2)

    child_cmd = [sys.executable, "-m", sys.argv[1], *sys.argv[2:]]
    log(f"starting console: {' '.join(child_cmd)}")
    try:
        child = subprocess.Popen(child_cmd)
    except OSError as e:  # e.g. missing module / bad interpreter
        log(f"CRITICAL: could not start console: {e}")
        sys.exit(1)

    shutdown_requested_at: float | None = None
    started_at = time.monotonic()
    misses = 0

    def _request_stop(signum: int, _frame) -> None:
        nonlocal shutdown_requested_at
        if shutdown_requested_at is None and child.poll() is None:
            log(f"SIGTERM/SIGINT ({signum}) received — forwarding to console")
            # Phase-4 verdict: forwarding INSIDE the handler is deliberate.
            # `docker stop` gives tini a 10 s budget; if we only set a flag and
            # the main loop slept up to a full tick before acting, a draining
            # console could blow that budget and be SIGKILLed mid-drain. The
            # inner poll()+flag guard makes a double signal harmless.
            child.terminate()
            shutdown_requested_at = time.monotonic()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    probe_url = f"http://127.0.0.1:{_probe_port(sys.argv[1:])}/api/status"

    while True:
        rc = child.poll()
        if rc is not None:
            # Never leave a zombie: poll() has reaped the child (waitpid).
            log(f"console exited with code {rc} — mirroring and exiting")
            sys.exit(rc)

        now = time.monotonic()

        if shutdown_requested_at is not None:
            if now - shutdown_requested_at >= TERM_WAIT:
                log("console still alive after graceful stop — SIGKILL")
                child.kill()
                shutdown_requested_at = now  # restart the clock for the kill
            time.sleep(0.1)
            continue

        if now - started_at < BOOT_GRACE:
            misses = 0  # still coming up — never count boot as wedge
            time.sleep(INTERVAL)
            continue

        if probe_ok(probe_url, PROBE_TIMEOUT):
            misses = 0
        else:
            misses += 1
            log(f"console not serving: probe miss {misses}/{MAX_MISSES}")
            if misses >= MAX_MISSES:
                log("console is not serving — beginning termination")
                shutdown_requested_at = time.monotonic()
                child.terminate()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()