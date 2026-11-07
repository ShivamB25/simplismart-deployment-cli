# Conventions
- Python 3.11+ annotations and `from __future__ import annotations`.
- Keep CLI orchestration in `cli.py`; SDK behavior and idempotency in `DeploymentManager`.
- SDK responses are untyped; validate established boundary shapes without guessing provider coercions. Resource discovery accepts a bare list or a `results` list envelope and refuses malformed payloads/rows instead of converting them to authoritative absence. State transitions and orchestration invariants: `mem:architecture/state_machines`.
- Start is a successful no-op for `DEPLOYED`/`PENDING`; stop is a no-op for `STOPPED`; restart is intentionally non-idempotent.
- Schedule clearing must pass `cron_scaling=[]`; `None` omits the field and does not clear an existing window.
- Native schedules always need both `--on-at` and `--off-at`; off-only nightly stop is local launchd + `scripts/stop-all-deployments.sh`, which must refuse outside 23:00–08:00 Asia/Kolkata so missed jobs cannot kill daytime GPUs on late wake (`mem:ops/local_auto_stop`).
- JSON errors go to stderr with stable exit codes. Token input remains environment-only.
- CLI redaction must scrub HF token-shaped scalar strings inside free-form messages, nested command arrays and error payloads, not just token-named keys. All env/environment/env_variables mappings are sensitive. Check JSON/table/stdout/stderr with explicitly reported CLI smoke runs; do not recreate fake manager/client permanent tests to assert fixture output.
- Incident scans: no raw secret lines, token prefixes/hashes, or unredacted reports. Distinguish public refs, local-only stash/reflog blobs and ignored artifacts; an alert or code risk does not establish the actual exposure source. Never rewrite/drop user Git history without a scoped coordinated plan.
- `update-env` is a one-target read/merge/edit/read-back operation; unchanged env is a read-only no-op. Actual writes require exactly DEPLOYED; reject pending/deploying/progressing, stopped, terminal and unknown/missing statuses. Preserve unrelated vars and refuse unavailable env. Key-only summaries; waited final state contains only status/health, never full detail or command argv.
- Env wait rechecks requested values/removals after DEPLOYED+Healthy to reject healthy rollback; updates are not atomic. See `mem:ops/environment_updates`.
- `--wait` polls read-only deployment/health endpoints, emits one final result, and maps timeout/terminal failure to exit 7; progress is TTY-only on stderr.
- Restart resolution order is explicit CLI values, settings (`ORG_ID`/`SIMPLISMART_NAMESPACE`), then deployment detail fields.

# Test selection and evidence
- User explicitly requested removal of low-value tests. No fake SDK/client/manager tests, mock echoes, call-forwarding assertions, help/display-wording snapshots, or private-helper matrices. Do not recreate deleted suites under new names.
- Keep the smallest set of distinct consumer-visible parsing, input-validation and scheduling regressions. Do not enumerate every SDK field/type error or pin dependency validators, copies, incidental defaults, or implementation wording. No target test counts.
- Retain meaningful manifest fail-fast boundaries (unsupported replica configuration and malformed environment mapping) and existing CLI validation, overnight-window and DST coverage. Environment fixture isolation is allowed; these tests must not contact a provider or use real credentials.
- Use throwaway localhost HTTP/isolated shell smoke scenarios to exercise orchestration. They prove local behavior, not live provider permissions/readiness/GPU release. Explicitly disclose prompt/tokenizer fixtures and other unexercised boundaries; help is startup evidence only.
- Run relevant checks after final edits and remove throwaway harnesses before commit. Guidance-only changes need no new tests or cloud mutations. Required completion checks are in `mem:task_completion`; state workflow boundaries are in `mem:architecture/state_machines`.