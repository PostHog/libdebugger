# HogTrace Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the hogtrace manager loop end-to-end — fetch programs from PostHog, reconcile into a lock-free registry, wrap target functions with self-uninstalling decorators, fire probes on call. Validated by Hypothesis property tests written first.

**Architecture:** Module-level atomic-rebound probe registry; self-managing `InstrumentationDecorator` that looks up probes on every call and uninstalls itself when the registry empties for its `qualname`. Entry probes are bytecode-injected into `instrumented_fn`; exit probes run in `__call__`'s `finally` against the captured frame.

**Tech Stack:** Python 3.11+, `uv` for env management, `hypothesis` for property-based testing, `bytecode` for runtime bytecode manipulation, `hogtrace` (Rust/PyO3) for probe compilation + execution, `posthoganalytics` for HTTP + event capture.

**Reference:** See companion spec at `docs/superpowers/specs/2026-05-13-hogtrace-manager-design.md`. Everything below assumes that spec is the source of truth — if anything here conflicts with the spec, the spec wins.

---

## File map

```
libdebugger/
  instrumentation.py       MODIFY — strip probe state from decorator, registry-lookup __call__,
                           atomic _PROBE_INDEX, _LOCK, free-function helpers, _run_probes,
                           _any_probes_for. Keep redirector + entry-probe bytecode injection.
  manager.py               MODIFY — finish _fetch_programs, install_program/uninstall_program/
                           update_program free functions, resolve_target, _rebuild_probe_index,
                           stdlib logging. HogTraceManager keeps start/stop/poll orchestration.
  program.py               DELETE — ProgramManager class is gone; free functions in manager.py.
  bytecode.py              UNCHANGED — EntrypointInjector + redirector_code already work.
  pytest_stress/           UNCHANGED — already keys off __posthog_decorator marker.

test/
  conftest.py              CREATE — fixture that resets _PROBE_INDEX, _INSTALLED_PROGRAMS,
                           and unwraps any leftover decorators between tests; mock for
                           _enqueue_message; helper to import a fresh per-test target module.
  strategies.py            CREATE — Hypothesis strategies for Program, Probe, specifier,
                           hogtrace source snippets. One stable target-function module.
  test_manager_property.py CREATE — phases 1-7 property tests (one phase per RuleBasedStateMachine
                           or hypothesis.given test). Replaces the old test_manager.py shape.
  test_instrumentation.py  KEEP — existing unit tests still useful for low-level bytecode behavior.
  test_instrumentation_signatures.py  KEEP — existing.
  test_manager.py          DELETE or empty — content is fully commented out, supplanted by
                           test_manager_property.py.

docs/superpowers/specs/2026-05-13-hogtrace-manager-design.md   (already exists)
```

---

## Phase ordering & test discipline

The phases below correspond 1:1 to the **Testing approach** section of the spec. Each phase:

1. Write the property test(s) for that phase. Run them. They should fail with a clear message ("not implemented," "AttributeError," etc.).
2. Write the minimum production code to make them pass. Avoid implementing future-phase concerns.
3. Run all tests from all phases-so-far — earlier phases must stay green.
4. Refactor if needed. Commit.

Don't skip ahead. The phases are dependency-ordered: P1 (behavior) before P2 (probes fire) before P3 (registry consistent) before P4 (cleanup) before P5 (order-independence) before P6 (recursion) before P7 (threads). Skipping breaks the test-failure feedback loop.

---

## Phase 0 — Scaffolding

**Goal:** Tests can run, imports work, every test starts from a clean slate.

- [ ] Create `test/conftest.py` with a `reset_state` autouse fixture that, after every test:
  - Rebinds `libdebugger.instrumentation._PROBE_INDEX = {}` and `._INSTALLED_PROGRAMS = {}`.
  - Walks the target-function module and, for each function with `__posthog_decorator`, calls `dec.cleanup()` and `del fn.__posthog_decorator`.
  - Resets any patched `_enqueue_message`.
- [ ] Create `test/strategies.py` with Hypothesis strategies:
  - `specifiers()` — picks from a fixed pool of qualnames of functions defined in the target module.
  - `programs(probes_max=4)` — builds `Program` objects via `hogtrace.compile(...)` then `hogtrace.package(id, bytecode)`. Use simple sources like `fn:test.target.f:entry { capture(x=1); }` so we don't depend on hogtrace expression features.
  - `program_lists(max_size=5)` — sets of programs with distinct IDs.
