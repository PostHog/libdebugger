# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`libdebugger` is a Python library that enables live debugging in production via PEP 669 `sys.monitoring`. It integrates with PostHog to install dynamic probes remotely and capture function entry/exit state without stopping execution. The library is the in-process counterpart to the `hogtrace` VM — it loads `Program`s from the control plane, resolves target functions, enables per-code-object `sys.monitoring` events, and dispatches probes against the running frame.

## Core Architecture

The library uses CPython's `sys.monitoring` API (3.12+) to receive events directly from the interpreter:

1. **HogTraceManager** (`manager.py`) — Top-level orchestrator. Polls PostHog's `/api/projects/@current/live_debugger/programs/active` endpoint, parses the response into a `hogtrace.ProgramList`, and reconciles the incoming program set against the in-process registry by routing diffs to the module-level `install_program` / `uninstall_program` / `update_program` functions.
2. **Dispatch state** (`instrumentation.py`) — Four module-level structures back the design:
   - `_INSTALLED_PROGRAMS: Dict[str, Program]` — source of truth, mutated under `_LOCK`.
   - `_PROBE_INDEX: Dict[(qualname, "entry"|"exit"|"line"), Tuple[(Program, Probe), ...]]` — qualname-keyed view of the registry, kept for tests / tooling that look up by specifier. Atomic-rebound.
   - `_CODE_PROBE_INDEX: Dict[(CodeType, "entry"|"exit"), Tuple[(Program, Probe), ...]]` — code-keyed dispatch table. Aggregates every specifier resolving to the same code object so an aliased function fires every probe pointing at it. The `sys.monitoring` callbacks read this. Atomic-rebound.
   - `_MONITORED_CODES: Dict[CodeType, int]` — current event-mask per code. Mutated under `_LOCK` by `_apply_monitoring`. Also the source of truth for `is_instrumented(fn)`.
3. **Dispatch callbacks** (`instrumentation.py`) — `_on_py_start`, `_on_py_resume`, `_on_py_return`, `_on_py_yield`, `_on_py_unwind`. Each grabs the current frame via `sys._getframe(2)` (callbacks live below the user frame) and runs the matching probes from `_CODE_PROBE_INDEX`. `PY_UNWIND` is global-only in CPython, so it's enabled via `sys.monitoring.set_events`; the callback filters by code via `_CODE_PROBE_INDEX` to stay cheap on non-monitored frames.
4. **Tool-id lifecycle** — On the first successful `install_program` we acquire a `sys.monitoring` tool slot. Preference order: 3, 4, then `DEBUGGER_ID`, `COVERAGE_ID`, `PROFILER_ID`, `OPTIMIZER_ID`. Ad-hoc slots first so we stay out of the way of pdb / debugpy / coverage by default; reserved slots are borrowed only when the ad-hoc ones are taken. Slot is held for the lifetime of the process — `_release_tool` disables events but does NOT free the slot.
5. **Bytecode utilities** (`bytecode.py`) — Retained for reference only; not imported by the runtime. Earlier versions used these to rewrite `__code__` on instrumented functions.
6. **Pytest stress plugin** (`pytest_stress/`) — Optional pytest plugin that rotates installed programs during a test run to stress the install/uninstall path.

### How Instrumentation Works

