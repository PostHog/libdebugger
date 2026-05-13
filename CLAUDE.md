# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`libdebugger` is a Python library that implements runtime bytecode instrumentation for live debugging. It integrates with PostHog to enable dynamic probes that can be installed remotely and capture function entry/exit state without stopping execution. The library is the in-process counterpart to the `hogtrace` VM — it loads `Program`s from the control plane, locates target functions, mutates their bytecode to redirect calls through an instrumentation decorator, and dispatches probes against the captured frames.

## Core Architecture

The library uses Python bytecode manipulation to inject a redirector into running code:

1. **HogTraceManager** (`manager.py`) - Top-level orchestrator. Polls PostHog's `/api/projects/@current/live_debugger/programs/active` endpoint via `posthoganalytics`'s `Poller`, parses the response into a `hogtrace.ProgramList`, and reconciles the incoming program set against the in-process registry by routing diffs to the module-level `install_program` / `uninstall_program` / `update_program` functions.
2. **Registry-lookup hot path** (`instrumentation.py`) - Two module-level dicts back the design:
   - `_INSTALLED_PROGRAMS: Dict[str, Program]` — the source of truth, mutated under `_LOCK`.
   - `_PROBE_INDEX: Dict[(qualname, "entry"|"exit"|"line"), Tuple[(Program, Probe), ...]]` — atomic-rebound on every reconcile. Wrappers read this with zero locking on the hot path.
3. **InstrumentationDecorator** (`instrumentation.py`) - One-per-target wrapper. Holds no probe state of its own; on every call it reads `_PROBE_INDEX` and dispatches whatever probes are registered for its `qualname` right now. Self-uninstalls on the next call after its registry slots all become empty.
4. **Bytecode utilities** (`bytecode.py`) - `EntrypointInjector` + `generate_code_call_self_method` + `redirector_code` build the redirector that the decorator splices into `wrapped_fn.__code__`.
5. **Pytest stress plugin** (`pytest_stress/`) - Optional pytest plugin that rotates instrumented functions during a test run to stress the install/uninstall path.

### How Instrumentation Works

- The manager fetches a `ProgramList` from PostHog, diffs against `_INSTALLED_PROGRAMS`, and routes per-program changes through `install_program` / `uninstall_program` / `update_program`.
- `install_program` registers the `Program`, rebuilds `_PROBE_INDEX` from scratch via `_rebuild_probe_index`, atomic-rebinds the global, and (for every probe in the program) walks `resolve_target` to find the callable. If the function isn't already wrapped, an `InstrumentationDecorator` is constructed with the probe's specifier as its `qualname`.
- The decorator mutates the function's `__code__` to a redirector that hops into the decorator's `__call__`. Original code is saved on `decorator.original_code`.
- On every call the decorator reads `_PROBE_INDEX[(qualname, "entry"|"exit"|"line")]` lock-free, runs whatever it finds, and self-cleans (restores `__code__`, deletes `__posthog_decorator`) if every slot for its qualname is empty.
- Entry probes execute inside `instrumented_fn`'s own frame (via a bytecode-injected call to `_capture_caller_frame_and_run_entry_probes`) so probes see named parameters as locals. Exit probes run in `__call__`'s `finally` against that same captured frame.
- Captures are forwarded to PostHog as `$hogtrace_capture` events via `_enqueue_message`.

### Key Implementation Details

