#!/usr/bin/env bash
# Stop every non-deleted Simplismart deployment.
# Intended for launchd/cron. Safe to re-run: already-STOPPED is a no-op.
#
# Safety: launchd StartCalendarInterval can fire a missed 11pm job on wake.
# Without a time guard, waking the Mac at 11am would stop daytime GPUs.
# This script only stops during the overnight catch-up window (11pm–8am IST).
# Outside that window it logs and exits 0. Override with FORCE_STOP=1.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CLI="${ROOT}/.venv/bin/simplismart-deploy"
PYTHON="${ROOT}/.venv/bin/python"

# Explicit IST so a Mac timezone change cannot move the night window.
export TZ="Asia/Kolkata"

# Minutes from midnight. Window is [23:00, 08:00) IST — overnight, spans midnight.
WINDOW_START_MIN=$((23 * 60)) # 11:00pm
WINDOW_END_MIN=$((8 * 60))    # 08:00am (exclusive)

now_stamp="$(date '+%Y-%m-%d %H:%M:%S %Z')"
now_min=$((10#$(date '+%H') * 60 + 10#$(date '+%M')))

in_window=0
if ((now_min >= WINDOW_START_MIN || now_min < WINDOW_END_MIN)); then
  in_window=1
fi

if ((in_window == 0)) && [[ "${FORCE_STOP:-0}" != "1" ]]; then
  echo "${now_stamp} refusing stop outside overnight window 23:00–08:00 Asia/Kolkata (now_min=${now_min})"
  echo "${now_stamp} this prevents a missed 11pm launchd job from killing daytime GPUs on late wake"
  echo "${now_stamp} to override: FORCE_STOP=1 $0"
  exit 0
fi

if ((in_window == 0)); then
  echo "${now_stamp} FORCE_STOP=1 set; bypassing overnight window"
fi

if [[ ! -x "$CLI" ]]; then
  echo "missing CLI binary: $CLI" >&2
  exit 2
fi

IDS=()
while IFS= read -r id; do
  [[ -n "$id" ]] && IDS+=("$id")
done < <(
  "$CLI" -o json list | "$PYTHON" -c '
import json, sys
rows = json.load(sys.stdin)
for row in rows:
    status = str(row.get("status") or "").upper()
    deployment_id = row.get("deployment_id")
    if deployment_id and status != "DELETED":
        print(deployment_id)
'
)

if [[ ${#IDS[@]} -eq 0 ]]; then
  echo "${now_stamp} no non-deleted deployments to stop"
  exit 0
fi

echo "${now_stamp} stopping ${#IDS[@]} deployment(s): ${IDS[*]}"
exec "$CLI" stop "${IDS[@]}"