- The manager fetches a `ProgramList` from PostHog, diffs against `_INSTALLED_PROGRAMS`, and routes per-program changes through `install_program` / `uninstall_program` / `update_program`.
- `install_program` acquires `_LOCK`, calls `_ensure_tool_registered` BEFORE mutating any state (so a failed tool acquisition doesn't pollute `_INSTALLED_PROGRAMS`), then registers the `Program` and calls `_rebuild_probe_index`.
- `_rebuild_probe_index` rebuilds `_PROBE_INDEX` and `_CODE_PROBE_INDEX` from scratch, resolves each specifier to a `CodeType`, and calls `_apply_monitoring` to diff the new code-mask set against `_MONITORED_CODES` (enabling new codes, disabling departed ones).
- When the interpreter fires a monitored event, the callback reads `_CODE_PROBE_INDEX[(code, kind)]` lock-free and runs the probes against the running frame. `__code__` is never mutated.
- Captures are forwarded to PostHog as `$hogtrace_capture` events via `_enqueue_message`.

### Key Implementation Details

- **Source of truth**: `_INSTALLED_PROGRAMS` (program_id → `Program`) is the only mutable state owned by manager-level operations; everything else is derived. `_rebuild_probe_index` is a pure function of `_INSTALLED_PROGRAMS` — asserted as a Hypothesis property (P5).
- **Lock-free read path**: writers serialize on `_LOCK`; readers (the `sys.monitoring` callbacks) take no lock. Whole-dict replacement of `_PROBE_INDEX` / `_CODE_PROBE_INDEX` keeps reads atomic under CPython's GIL.
- **Tuple reuse**: when `_rebuild_probe_index` produces a slot with the same `(program.id, probe.id)` set as the previous index, it reuses the old tuple object so identity-compare-based drift detection stays stable.
- **Generator semantics**: entry probes fire on `PY_START` AND `PY_RESUME`; exit probes fire on `PY_RETURN`, `PY_YIELD`, and `PY_UNWIND`. v1 (bytecode-rewriting) only fired entry once per outer call — the new behavior is a deliberate, broader trace.
- **Reentrancy**: CPython suppresses monitoring events for our tool id while one of our callbacks is on the stack, so a probe body that re-enters an instrumented function does NOT recursively fire. Pinned by `test_sys_monitoring.py::test_callback_does_not_reenter_on_self_call` — a future interpreter change there gets caught at test time.
- **Polling lifecycle**: `HogTraceManager.start()` spawns a `posthoganalytics.poller.Poller` only when `client.personal_api_key` is set. `stop()` snapshots `_INSTALLED_PROGRAMS` keys under `_LOCK`, releases the lock, then iterates `uninstall_program(pid)` — never holding `_LOCK` across calls because the lock is non-reentrant.
- **Reconcile error isolation**: `_fetch_programs` wraps each per-program install/uninstall/update in its own `try/except` so a single bad program does not abort the cycle. HTTP / parse errors are caught around the whole fetch so the poller keeps spinning.

## Development Commands

This project uses `uv` for dependency management. Python 3.12+ is required.

### Setup
```bash
uv sync
```

### Linting
```bash
uv run ruff check .
uv run ruff format .
```

### Tests
```bash
uv run pytest test/ -v
# Concurrency stress (run multiple times to flush out races):
for i in 1 2 3; do uv run pytest test/test_manager_concurrency.py 2>&1 | tail -2; done
```

### Running the example
```bash
cd example && uv run flask --app app run
```

## Dependencies

- **hogtrace**: Probe compilation + `Program` / `ProgramList` types + probe-execution VM (`execute_probe`, `get_store`, `get_scope`).
- **posthog / posthoganalytics**: PostHog Python SDK for `capture()` and the polling `Poller`.
- **hypothesis**: Property-based testing framework; used heavily by the stateful machine that drives install/uninstall/update exploration.
- **ruff**: Fast Python linter and formatter (dev dependency).
- **bytecode**: Still listed because `libdebugger/bytecode.py` is retained for reference; no longer imported at runtime.

## Testing

The test suite uses Hypothesis property tests to pin the spec's invariants:

- `test_manager_behavior.py` — behavior preservation (P7). Install + uninstall is a no-op on return values.
- `test_manager_probe_firing.py` — trace fidelity (P1) plus `resolve_target` and `_rebuild_probe_index` tuple-reuse coverage.
- `test_manager_self_cleanup.py` — registry consistency (P2/P3), synchronous cleanup convergence (P4), order-independence (P5).
- `test_manager_recursion.py` — recursion safety (P6). Hand-written counts for `fact()` / `recur_raise()` plus a Hypothesis sweep.
- `test_manager_registry_machine.py` — `RegistryMachine` Hypothesis stateful machine. Asserts P2/P3/P4/P5 invariants over arbitrary install/uninstall/update sequences.
- `_manager_helpers.py` — Shared helpers (target module, args strategies, `_build_program`, `_drain_registry`, normalization helpers).
- `test_manager_concurrency.py` — thread-interleaving stress: worker threads calling instrumented functions while another thread reconciles.
- `test_manager_integration.py` — end-to-end `HogTraceManager` wiring with mocked HTTP, deadlock-free `stop()`, the no-API-key short-circuit.
- `test_sys_monitoring.py` — tool-id lifecycle (acquisition, idempotence, fallback chain, conflict refusal, slot retention), generator entry/exit semantics, `PY_UNWIND` delivering the exception, and frame-access via `sys._getframe(2)`.
- `test_pr6_review_findings.py` — regressions for issues caught during PR #6 review (failed install pollution, aliased-code dispatch, line-probe filtering, tool-slot fallback, marker-race).
- `test_example_app_status.py` — regression for the example Flask app's status snapshot using `is_instrumented`.
- `test_strategies.py` — meta-tests for the Hypothesis strategies in `test/strategies.py`.

A `conftest.py` autouse fixture (`reset_state`) clears `_INSTALLED_PROGRAMS`, `_PROBE_INDEX`, `_CODE_PROBE_INDEX`, `_MONITORED_CODES`, and forcibly frees the `sys.monitoring` tool slot between tests. Production code holds the slot for the process lifetime; the test harness explicitly frees it so the tool-slot tests can exercise the full acquisition path each time.

## Important Constraints

- **Line probes are not implemented in v1.** The `(qualname, "line")` slot of `_PROBE_INDEX` exists, but `_rebuild_probe_index` skips them from `_CODE_PROBE_INDEX` and from `_MONITORED_CODES` — the LINE event mask is never enabled. A warning is logged once per `(program_id, probe_id)`.
- **`resolve_target` only walks module-attribute paths.** Closures inside other functions, lambdas, functions stored in containers (`HANDLERS = [foo]`, `DISPATCH = {"x": foo}`), per-instance method overrides, monkey-patched functions, and descriptors / properties are out of scope.
- **Wildcard specifiers (`fn:myapp.users.*:entry`) are not resolved.** The manager logs a warning and skips the probe.
- **Cleanup is synchronous.** `uninstall_program` disables events on every code that left the dispatch table inside the same critical section as the registry update — no lazy "next call" trigger.
- **Tool-slot ownership is process-lifetime.** Once acquired, the slot stays ours until the process exits (or a test explicitly frees it).
- **`HogTraceManager.start` requires `client.personal_api_key`.** Without it the manager logs a warning and does not spawn a poller; this is by design so the SDK can be loaded without a key and not crash.
