#!/usr/bin/env bash
# Probe-guarded benchmark record retry (2026-09-22).
# Free-tier Nvidia models 503 "provider overloaded" in congested windows.
# Strategy: probe with 1 tiny call; only run the full record when the probe
# answers, else sleep and retry. Stops after MAX_TRIES or when a record with
# >= MIN_ANSWERED incidents completes. Never burns 25 calls on a dead window.
set -u
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
OUT="out10c.json"
MAX_TRIES="${MAX_TRIES:-4}"
MIN_ANSWERED="${MIN_ANSWERED:-8}"
SLEEP_MIN="${SLEEP_MIN:-20}"

probe() {
  curl -s --max-time 30 https://openrouter.ai/api/v1/chat/completions \
    -H "Authorization: Bearer $NEXUSOPS_LLM_API_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$NEXUSOPS_SLM_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_tokens\":3}" \
    | python3 -c "import json,sys;d=json.load(sys.stdin);sys.exit(0 if 'choices' in d and d['choices'][0].get('message',{}).get('content') else 1)"
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