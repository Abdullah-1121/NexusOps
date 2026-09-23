#!/usr/bin/env bash
# Probe-guarded benchmark JUDGE retry (2026-09-22).
# The judge consumes the scarce Frontier quota (Gemini Flash = 20 req/day; a
# full 10-incident pass is ~10 judge + 1 probe). In a congested window the
# judge call can 503 repeatedly, and in-process retries multiplied the burn
# until the whole day's quota was exhausted (the 2026-09-22 lesson, fixed in
# app/models.py `_quota_exhausted` + retries capped at 2 in benchmark.py).
#
# Strategy, mirroring retry_record.sh: probe with 1 tiny call; only run the
# judge pass when the probe answers and quota remains; else sleep and retry.
# NEVER re-runs the pipeline — judge_checkpoint replays the saved record.
set -u
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
REC="${1:-outG1.json}"
REPORT="judgeG1_report.json"
MAX_TRIES="${MAX_TRIES:-6}"
MIN_GRADED="${MIN_GRADED:-8}"
SLEEP_MIN="${SLEEP_MIN:-15}"

# Probe the FRONTIER model (the one the judge calls) with a 1-token answer.
# A quota-exhausted 429 now reads as non-retryable -> probe fails -> we sleep
# until the daily reset instead of burning the pass on a dead day.
# Robustness (2026-09-23): Gemini's OpenAI-compat shim returns the healthy body
# as an OBJECT {choices:...} but a 503 as an ARRAY [{error:...}], and a healthy
# answer can carry empty `content` (thinking model, finish_reason=length). The
# old check required truthy content -> healthy windows read as dead. This probe
# accepts any body with a `choices` array and only fails on an explicit error.
probe() {
  curl -s --max-time 30 "$NEXUSOPS_LLM_API_URL/chat/completions" \
    -H "Authorization: Bearer $NEXUSOPS_LLM_API_KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$NEXUSOPS_FRONTIER_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_tokens\":3}" \
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

graded() {
  python3 -c "
import json,sys
d=json.load(open('$REPORT'))
print(d.get('graded', 0))
"
}

for i in $(seq 1 "$MAX_TRIES"); do
  echo "[judge-retry] $i/$MAX_TRIES probing $NEXUSOPS_FRONTIER_MODEL ..."
  if probe; then
    echo "[judge-retry] probe OK -> judging $REC"
    PYTHONPATH=. .venv/bin/python -u scripts/run_live_benchmark.py --judge "$REC" > "$REPORT" 2> judgeG1_err.log
    n="$(graded)"
    echo "[judge-retry] judge pass done: $n incidents graded"
    if [ "$n" -ge "$MIN_GRADED" ]; then
      echo "[judge-retry] clean pass -> stop"
      exit 0
    fi
    echo "[judge-retry] pass incomplete ($n < $MIN_GRADED) -> sleep ${SLEEP_MIN}m"
  else
    echo "[judge-retry] probe failed (congested or daily quota) -> sleep ${SLEEP_MIN}m"
  fi
  [ "$i" -lt "$MAX_TRIES" ] && sleep "$((SLEEP_MIN * 60))"
done
echo "[judge-retry] gave up after $MAX_TRIES tries"
exit 1