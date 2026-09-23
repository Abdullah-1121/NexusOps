#!/usr/bin/env bash
# Probe-guarded benchmark record retry (2026-09-22, provider-neutral since D-9).
# The record consumes the SLM quota (Gemini Flash-Lite, ~500 req/day) but a
# congested window or a quota-exhausted 429 can fail calls; in-process retries
# multiplied the burn until the whole window was dead (2026-09-22 lesson,
# fixed in app/models.py `_quota_exhausted` + retries capped at 2 in benchmark.py).
# Strategy: probe with 1 tiny call; only run the full record when the probe
# answers, else sleep and retry. Stops after MAX_TRIES or when a record with
# >= MIN_ANSWERED incidents completes. Never burns 25 calls on a dead window.
# NOTE: the probe endpoint is NOT hard-coded — it follows NEXUSOPS_LLM_API_URL
# from .env, so both providers (OpenRouter then, Gemini now) work unchanged.
set -u
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
OUT="out10c.json"
MAX_TRIES="${MAX_TRIES:-4}"
MIN_ANSWERED="${MIN_ANSWERED:-8}"
SLEEP_MIN="${SLEEP_MIN:-20}"

probe() {
  curl -s --max-time 30 "$NEXUSOPS_LLM_API_URL/chat/completions" \
    -H "Authorization: Bearer $NEXUSOPS_LLM_API_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$NEXUSOPS_SLM_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_tokens\":3}" \
    | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
items = d if isinstance(d, list) else [d]
if any(isinstance(x, dict) and x.get('error') for x in items):
    sys.exit(1)
choices = d.get('choices') if isinstance(d, dict) else items[0].get('choices') if items else None
sys.exit(0 if choices else 1)
"
}

answered() {
  python3 -c "import json,sys;r=json.load(open('$OUT'))['results'];print(sum(1 for x in r if not x.get('manual_review_reason')))"
}

for i in $(seq 1 "$MAX_TRIES"); do
  echo "[retry] $i/$MAX_TRIES probing $NEXUSOPS_SLM_MODEL ..."
  if probe; then
    echo "[retry] probe OK -> recording"
    .venv/bin/python -u -m scripts.run_live_benchmark --record "$OUT" --limit 10 > live10c_report.json 2> live10c_err.log
    n="$(answered)"
    echo "[retry] record done: $n/10 answered"
    if [ "$n" -ge "$MIN_ANSWERED" ]; then
      echo "[retry] good record -> stop"
      exit 0
    fi
  else
    echo "[retry] probe failed (503/429 window) -> sleep ${SLEEP_MIN}m"
  fi
  [ "$i" -lt "$MAX_TRIES" ] && sleep "$((SLEEP_MIN * 60))"
done
echo "[retry] gave up after $MAX_TRIES tries"