- **Source of truth**: `_INSTALLED_PROGRAMS` (program_id → `Program`) is the only mutable state; `_PROBE_INDEX` is derived. `_rebuild_probe_index` is a pure function of `_INSTALLED_PROGRAMS` — this is asserted as a Hypothesis property (P5).
- **Lock-free read path**: writers serialize against each other on `_LOCK`; readers (the wrapper's `__call__`) take no lock. Whole-dict replacement of `_PROBE_INDEX` keeps reads atomic under CPython's GIL.
- **Tuple reuse**: when `_rebuild_probe_index` produces a slot with the same `(program.id, probe.id)` set as the previous index, it reuses the old tuple object so the wrapper's identity-compare drift check stays stable.
- **Self-uninstalling wrapper**: when a call ends and the registry has no probes left for the wrapper's qualname, the decorator restores the original `__code__` and deletes `__posthog_decorator` from `wrapped_fn`. The check uses `delattr(..., "__posthog_decorator")` because Python's class-body name-mangling would otherwise rewrite `del self.wrapped_fn.__posthog_decorator` to a never-matching mangled name.
- **Recursion**: per-call frame-stack on the decorator (`self.frames`) lets recursive calls each push their own frame; the `previous_frame_top` check in `__call__` distinguishes "we pushed a new frame" from "instrumented_fn crashed before pushing".
- **Polling lifecycle**: `HogTraceManager.start()` spawns a `posthoganalytics.poller.Poller` only when `client.personal_api_key` is set. `stop()` snapshots `_INSTALLED_PROGRAMS` keys under `_LOCK`, releases the lock, then iterates `uninstall_program(pid)` — never holding `_LOCK` across calls into `uninstall_program` because the lock is non-reentrant.
- **Reconcile error isolation**: `_fetch_programs` wraps each per-program install/uninstall/update in its own `try/except` so a single bad program does not abort the cycle. HTTP/parse errors are caught around the whole fetch so the poller keeps spinning.

## Development Commands

This project uses `uv` for dependency management. The project requires Python 3.11+.

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
uv run python main.py
```

## Dependencies

- **hogtrace**: Probe compilation + `Program` / `ProgramList` types + probe-execution VM (`execute_probe`, `get_store`, `get_scope`).
- **bytecode**: Core library for Python bytecode manipulation.
- **posthog / posthoganalytics**: PostHog Python SDK for `capture()` and the polling `Poller`. Also provides `posthoganalytics.request.get` for the control-plane fetch.
- **hypothesis**: Property-based testing framework; used heavily by `test_manager_property.py` and the stateful machine that drives install/uninstall/update exploration.
- **ruff**: Fast Python linter and formatter (dev dependency).

## Testing

The test suite is built around Hypothesis property tests that pin the spec's invariants:

- `test_manager_property.py` — Phase 1–6 properties: behavior preservation, trace fidelity, registry/index consistency, self-cleanup convergence, order-independence, recursion safety. Also hosts the `RegistryMachine` Hypothesis stateful machine.
- `test_manager_concurrency.py` — Phase 7: thread-interleaving stress, multiple worker threads calling instrumented functions while another thread reconciles.
- `test_manager_integration.py` — Phase 8: end-to-end `HogTraceManager` wiring with mocked HTTP, deadlock-free `stop()`, and the no-API-key short-circuit.
- `test_instrumentation*.py` — lower-level coverage of `InstrumentationDecorator` and bytecode signatures.
- `test_strategies.py` — meta-tests for the Hypothesis strategies in `test/strategies.py`.

A `conftest.py` autouse fixture (`reset_state`) wipes `_INSTALLED_PROGRAMS`, `_PROBE_INDEX`, and any lingering `__posthog_decorator` attributes between tests so test residue can't bleed into the next case.

## Important Constraints

- **Line probes are scaffolded but not woven into bytecode in v1.** The `(qualname, "line")` slot of `_PROBE_INDEX` exists and the wrapper's drift check accepts the slot, but `_build_instrumented` only injects the entry-probe call and logs a warning if any line probes are passed. v2 will weave probe calls at the right `co_lnotab` offsets.
- **`resolve_target` only walks module-attribute paths.** Closures inside other functions, lambdas, functions stored in module-level containers (`HANDLERS = [foo]`, `DISPATCH = {"x": foo}`), per-instance method overrides, monkey-patched functions, and descriptors/properties are out of scope. The Future-work section in the spec covers line-probe-driven lambda resolution for v2.
- **Wildcard specifiers (`fn:myapp.users.*:entry`) are not resolved.** The manager logs a warning and skips the probe.
- **Cleanup is lazy.** Wrappers self-uninstall on the next call after their registry slots empty. For rarely-called functions an idle wrapper can hang around indefinitely — a few hundred bytes plus one extra function call per invocation, both negligible.
- **`HogTraceManager.start` requires `client.personal_api_key`.** Without it the manager logs a warning and does not spawn a poller; this is by design so the SDK can be loaded without a key and not crash.