- [ ] Create a target module `test/target.py` containing a stable pool of plain functions, methods on a class, and one recursive function. Don't import anything fancy — these need to be deterministic across runs.
- [ ] Run `uv run pytest test/test_manager_property.py -v` (which doesn't exist yet) → confirm "no tests collected" or import error, not silent pass.

**Commit at end of phase.** Suggested message: `test: scaffolding for hogtrace-manager property tests`.

**Gotchas:**
- The fixture MUST run after every test or state leaks between tests will produce false flakiness that's a nightmare to debug.
- Hypothesis examples are reused across runs via `.hypothesis/` — if test state leaks once, you'll see the same failing example replayed every run until you delete the database. Add `--hypothesis-seed=<n>` reproduction to your toolbelt.
- `hogtrace.compile()` returns `ProgramBytecode`, not `Program`. Use `hogtrace.package(id, bytecode)` to wrap. Don't confuse the two.

---

## Phase 1 — Behavior preservation (P7)

**Goal:** Wrap a function with the decorator, no probes installed; assert `f(args) == original_f(args)` for any input. Wrap → call → cleanup → call must equal call → call.

What gets built / modified:
- `InstrumentationDecorator.__init__` accepting `qualname` (new param), unwrap bound methods, generate redirector, build `instrumented_fn` via `_build_instrumented` (entry-probe injection only).
- `_build_instrumented` shape from the spec — for v1 just wraps `original_code` with the entry-probe call. Line probes get a warning + ignored.
- `__call__` does the line-drift check (no-op until phase 2 because registry is empty), then calls `instrumented_fn(*args, **kwds)` and returns. No exit-probe loop yet.
- `cleanup()` already exists at `instrumentation.py:135`; keep it idempotent.

**Commit when green.** Suggested: `feat(instrumentation): minimal InstrumentationDecorator wrap/unwrap`.

**Gotchas:**
- `wrapped_fn.__code__` mutation must happen at the *end* of `__init__` — if you swap it before `instrumented_fn` is built, the recursive copy inside `_build_instrumented` will get the redirector code, not the original.
- `instrumented_fn` MUST be a separate `FunctionType`. If you call `self.wrapped_fn(*args, **kwds)` from inside `__call__`, you'll loop forever through the redirector.
- Bound methods: `inspect.ismethod(fn)` → store `fn.__self__`, then unwrap to `fn.__func__`. The marker attribute lands on the underlying function. Existing code does this; preserve it.
- Closures: redirector code must preserve `freevars` and `cellvars` from `original_code` even though the redirector itself doesn't use them. Existing code does this at `bytecode.py`.
- `__del__` runs `cleanup()`. If the test fixture also calls `cleanup()` then deletes the attribute, the finalizer's second `cleanup()` is a no-op (because `wrapped_fn.__code__ is already self.original_code`). Don't add an `assert is not` check that would make it crash.

---

## Phase 2 — Trace fidelity (P1)

**Goal:** Install a program with one entry probe on a known function; call once; `_enqueue_message` is called once with the right `(program, probe)`. Then with N entry probes, called N times. Exit probes fire on return AND on exception (with `exception=` set).

What gets built / modified:
- `manager.py`: `install_program`, `_rebuild_probe_index`, `resolve_target`, module-level `_LOCK` / `_INSTALLED_PROGRAMS`. Stdlib `logging`.
- `instrumentation.py`: `_PROBE_INDEX` module global (initialized to `{}`), `_run_probes` helper, `_capture_caller_frame_and_run_entry_probes` reads from `_PROBE_INDEX` instead of `self.entry_probes`, `__call__` exit-probe loop runs in `finally` against the captured frame.
- Remove `self.entry_probes` and `self.exit_probes` and `add_probe`/`remove_probe` from the decorator (they have no remaining callers).

**Commit when green.** Suggested: `feat(manager): install_program + registry-lookup probe firing`.

**Gotchas:**
- `_capture_caller_frame_and_run_entry_probes` uses `sys._getframe(1)` to grab `instrumented_fn`'s frame. The frame index is sensitive to indirection — if you add a wrapper layer or invoke through `functools.partial`, that `1` becomes wrong. Leave it alone.
- `execute_probe(program.program_bytecode, probe, frame, store, retval=, exception=)` — note it's `program_bytecode`, not `program`. The wrapper around `Program` already exists in hogtrace.
- `get_store()` returns `None` outside a request scope. Either wrap probe execution in a default scope for tests, or have `_run_probes` skip probes when store is `None`. The spec says skip (`if req_store is None: continue`).
- `_enqueue_message` calls `posthoganalytics.capture` and `get_scope()`. In tests, mock the whole `_enqueue_message` function rather than mocking `capture` — simpler, and `get_scope` will return `None` outside a context.
- `_rebuild_probe_index` must reuse the existing tuple object when contents are unchanged (compare with `==` first, fall back to building a new tuple only when actually different). Skip this and line-probe drift detection (phase 6+) will fire on every call.
- `_INSTALLED_PROGRAMS` is a regular dict mutated in place under `_LOCK`. `_PROBE_INDEX` is atomic-rebound (whole-dict replace). Different semantics — readers of `_INSTALLED_PROGRAMS` must hold `_LOCK`, readers of `_PROBE_INDEX` must not.

---

## Phase 3 — Registry & index consistency (P2, P3)

**Goal:** Hypothesis stateful machine. Steps: install/uninstall/update programs in any order. Invariants after every step: `set(_INSTALLED_PROGRAMS) == expected_ids`; every `(program, probe)` in `_PROBE_INDEX` has `program.id in _INSTALLED_PROGRAMS` (no dangling entries).

What gets built / modified:
- `manager.py`: `uninstall_program`, `update_program`.
- Test side: `RuleBasedStateMachine` subclass with `@rule`s for `install` / `uninstall` / `update`; invariants checked via `@invariant`.

**Commit when green.** Suggested: `test(manager): stateful property test for registry consistency`.

**Gotchas:**
- `update_program(p)` is defined as `uninstall(p.id); install(p)`. Make sure the locking on each is non-reentrant safe — `_LOCK` is a `Lock`, not an `RLock`, so don't call `install_program` while holding `_LOCK`. The spec's code already releases between operations; preserve that.
- `RuleBasedStateMachine` builds its own `Bundle`s of state — make sure your model state mirrors what's actually in `_INSTALLED_PROGRAMS` so divergence is detectable.
- `hypothesis.stateful` deepcopies state for shrinking by default. `Program` from hogtrace may not be deepcopy-friendly (Rust-backed objects). Either provide `__deepcopy__` or store `program.id` in your model and re-fetch from `_INSTALLED_PROGRAMS`.
- Two programs with the same ID: spec says `install_program(p)` overwrites. Make sure your Hypothesis strategies don't accidentally produce duplicate IDs that mask a real bug. Use `unique_by=lambda p: p.id` on the list strategy.

---

## Phase 4 — Self-cleanup convergence (P4)

**Goal:** After uninstalling every program targeting function `F` and calling `F` once, `not hasattr(F, '__posthog_decorator') and F.__code__ is original_code_for(F)`.

What gets built / modified:
- `__call__` `finally` block: the empty-registry check + lock + re-check + `cleanup() + delattr`.
- `_any_probes_for(qualname)` helper.

**Commit when green.** Suggested: `feat(instrumentation): wrapper self-uninstalls when registry empties`.

**Gotchas:**
- The "re-check under lock" pattern is load-bearing. Without it, a concurrent installer could add a probe between your `not entry and not exit_ and not line` check and the cleanup, and you'd lose their probe. Don't be tempted to skip it.
- `delattr(fn, '__posthog_decorator')` on a function whose attribute was never set raises `AttributeError`. Wrap in `try/except AttributeError: pass` even though the conditional should prevent it — defensive in the recursion-cleanup case where two stacked __call__ frames both decide to cleanup.
- After cleanup, `fn.__code__ is original_code` should hold. But `fn` is a function object whose `__code__` was *mutated* — `is` check works because Python re-uses the original code object reference stored on `self.original_code`. Sanity-check this in the test.
- The original `test_manager.py` had a `reset_function` helper for similar cleanup; you can scavenge ideas but don't import — that file is going away.

---

## Phase 5 — Order-independence (P5)

**Goal:** Run the stateful machine on two different orderings of the same final-state set; assert resulting `_PROBE_INDEX` is structurally equal.

What gets built / modified:
- Test only. Re-uses the Phase 3 stateful machine but checks a stronger invariant at terminal state.

**Commit when green.** Suggested: `test(manager): _PROBE_INDEX is a pure function of installed program set`.

**Gotchas:**
- Two `Program` objects with the same probe set may not be `==` (depends on hogtrace's `__eq__` impl). Compare via a normalized view — e.g., `{(qn, target, p.id, probe.id) for (qn, target), pairs in idx.items() for p, probe in pairs}`.
- This phase mostly catches bugs where `_rebuild_probe_index` is *non-deterministic* (e.g., set-ordering leaks into output, or `_INSTALLED_PROGRAMS.values()` returns in insertion-order and that order changes outcomes). Dict insertion order is stable in Python 3.7+, so this should pass once the index build is right.

---

## Phase 6 — Recursion safety (P6)

**Goal:** Recursive function with depth `N` and one entry probe + one exit probe fires `_enqueue_message` exactly `N * 2` times. No deadlock, no missed probes.

What gets built / modified:
- Test only, against an existing recursive function in `test/target.py`.
- If something breaks, the fix is probably in the frame-stack handling in `__call__` — specifically the `previous_frame_top` check. Recursive calls each push their own frame; we pop one per call. The check distinguishes "this call pushed a frame" from "this call crashed before push."

**Commit when green.** Suggested: `test(instrumentation): probes fire correctly under recursion`.

**Gotchas:**
- A common bug: forgetting that `self.frames` is shared across threads, not just stack frames. If two threads enter the same wrapper, their pushes/pops interleave. Phase 7 catches this — but if you see it here with depth=1, it's a single-thread bug.
- Python recursion limit is 1000 by default. Hypothesis can generate huge recursion depths; cap your test's depth at something sane (e.g., 50) via the strategy.
- `_enqueue_message` is mocked in tests; ensure the mock isn't lock-contended across recursion (use `MagicMock`'s default which is fine).

---

## Phase 7 — Thread interleaving (P8)

**Goal:** Multiple threads call instrumented functions while another thread runs install/uninstall. Properties 1, 4, 7 still hold. No `RuntimeError: dictionary changed size during iteration`.

What gets built / modified:
- Test only, but this is the meanest one. Either:
  - A custom Hypothesis test that spawns a thread pool, runs a random workload for N seconds, then asserts invariants on the final state, OR
  - A `RuleBasedStateMachine` with `@invariant` checks plus a `@rule` that spawns N concurrent calls to a wrapped function and waits.

This phase **does not** need new production code if the design is right — the locking and atomic-rebind discipline from earlier phases is what carries it. If something blows up, fix it in the relevant earlier phase and don't backfill here.

**Commit when green.** Suggested: `test(manager): registry survives concurrent install + call workload`.

**Gotchas:**
- Hypothesis stateful machines aren't built for concurrent state mutation. You'll likely roll your own threading harness inside a regular `@given` test, OR use `RuleBasedStateMachine` only for the serial portion and add a separate concurrent stress test.
- `RuntimeError: dictionary changed size during iteration` is the smoking-gun symptom of someone iterating `_PROBE_INDEX` (or its values) while a writer mutates it. Atomic-rebind prevents this AT THE INDEX LEVEL — but if you ever iterate `_INSTALLED_PROGRAMS` without holding `_LOCK`, this will fire. Audit every loop.
- `pytest --forked` or `pytest -p no:cacheprovider` can help if Hypothesis's example cache trips over thread state.
- This phase is expected to take much longer than the others. Budget ~half a day. Don't beat yourself up if you need to checkpoint and continue.
- If you can't get a deterministic Hypothesis-driven concurrent test, fall back to a deterministic-replay stress test (fixed seed, fixed workload) and document the gap. Better than nothing.

---

## Phase 8 — HogTraceManager top-level wiring

**Goal:** `HogTraceManager.start()` actually polls and reconciles. `_fetch_programs` does the HTTP call, parses `ProgramList`, diffs against `_INSTALLED_PROGRAMS`, and routes to `install_program` / `uninstall_program` / `update_program`. `stop()` halts the poller and uninstalls everything. Errors during a poll cycle log and continue — the poller never dies.

What gets built / modified:
- `manager.py`: `HogTraceManager.start`, `HogTraceManager.stop`, `_fetch_programs`. Replace the never-implemented `self.log_info` / `self.log_warning` calls with stdlib `logging`.

Tests live in `test_manager_property.py` or a separate `test_manager_integration.py`:
- Mock `posthoganalytics.request.get` (e.g. with `responses` or `unittest.mock.patch`). Feed it a serialized `ProgramList`. Assert `_INSTALLED_PROGRAMS` matches after a fetch.
- Mock to raise a `requests.RequestException` mid-fetch; assert the next tick succeeds (poller survived).
- Empty `ProgramList` response → previously-installed programs all uninstalled.
- Hash change for an existing program → `update_program` fired.

**Commit when green.** Suggested: `feat(manager): wire HogTraceManager.start/stop end-to-end`.

**Gotchas:**
- `posthoganalytics.request.get` returns a `requests.Response`. Use `.content` (bytes) for `ProgramList.from_bytes`, not `.text` (str). Wrong one → silent decode error.
- `Poller`'s `execute` callback runs on a background thread. If `_fetch_programs` raises, does `Poller` swallow it and continue, or stop? Assume the worst — wrap the whole body in `try/except Exception: logger.exception(...)`.
- The endpoint `/api/projects/@current/live_debugger/programs/active` is unverified. If the PostHog server doesn't have it yet, you'll get a 404. Build a test fixture that serves a valid `ProgramList` over a local HTTP mock so you don't depend on the real endpoint to make tests green.
- **Spec bug:** `HogTraceManager.stop()` in the spec is written as `with _LOCK: for pid in list(_INSTALLED_PROGRAMS): uninstall_program(pid)`. That deadlocks: `_LOCK` is a non-reentrant `threading.Lock`, and `uninstall_program` itself does `with _LOCK`. Fix when implementing — snapshot under the lock, release, then iterate:
  ```python
  with _LOCK:
      pids = list(_INSTALLED_PROGRAMS)
  for pid in pids:
      uninstall_program(pid)
  ```
  Update the spec's stop() sketch in the same commit so the spec stays correct.

---

## Cross-cutting gotchas (read once, refer often)

### Bytecode / Python-version specifics

- Python 3.11+ has different bytecode (PRECALL/CALL vs CALL_FUNCTION). The `bytecode` library abstracts this, but if you're debugging `bytecode.py` directly, know which version you're on.
- Generator functions (`def f(): yield`) and async functions (`async def f()`) have different code object flags. Bytecode injection at entry might work, but the redirector + `instrumented_fn` chain hasn't been validated on these. **Out of scope for v1; document the gap in the spec's "Known limitations" if you discover failures.**
- `co_exceptiontable` (3.11+) changes how exceptions are encoded. The `bytecode` library handles it; just don't write your own exception-table parser.

### Hogtrace contract

- `hogtrace.compile(src) → ProgramBytecode`.
- `hogtrace.package(id, bytecode) → Program`.
- `Program.program_bytecode` returns the bytecode object — pass that to `execute_probe`.
- `Probe.spec.specifier` is a string like `"myapp.users.create"`. `Probe.spec.target` is `"entry"` or `"exit"` (or `"line"` in v2).
- `Program.hash` is `str`, not `int`. Spec field types in `program.py` were wrong; new design has them right.
- `ProgramList.from_bytes(bytes) → ProgramList`. Iterate `.programs`, not `.items()`.

### State / locking

- `_LOCK` is a `threading.Lock`, not `RLock`. No nested acquisition.
- Wrapper's `self._lock` and module-level `_LOCK` are distinct. Lock order: manager → never wrapper; wrapper → never manager. Easy to keep right because the manager never reaches into a wrapper's state and vice versa.
- `_PROBE_INDEX` mutation: ALWAYS build a new dict and rebind the module global. NEVER `_PROBE_INDEX[key] = value` or `_PROBE_INDEX.clear()`. Those break the atomic-rebind contract and you'll get racy reads.
- `_INSTALLED_PROGRAMS` IS mutated in place. Mismatched conventions are intentional — track which is which.

### Testing fixtures

- Hypothesis caches examples under `.hypothesis/`. After a structural test change, delete the cache or pass `--hypothesis-seed=<new>` to avoid replaying stale examples that no longer match the strategy shape.
- `MagicMock` is happy to record any call signature; that's a feature for `_enqueue_message` mock but means typos in your assertions can silently pass. Prefer `mock.assert_called_with(...)` over `assert mock.called`.
- `pytest-xdist` parallelism + module-global state = pain. Run the property tests with `-p no:xdist` until phase 7 is green.

### Things that look right but aren't

- Confirming the PostHog endpoint exists. `/api/projects/@current/live_debugger/programs/active` is assumed from the commented-out code in `manager.py`. Verify before running against a real instance. If it's wrong, the demo will silently get an empty list (`response.content` decodes to an empty `ProgramList`) or 404.
- `posthoganalytics.poller.Poller`'s start/stop semantics. Confirm `stop()` is idempotent and that calling `start()` twice doesn't double up.

---

## When the dust settles

After Phase 7 is green:
- Delete `libdebugger/program.py` (gone in design).
- Delete or empty `test/test_manager.py` (replaced by `test_manager_property.py`).
- Verify `pytest_stress/` still works — it keys off `__posthog_decorator`, which we preserve.
- Manual smoke test: spin up a PostHog instance (or stub one with `responses`), point `HogTraceManager` at it with a hand-written program, hit the wrapped endpoint, see `$hogtrace_capture` events.
- Update `CLAUDE.md`: the section about "Conditional expression feature is stubbed" / "Expression evaluation is not implemented" should be revised; the `Closures retain instrumentation` note can probably go away.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-13-hogtrace-manager.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per phase, review between phases, fast iteration.
2. **Inline Execution** — execute phases in this session using `executing-plans`, batch with checkpoints.

Which approach?
