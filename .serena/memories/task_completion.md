# Completion checks
1. Run `uv lock` when dependencies or project metadata change; run `uv sync --locked --all-groups` after lock changes.
2. For behavioral/CLI changes, run `uv run --locked pytest -q` after final edits, not repeatedly during parallel implementation. Select tests by consumer-visible regression value, not a target count; see `mem:conventions`.
3. Smoke the changed behavior through the packaged CLI or actual script. `--version`/`--help` prove startup only. Use localhost HTTP or isolated shell scenarios instead of live cloud mutations for guard/error-path checks; state fixture substitutions and unexercised provider/tokenizer/model boundaries. State-specific invariants: `mem:architecture/state_machines`.
4. Remove throwaway harnesses and redundant/obsolete tests before committing; preserve meaningful parsing, validation and schedule/DST regressions.
5. Guidance/memory-only changes need no new tests, dependency churn or cloud operations; check scoped diffs, memory references, staged paths and secret safety.
6. Run `uv build` for distribution/package identity changes.
7. After push, verify the relevant GitHub Actions Python 3.11-3.14 matrix; report CI success only after observing terminal success, otherwise state its status or verification limit.
