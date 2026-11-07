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

if [[ ! -x "$PYTHON" ]]; then
  echo "missing Python interpreter: $PYTHON" >&2
  exit 2
fi

exec "$PYTHON" - "$CLI" "$now_stamp" <<'PY'
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile

cli, now_stamp = sys.argv[1:]
lock_path = os.path.join(
    "/tmp",
    "simplismart-stop-all-deployments-{}.lock".format(os.getuid()),
)
lock_flags = os.O_CREAT | os.O_RDWR
if hasattr(os, "O_NOFOLLOW"):
    lock_flags |= os.O_NOFOLLOW

try:
    lock_fd = os.open(lock_path, lock_flags, 0o600)
    lock_stat = os.fstat(lock_fd)
    if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid():
        raise OSError("lock path is not a regular file owned by this user")
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("another stop-all-deployments invocation is already running", file=sys.stderr)
    sys.exit(1)
except OSError as exc:
    print("could not acquire stop-all-deployments lock: {}".format(exc), file=sys.stderr)
    sys.exit(1)

try:
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as listing:
        result = subprocess.run([cli, "-o", "json", "list"], stdout=listing)
        if result.returncode != 0:
            print(
                "{} deployment listing failed (exit {})".format(
                    now_stamp, result.returncode
                ),
                file=sys.stderr,
            )
            sys.exit(result.returncode if result.returncode > 0 else 1)
        listing.seek(0)
        try:
            rows = json.load(listing)
        except (json.JSONDecodeError, UnicodeError, OSError) as exc:
            print(
                "{} could not parse deployment listing: {}".format(now_stamp, exc),
                file=sys.stderr,
            )
            sys.exit(1)
except OSError as exc:
    print("{} could not run deployment listing: {}".format(now_stamp, exc), file=sys.stderr)
    sys.exit(1)

if not isinstance(rows, list):
    print("{} deployment listing must be a JSON array".format(now_stamp), file=sys.stderr)
    sys.exit(1)

ids = []
for index, row in enumerate(rows):
    if not isinstance(row, dict):
        print(
            "{} deployment listing row {} must be an object".format(now_stamp, index),
            file=sys.stderr,
        )
        sys.exit(1)
    deployment_id = row.get("deployment_id")
    status = row.get("status")
    if (
        not isinstance(deployment_id, str)
        or not deployment_id
        or "\0" in deployment_id
        or not isinstance(status, str)
        or not status.strip()
    ):
        print(
            "{} deployment listing row {} has an invalid deployment_id or status".format(
                now_stamp, index
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    if status.upper() != "DELETED":
        ids.append(deployment_id)

if not ids:
    print("{} no non-deleted deployments to stop".format(now_stamp), flush=True)
    sys.exit(0)

print(
    "{} stopping {} deployment(s): {}".format(now_stamp, len(ids), " ".join(ids)),
    flush=True,
)
os.set_inheritable(lock_fd, True)
os.execv(cli, [cli, "stop", *ids])
PY
