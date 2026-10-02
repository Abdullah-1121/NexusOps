"""Stand-in for the real console, used by tests/test_guard.py.

Deliberately tiny: just enough of an HTTP surface that the guard's probe has
something real to tap, plus scripted failure behaviours. This is a TEST
harness file, not production code.

Modes:
  serve        answer every request with 200; on SIGTERM drain cleanly and
               exit 0 (a healthy console on `docker compose stop`)
  crash:<code> exit with <code> after a beat (a console that died on its own)
  wedged       answer the first N requests, then accept connections but never
               respond, and IGNORE SIGTERM (a truly wedged console: it can
               neither serve nor drain — only the guard's force-kill can end
               it, exactly like a hung event loop in the real app)
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS: argparse.Namespace
_hits = 0
_lock = threading.Lock()
_stop = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # the guard's probe hits /api/status
        global _hits
        with _lock:
            _hits += 1
            n = _hits
        if ARGS.mode == "wedged" and n > ARGS.serve_hits:
            time.sleep(9999)  # wedged: connection accepted, never answered
            return
        body = b'{"redis":"ok","queue_depth":0}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:  # keep test output quiet
        pass


def _graceful_stop(_signum: int, _frame: object) -> None:
    # Instant clean stop: the REAL console's drain takes up to its 10 s
    # lifespan budget — that is exactly what the guard's TERM_WAIT is for. The
    # dummy is here to test the GUARD, so its graceful stop is just exit 0
    # (no HTTP-server teardown: serve_forever's 0.5 s poll cycle would race
    # the test's fast TERM_WAIT and flake, as it did in the suite run). The
    # daemon serve thread dies with the process.
    sys.exit(0)


def main() -> None:
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument("--mode", choices=["serve", "crash", "wedged"],
                        default="serve")
    parser.add_argument("--crash-code", type=int, default=1)
    parser.add_argument("--serve-hits", type=int, default=2)
    ARGS = parser.parse_args()

    if ARGS.mode == "crash":
        # A crashed process doesn't linger politely — exit immediately. The
        # guard must mirror the code on its very next check, no waiting.
        sys.exit(ARGS.crash_code)

    if ARGS.mode == "serve":
        signal.signal(signal.SIGTERM, _graceful_stop)
        signal.signal(signal.SIGINT, _graceful_stop)
    else:  # wedged: cannot even honour a stop request
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    server = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"fake console (mode={ARGS.mode}) listening on {ARGS.port}",
          flush=True)
    # SIGTERM handler exits 0 (serve mode) or is ignored (wedged mode), so
    # this wait is the serve-mode process's indefinite "running" state — the
    # exit happens inside the handler, on the main thread.
    _stop.wait()


if __name__ == "__main__":
    main()