# HogTrace Manager — Design Spec

**Date:** 2026-05-13
**Status:** Resolved (pending final user sign-off)
**Scope:** Finish the `HogTraceManager` lifecycle so that programs fetched from PostHog are reconciled into a live probe registry, wrappers are installed on target functions, and probes fire on call.

## Goal

Close the loop:

1. `HogTraceManager.start()` polls PostHog for the active `ProgramList`.
2. Each `Program` is reconciled into a module-level registry.
3. Functions named by probe specifiers are wrapped with `InstrumentationDecorator`.
4. On every call, the wrapper looks up the registry and runs whatever entry / exit probes are registered for it *right now*.
5. When the registry has no more probes for a function, the wrapper self-uninstalls on its next call.

In-function ("line") probes are out of scope for v1 but the design leaves a clean seam for them in v2.

## Non-goals (v1)

- Line / in-function probes (scaffolded but not executable in v1).
- Wildcard specifiers (`fn:myapp.users.*:entry`).
- `returns.Result` types — stdlib exceptions + logging.

Named-parameter access in entry / exit probes is *in* v1, achieved by retaining the existing entry-side bytecode injection so probes execute inside the function's own frame (see "The wrapper" below).

Hypothesis property tests are *in* v1 — they drive implementation order via red/green TDD (see "Testing approach").

## Core architectural choices

### Choice 1: Bytecode redirector, not reference swap

Wrapping a function mutates `fn.__code__` in place to a redirector that jumps through the decorator. The function object is *not* replaced.

Why: production callers capture references at startup (Django URL patterns, Flask routes, signal handlers, `from x import y` imports, `functools.partial`, callback registries). A reference swap (`module.f = Decorator(module.f)`) only catches lookups performed *after* the swap. The bytecode redirect catches them all.

Cost: ~50 lines of bytecode generation, already implemented in `libdebugger/bytecode.py` and working.

### Choice 2: Registry-lookup hot path (no probe state on the wrapper)

The wrapper carries no probe lists. On every call it reads the module-level `_PROBE_INDEX` and runs whatever it finds for its `qualname`. Probe changes become visible to the wrapper on the next call automatically — no notification or sync protocol between manager and wrapper.

Why: state lives in one place (the registry). No way for wrapper and registry to drift. Modeled after `cltrace`'s `*installed-programs*` pattern.

Cost: each `__call__` does up to three dict lookups. Acceptable for the small probe counts we expect.

**The read path is lock-free.** Writers (reconcile) build a fresh `_PROBE_INDEX` dict from scratch and assign it to the module global; readers (the hot path) grab a local reference and read from that snapshot. CPython makes both the module-attribute read and `dict.get()` atomic under the GIL. The reader's local reference cannot be invalidated mid-read — Python just bumps the old dict's refcount and lets it GC when no reader points at it. `_LOCK` only serializes writers against each other; it never blocks readers, and readers never block writers. This is strictly better than a `RwLock` for this access pattern because the read path takes *zero* synchronization, not just a shared lock.

The pattern only holds because writes are **whole-dict replacements**. The design enforces "always rebuild and rebind" — `_rebuild_probe_index` never mutates the existing dict in place. If we ever did, we'd lose atomicity (the dict could be mid-rehash when a reader iterates).

### Choice 3: Self-uninstalling wrapper

If a call ends with zero probes found for the wrapper's `qualname`, the wrapper takes a lock, re-checks under the lock, and if still empty restores its function's bytecode and removes itself from the function. No external lifecycle plumbing in the manager.

Why: keeps the manager dumb. The manager just edits the registry; cleanup is a property of the wrapper. Modeled directly after `cltrace`'s self-uninstall pattern.

Cost: cleanup is *lazy* — only triggers on the next call after the last probe is removed. For rarely-called functions, the wrapper hangs around. This is a by-design property: the cost of an idle wrapper is a few hundred bytes and one extra function call per invocation, both negligible, and the alternative ("walk all wrappers on uninstall and force cleanup") would require an extra registry of decorators that this design intentionally avoids.

### Choice 4: On-demand bytecode rebuild for line probes

Line probes are baked into bytecode at install time, so registry-lookup doesn't reach them. Instead, the wrapper keeps a single piece of state — the tuple of line probes it last built `instrumented_fn` for. On every `__call__`, identity-compare against the current registry tuple; if drifted, rebuild from `self.original_code` under the wrapper's lock. If the new line-probe tuple is empty, reset `instrumented_fn` to a clean copy of the original.

Why: same "no install/uninstall plumbing" property as entry/exit. The manager just edits the registry; the wrapper notices on next call.

v1: the rebuild stub is in place but performs no injection (line probes deferred). The slot in `_PROBE_INDEX` exists.

## Module-level state (`instrumentation.py`)

```python
_LOCK: threading.Lock                              # protects reconcile + index rebuild

_INSTALLED_PROGRAMS: Dict[str, Program]            # program_id -> Program (source of truth)

# Index rebuilt from _INSTALLED_PROGRAMS on every reconcile, atomic-rebound.
# Tuples (not lists) so readers get an immutable snapshot and identity-compare
# for line-probe drift detection is a cheap pointer compare.
_PROBE_INDEX: Dict[Tuple[str, str], Tuple[Tuple[Program, Probe], ...]]
              # key: (qualname, "entry" | "exit" | "line")
              # value: tuple of (Program, Probe) pairs
```

Everything is mutated under `_LOCK`. Hot-path reads are lock-free (atomic dict reference read).

## The wrapper (`InstrumentationDecorator`)

### Fields

```python
self.original_code: CodeType            # canonical, never mutated
self.wrapped_fn: FunctionType           # the user-visible function (its __code__ is the redirector)
self.instrumented_fn: FunctionType      # body executable; bytecode-injected with entry-probe call; rebuilt on line-probe drift
self.qualname: str                      # registry key
self._lock: threading.Lock              # serializes rebuild + cleanup
self._installed_line_probes: Tuple[Tuple[Program, Probe], ...]   # snapshot of what's baked into instrumented_fn
self.frames: List[FrameType]            # stack of captured frames so exit probes can run against the function's own frame
```

`wrapped_fn` and `instrumented_fn` are both required and not interchangeable:

- `wrapped_fn` is the call-site hook. Every existing reference (`from x import f`, captured callback, etc.) points at this object. Its `__code__` is permanently the redirector. We can't replace it — only mutate it.
- `instrumented_fn` is the executable body. A separate `FunctionType` over `self.original_code`, with the entry-probe call bytecode-injected at the function's first instruction (and, in v2, line probes injected at the right offsets). The decorator's `__call__` invokes this to actually run the user's logic. Rebuilt by atomic-rebind when line probes drift; rebuilt once at construction with the entry-probe call.

Entry probes run from *inside* `instrumented_fn`'s frame so they see the function's named parameters as locals. The frame is captured into `self.frames` at entry-probe time; the decorator's `__call__` `finally` pops it back off and uses it to execute exit probes — which means exit probes also see the function's locals (as they are at exit). Both entry and exit probes therefore get named-parameter access without any hogtrace API changes.

### Helper: `_any_probes_for`

```python
def _any_probes_for(qualname: str) -> bool:
    index = _PROBE_INDEX
    return bool(
        index.get((qualname, "entry"))
        or index.get((qualname, "exit"))
        or index.get((qualname, "line"))
    )
```

Used by the wrapper's cleanup re-check under `self._lock`.

### `qualname` resolution

The wrapper's `self.qualname` is set at construction time to the specifier that resolved to the function (e.g. `"myapp.users.create"`). This is the same string used as the registry key. The manager passes it in when constructing the wrapper:

```python
fn.__posthog_decorator = InstrumentationDecorator(fn, qualname=probe.spec.specifier)
```

Subsequent `add_probe` calls for the same function (via other probes / programs) re-use the existing wrapper; the qualname is set on first creation.

For v1, `_qualname_for_specifier` is the identity function — the specifier and the qualname are the same string. The indirection exists so v2 can normalize wildcards or class-method chains without changing the registry shape.

### `__call__` (the hot path)

```python
def __call__(self, *args, **kwds):
    # Line-probe drift check — identity compare, near-free.
    line = _PROBE_INDEX.get((self.qualname, "line"), ())
    if line is not self._installed_line_probes:
        with self._lock:
            line = _PROBE_INDEX.get((self.qualname, "line"), ())   # re-read under lock
            if line is not self._installed_line_probes:
                self.instrumented_fn = _build_instrumented(self, line)
                self._installed_line_probes = line

    # Entry probes run from inside instrumented_fn (bytecode-injected); the
    # function's frame is pushed onto self.frames by that call.
    previous_frame_top = self._peek_frame()
    exception: Optional[BaseException] = None
    retval: Any = None

    try:
        retval = self.instrumented_fn(*args, **kwds)
        return retval
    except BaseException as e:
        exception = e
        raise
    finally:
        function_frame = self._pop_frame()
        exit_ = _PROBE_INDEX.get((self.qualname, "exit"), ())

        if function_frame is not None and function_frame is not previous_frame_top:
            # instrumented_fn ran far enough to push its own frame — run exit probes against it.
            _run_probes(exit_, function_frame, retval=retval, exception=exception)
        elif function_frame is not None:
            # We popped a frame that doesn't belong to this call (instrumented_fn
            # crashed before its entry-probe injection ran). Restore it.
            self._push_frame(function_frame)

        # Self-uninstall if registry says nobody's home.
        entry_now = _PROBE_INDEX.get((self.qualname, "entry"), ())
        if not entry_now and not exit_ and not self._installed_line_probes:
            with self._lock:
                if not _any_probes_for(self.qualname):
                    self.cleanup()
                    try:
                        del self.wrapped_fn.__posthog_decorator
                    except AttributeError:
                        pass
```

Notes:

- Entry probes are *not* invoked from `__call__` directly. They run inside `instrumented_fn` via the bytecode-injected call to `_capture_caller_frame_and_run_entry_probes` (see below) so the captured frame has the function's named parameters as locals.
- Exit probes run in the `finally` against the function frame that was captured at entry. They therefore also see named locals (as they are at exit time).
- Recursive calls naturally stack via `self.frames`. The `previous_frame_top` check distinguishes "instrumented_fn pushed its own frame this call" from "instrumented_fn crashed before push" — the latter leaves whatever was on top alone.
- `BaseException` (not `Exception`) so probes also fire on `KeyboardInterrupt` / `SystemExit`. Exit probes get the exception object.

### `_capture_caller_frame_and_run_entry_probes`

Called from inside `instrumented_fn` at the function's first instruction, via bytecode injected by `_build_instrumented`. Exists in the current codebase at `instrumentation.py:190`; the only change vs. today is that it reads entry probes from `_PROBE_INDEX` instead of `self.entry_probes`:

```python
def _capture_caller_frame_and_run_entry_probes(self) -> None:
    try:
        caller_frame = sys._getframe(1)   # the instrumented_fn's frame
        self._push_frame(caller_frame)
        entry = _PROBE_INDEX.get((self.qualname, "entry"), ())
        _run_probes(entry, caller_frame)
    except Exception:
        logger.exception("entry-probe execution failed for %s", self.qualname)
```

Wrapped in `try / except` because nothing on the probe path may disrupt user code. Probe execution errors are logged and swallowed.

### `_build_instrumented` (v1)

```python
def _build_instrumented(decorator: InstrumentationDecorator, line_probes: Tuple[...]) -> FunctionType:
    # Inject a call to `decorator._capture_caller_frame_and_run_entry_probes()`
    # at the start of the function body. Same machinery as today
    # (`EntrypointInjector` + `generate_code_call_self_method`).
    injector = EntrypointInjector(
        code_generator=generate_code_call_self_method(
            decorator, "_capture_caller_frame_and_run_entry_probes",
        ),
    )
    code = injector.inject(decorator.original_code).to_code()

    if line_probes:
        logger.warning("Line probes deferred to v2; ignoring %d probe(s)", len(line_probes))
        # v2: extend `injector` to also weave probe calls at each line probe's offset.

    return FunctionType(
        code, decorator.wrapped_fn.__globals__,
        decorator.wrapped_fn.__name__, decorator.wrapped_fn.__defaults__,
        decorator.wrapped_fn.__closure__,
    )
```

v2 will add line-probe injection at the right `co_lnotab` offsets using existing `bytecode` library helpers — same machinery as the entry-probe injection above.

### `__init__` and `cleanup`

`__init__` matches the current implementation's shape: unwrap bound methods, capture `original_code`, generate the redirector bytecode, build `instrumented_fn` via `_build_instrumented` (which injects the entry-probe call), then mutate `wrapped_fn.__code__` to the redirector. New parameter compared to today: `qualname: str`.

`cleanup` is the existing method at `instrumentation.py:135`. It restores `wrapped_fn.__code__ = self.original_code`. Idempotent. Still called from `__del__` as a finalizer.

The marker attribute `fn.__posthog_decorator` is set in the manager when it first wraps the function (not in `__init__`) — see below.

## Manager (`manager.py`)

`HogTraceManager` keeps `start` / `stop` / poll-orchestration responsibilities. Reconcile logic lives in module-level free functions; `program.py` is deleted.

### Free functions

```python
def install_program(program: Program) -> None:
    with _LOCK:
        _INSTALLED_PROGRAMS[program.id] = program
        _rebuild_probe_index()
    for probe in program.probes:
        fn = resolve_target(probe.spec.specifier)
        if fn is None:
            logger.warning("Probe %s: target %s not resolvable; skipping",
                           probe.id, probe.spec.specifier)
            continue
        if not hasattr(fn, "__posthog_decorator"):
            fn.__posthog_decorator = InstrumentationDecorator(fn)
        # No further action — wrapper sees the new probes on next call.


def uninstall_program(program_id: str) -> None:
    with _LOCK:
        _INSTALLED_PROGRAMS.pop(program_id, None)
        _rebuild_probe_index()
    # Wrappers self-clean on next call when they detect empty registry slots.


def update_program(program: Program) -> None:
    uninstall_program(program.id)
    install_program(program)
```

### `_rebuild_probe_index`

Walks `_INSTALLED_PROGRAMS`, builds a fresh `Dict[(qualname, target), Tuple[(Program, Probe), ...]]`, atomic-rebinds the global. **Important:** when the contents for a given key are unchanged from the previous index, reuse the *existing tuple object* so that line-probe identity-compare in the wrapper's hot path stays stable. Implementation:

```python
def _rebuild_probe_index() -> None:
    global _PROBE_INDEX
    prev = _PROBE_INDEX
    new_raw: Dict[Tuple[str, str], List[Tuple[Program, Probe]]] = {}
    for program in _INSTALLED_PROGRAMS.values():
        for probe in program.probes:
            qualname = _qualname_for_specifier(probe.spec.specifier)
            target = probe.spec.target   # "entry" | "exit" | "line"
            new_raw.setdefault((qualname, target), []).append((program, probe))

    new_index: Dict[Tuple[str, str], Tuple[Tuple[Program, Probe], ...]] = {}
    for key, pairs in new_raw.items():
        new_tuple = tuple(pairs)
        existing = prev.get(key)
        new_index[key] = existing if existing == new_tuple else new_tuple
    _PROBE_INDEX = new_index
```

### `HogTraceManager._fetch_programs`

```python
def _fetch_programs(self) -> None:
    if not self.client.personal_api_key:
        logger.warning("No personal API key; skipping fetch")
        return
    try:
        resp = get(
            self.client.personal_api_key,
            "/api/projects/@current/live_debugger/programs/active",
            self.client.host,
            timeout=10,
        )
        incoming = {p.id: p for p in ProgramList.from_bytes(resp.content).programs}
    except Exception:
        logger.exception("Failed to fetch programs")
        return

    current_ids = set(_INSTALLED_PROGRAMS)
    incoming_ids = set(incoming)

    for pid in current_ids - incoming_ids:
        uninstall_program(pid)
    for pid in incoming_ids - current_ids:
        install_program(incoming[pid])
    for pid in current_ids & incoming_ids:
        if _INSTALLED_PROGRAMS[pid].hash != incoming[pid].hash:
            update_program(incoming[pid])
```

Errors during reconcile must not kill the poller. Any per-program install/uninstall failure is logged and skipped; the next poll tick will retry the failed program.

### Logging

`logging.getLogger("libdebugger.manager")` and `logging.getLogger("libdebugger.instrumentation")`. Replaces the never-implemented `self.log_info` / `self.log_warning`.

### `HogTraceManager.stop`

```python
def stop(self) -> None:
    if self.poller:
        self.poller.stop()
    with _LOCK:
        for pid in list(_INSTALLED_PROGRAMS):
            uninstall_program(pid)
    self.enabled = False
```

Clears the registry; wrappers self-clean on next call. No global "kill every wrapper now" hook in v1.

## Function resolution (`resolve_target`)

Given `"a.b.c.d"`:

1. Try `importlib.import_module("a.b.c.d")`. If a module, no callable here — return `None`.
2. Walk shorter prefixes downward: `"a.b.c"`, `"a.b"`, `"a"`. For each that imports, attempt `getattr` through the remaining components.
3. Return the first callable found.
4. If nothing resolves, return `None` and let the caller log.

Handles `module.function` and `module.Class.method`. Does **not** handle wildcards, instance attributes, or runtime-generated functions.

## Probe execution (`_run_probes`)

```python
def _run_probes(
    probes: Tuple[Tuple[Program, Probe], ...],
    frame: FrameType,
    *,
    retval: Any = None,
    exception: Optional[BaseException] = None,
) -> int:
    for program, probe in probes:
        try:
            req_store = get_store()
            if req_store is None:
                continue
            store = req_store.for_program(program_id=program.id)
            captures = execute_probe(
                program.program_bytecode, probe, frame, store,
                retval=retval, exception=exception,
            )
            if captures:
                _enqueue_message(program, probe, captures)
        except Exception:
            logger.exception("Probe execution failed for program=%s probe=%s",
                             program.id, probe.id)
    return len(probes)
```

Per `cltrace` semantics, the return value is the number of probes *registered for this fire point*, not the number whose predicate matched. This is what drives the self-uninstall check.

## Concurrency summary

| Operation | Lock | Notes |
|---|---|---|
| `__call__` reads `_PROBE_INDEX` | none | Atomic dict reference read; readers get a stable tuple snapshot |
| `__call__` rebuilds `instrumented_fn` | `self._lock` | Per-wrapper lock; re-reads registry under lock |
| `__call__` self-cleanup | `self._lock` | Re-checks via `_any_probes_for` under lock before restoring `__code__` |
| `install_program` / `uninstall_program` | `_LOCK` | Mutates `_INSTALLED_PROGRAMS`, rebuilds + rebinds `_PROBE_INDEX` |
| `_fetch_programs` reconcile body | none (calls into the above) | Polling thread errors are caught and logged; poller keeps running |

Lock order: manager always acquires `_LOCK` only; wrapper always acquires `self._lock` only. No nesting, no deadlock surface.

## Success criteria

1. `HogTraceManager.start()` polls and reconciles a `ProgramList`.
2. A program with one entry probe on a known function fires `_enqueue_message` when that function is called.
3. Removing the program (next poll returns it absent) causes the wrapper to restore `__code__` and remove `__posthog_decorator` on the function's next call.
4. Reinstalling the same program rewraps the function.
5. Two programs with entry probes on the same function both fire on a single call.
6. A poll cycle that returns an HTTP error does not stop the poller; the next tick still runs.
7. Concurrent stress test (multiple threads calling instrumented functions while another thread reconciles) does not raise `RuntimeError: dictionary changed size during iteration` or any race-condition error.

## Known limitations (carried forward to v2)

- Line probes are recognized by the registry but not executed; the manager logs a warning when one is encountered. `_build_instrumented` accepts the line-probe tuple but only weaves entry-probe injection; line-probe weaving lands in v2.
- Wildcard specifiers (`fn:myapp.users.*:entry`) are not resolved. The manager logs a warning and skips the probe.
- `resolve_target` only resolves callables reachable by walking module attributes — `module.func` and `module.Class.method`. Specifically out of scope for v1:
  - **Closures defined inside other functions** (`def outer(): def inner(): ...; return inner`) — `inner` lives in `outer`'s cell vars, not in a module namespace.
  - **Lambdas** — no qualified name to specify against. (v2 idea: resolve via line-probe-style file/line addressing — see Future work.)
  - **Functions stored in module-level containers** (`HANDLERS = [foo]`, `DISPATCH = {"x": foo}`) — dotted-name walking can't index.
  - **Per-instance method overrides** (`instance.method = ...`) — we resolve the class-level version only.
  - **Functions monkey-patched after import** — we resolve whatever's currently at the module attribute. Behavior depends on patcher ordering.
  - **Descriptors / properties / `__init_subclass__` hooks** — `getattr` doesn't return a normal callable.

## Testing approach

Driven by Hypothesis property tests written **before** the implementation, red/green TDD. Each phase adds one property test, watches it fail, then adds the minimum production code to make it green, then a small refactor pass.

Phase ordering matches the dependency graph between properties — each property assumes the earlier ones already hold:

- **Phase 0 — Scaffolding.** Hypothesis strategies for `Program`, `Probe`, and function specifiers. A `FunctionPool` of stable target functions defined in the test module. A fixture that resets all module globals between tests (`_INSTALLED_PROGRAMS`, `_PROBE_INDEX`, the `__posthog_decorator` attribute on each pool function, and `wrapped_fn.__code__`). A mock for `_enqueue_message`. End state: harness imports clean, zero tests.
- **Phase 1 — Behavior preservation (P7).** Wrap and unwrap a function with no probes installed; assert `f(args) == original_f(args)` for any input. Forces `__init__` / `cleanup` / redirector wiring to work end-to-end before probes enter the picture.
- **Phase 2 — Trace fidelity (P1).** Install a program with one entry probe; call once; assert `_enqueue_message` fired exactly once with the right `(program, probe)`. Forces `install_program` + `_rebuild_probe_index` + `_capture_caller_frame_and_run_entry_probes` registry-lookup.
- **Phase 3 — Registry / index consistency (P2, P3).** Hypothesis stateful machine that generates random sequences of `install_program` / `uninstall_program` / `update_program`. Invariants checked after every step: `set(_INSTALLED_PROGRAMS) == expected_program_ids`, every `(program, probe)` in `_PROBE_INDEX` has `program.id in _INSTALLED_PROGRAMS`.
- **Phase 4 — Self-cleanup convergence (P4).** Add a step type to the machine: "call function once." Invariant: after uninstalling every program targeting `f` and calling `f` once, `not hasattr(f, '__posthog_decorator') and f.__code__ is original_code_for(f)`.
- **Phase 5 — Order-independence (P5).** Run the machine twice on permuted input sequences with the same final program set; assert the resulting `_PROBE_INDEX` is identical between runs.
- **Phase 6 — Recursion safety (P6).** Add recursive functions to `FunctionPool`. Assert `_enqueue_message` call count for a depth-`N` recursive call equals `N × (entry_count + exit_count)`.
- **Phase 7 — Thread interleaving (P8).** Hypothesis stateful machine + a small worker pool that calls instrumented functions from multiple threads while another thread runs install / uninstall steps. Assert P1, P4, P7 still hold and no `RuntimeError: dictionary changed size during iteration` ever occurs.

Phase 7 is the costliest — Hypothesis stateful machines for concurrent code take real care to get right. Plan for it to take a half-day on its own; everything earlier should move fast.

The implementation plan (written next via `superpowers:writing-plans`) lays out the file-level changes per phase.

## Future work

- **Line probes (v2):** complete `_build_instrumented` to weave probe-call bytecode at each line probe's offset using `bytecode` library helpers. The registry slot, drift-detection logic, and rebuild path are already in v1 — only the bytecode-weaving is missing.
- **Line-probe-driven lambda / closure resolution:** when a probe specifier addresses a lambda or nested function by file + line (e.g. `fn:myapp/views.py:42:entry`), resolve by walking the module's `co_consts` recursively for code objects matching that location. Enables instrumentation of callables that have no module-level name.
- **Wildcard specifiers (`fn:myapp.users.*:entry`):** resolve to all matching module attributes at reconcile time. Watch for explosive matches (`fn:*:entry`) — likely want a cap and a warning.
- **`execute_probe_with_bindings` API in hogtrace:** accept a pre-built `dict[name, value]` as the locals view, removing the need for bytecode-injection just to get named-param access. Would let us simplify the wrapper further once landed.
- **Eager teardown:** if we ever want non-lazy cleanup, a `weakref.WeakSet[InstrumentationDecorator]` lets `HogTraceManager.stop()` (and similar) walk every wrapper and call `cleanup()` immediately. Not needed for v1; the lazy property is correct for normal operation.

## Open questions

None blocking. The endpoint `/api/projects/@current/live_debugger/programs/active` is assumed correct; verify against the PostHog server before integration testing.

---

## Appendix: Design discussion history

The threads below capture the back-and-forth that produced the design above. All have been resolved; the body of the spec is the source of truth. Kept here for context on *why* particular choices were made.

### Thread A: Lock-free read path (resolved into Choice 2)

[H]: Okay, can we do this without locking is my question? Is there a thread-safe dictionary in python? Is there a RwLock equivalent where we can have multiple readers and only one writer so under the heavy read path we don't lock other readers in that case?

[A]: We are already lock-free on the read path — that's the whole point of the atomic-rebind pattern. CPython dict `.get()` and module-attribute read are both atomic under the GIL (the bytecode op runs to completion without releasing it). The pattern is RCU-style:

```
# writer (under _LOCK only to serialize against OTHER writers):
new = {...build fresh dict...}
_PROBE_INDEX = new      # atomic module-attr rebind

# reader (no lock at all):
idx = _PROBE_INDEX                          # atomic
entry = idx.get((qualname, "entry"), ())    # atomic; reads from the snapshot we just grabbed
```

The reader's local `idx` reference can't be invalidated by a concurrent write — Python just bumps the refcount on the old dict and lets it GC when nobody points at it. So `_LOCK` never blocks readers, and readers never block writers. We get "infinite readers, serialized writers" for free.

Stdlib doesn't ship a true RwLock (`threading.RLock` is *re-entrant*, not reader-writer). Third-party `readerwriterlock` exists, but we don't need one — atomic rebind is strictly better than RwLock for this access pattern because the read path takes *zero* synchronization, not just a shared lock.

Memory cost: each reconcile allocates a fresh dict. Old one GC's when the last in-flight reader drops it. Reconciles run every 30s by default, so churn is negligible.

The one caveat: this only works because writes are *whole-dict replacements*. If we ever mutate the dict in place (e.g. `_PROBE_INDEX[k] = v`), we'd lose atomicity (the dict could be mid-rehash when a reader iterates). The design enforces "always rebuild and rebind" — `_rebuild_probe_index` never mutates the existing dict.

[H]: Cool, resolve and mention it

[A]: Folded into Choice 2 as a permanent paragraph.

### Thread B: Lazy cleanup framing (resolved into Choice 3)

[H]: Acceptable always tbh.

[A]: Agreed. Reworded to "by-design property" rather than "demo-acceptable."

[H]: Resolve

[A]: Logged.

### Thread C: PBT from day one (resolved into Testing approach)

[H]: I would start with a strong propery-based testing harness that tests installing and uninstalling flows. Basically the input is a generator of program installing and uninstallings with various functions. The properties are fairly clear I think, calling the functions always returns the expected values and after calling the function the decorator should only exist IFF the function is traced. Any other property?

[A]: Good list. Properties I'd add on top of the two you named:

1. **Trace fidelity / call count:** for a single call, `_enqueue_message` is invoked exactly `len(entry_probes_for_fn) + len(exit_probes_fired_for_fn)` times. On normal return both fire; on exception only exit fires (with `exception=` set). Entry never fires twice for one call.

2. **Registry consistency post-reconcile:** after a reconcile pass with input set `S = {p1, p2, ...}`, `set(_INSTALLED_PROGRAMS.keys()) == {p.id for p in S}` exactly. No orphans, no missing.

3. **Index consistency:** every `(program, probe)` pair appearing anywhere in `_PROBE_INDEX` has `program.id in _INSTALLED_PROGRAMS`. No dangling references to uninstalled programs.

4. **Self-cleanup convergence:** after uninstalling every program targeting function `F` and then calling `F` once, `hasattr(F, '__posthog_decorator')` is `False` AND `F.__code__ is original_code_for_F`.

5. **Order-independence:** for any permutation of a fixed multiset of install/uninstall operations that ends in the same final program set, the resulting `_PROBE_INDEX` is identical. (Reconciles are pure functions of `_INSTALLED_PROGRAMS`.)

6. **Recursion safety:** for a function `F` that calls itself `N` times within one outer call, `_enqueue_message` fires exactly `N × (entry_count + exit_count)` times. No deadlock, no missed probes, no double-firing.

7. **Behavior preservation:** instrumented `F(args)` returns the same value (or raises the same exception type) as uninstrumented `F(args)`, for any `args` — modulo probe side effects.

8. **Thread interleaving:** under concurrent install/uninstall from one thread and `F(...)` calls from many threads, properties 1, 4, 7 still hold. No `RuntimeError: dictionary changed size during iteration` or partial-state observations.

You'd noted property tests as "punt to post-demo" in non-goals up top — sounds like you want to elevate them. If so, the implementation plan grows: Hypothesis scaffolding is real work (probably half a day to get strategies + stateful machine humming), but it'd front-load correctness and make the reviewer story much stronger. Want me to move "hypothesis property-test harness" into v1 scope when we resolve? It'd reshape phase ordering in the plan.

[H]: Yes, I def want PBT from the start here, let's start with this harness. Red/Green TDD baby.

[A]: Confirmed. Implementation plan reshaped around "write a property → watch it go red → write the smallest thing that turns it green → refactor → next property." Property ordering and phase plan now in the "Testing approach" section.

[H]: Sounds good, resolve

[A]: Non-goal line removed; Testing approach section added with the phase ordering.

### Thread D: Named-param access in entry/exit probes (resolved by retaining bytecode injection)

[H]: I don't think it requires that, right? You can extract the arglist using introspection mechanisms?

[A]: True at the Python layer — `inspect.signature(fn).bind(*args, **kwds).arguments` gives you `dict[name, value]` for free. The bottleneck is *on the hogtrace side*: `execute_probe` takes a `PyFrame` and reads variables via the frame's `f_locals` machinery. It has no alternative entry point that accepts a pre-built `dict` as the "locals view."

Three ways to lift the limitation, in increasing effort:

- **(a) Bytecode injection (status quo before the refactor):** run entry probes from *inside* `instrumented_fn` so the function's real frame already has the bound parameters in `f_locals`. Works with the existing hogtrace API, no cross-repo changes needed. Cost: have to keep the entry-side bytecode injection in `instrumented_fn`.
- **(b) Hogtrace API addition:** add `execute_probe_with_bindings(program, probe, bindings: dict, store, ...)` that builds its dispatcher view over a dict instead of a frame. Cleanest end-state, but it's a hogtrace edit and a cross-repo dependency.
- **(c) Frame synthesis:** create a fake `PyFrame` with the bindings as locals via `ctypes` or compile-and-exec trickery. Don't.

[H]: Ah shit, you are right, we do not have locals available there. Let's keep entry and exit probes as part of the instrumentation then. Not as part of the decorator.

[A]: Two readings of "as part of the instrumentation":

**Reading A — Pragmatic / matches the existing code.** Entry-probe execution is *bytecode-injected* into `instrumented_fn`. The frame is captured into the decorator's frame-stack. Exit probes run in the decorator's `__call__` `finally`, but against *that captured frame*, so they see the function's locals at exit time. Existing code already works this way.

**Reading B — Symmetric / cleaner decorator.** Both entry *and* exit probes are bytecode-injected into `instrumented_fn` via try/finally bytecode. Decorator's `__call__` becomes pure dispatch. Cleaner but try/finally bytecode injection is meaningfully harder (edge cases around `co_consts`, `co_exceptiontable` for 3.11+, generators, async).

[H]: I meant A, how we were doing things until now basically. Where we inject an entry/exit call into instrumented_fn

[A]: Got it. Restored entry-side bytecode injection + frame-stack pattern in the wrapper section. Exit runs in `__call__`'s finally against the captured frame so both entry and exit see named params. Named-param limitation removed from non-goals.

### Thread E: Concrete `resolve_target` limitations (resolved into Known limitations)

[H]: What do you mean by that? what else should we need to add?

[A]: Just calling out the shapes that don't survive a `module.getattr.getattr...` walk: closures inside other functions, lambdas, functions stored in module-level containers, per-instance method overrides, monkey-patched functions, descriptors/properties. None block the demo.

[H]: Yeah, fair enough. I think I had a bit of magic to resolve lambdas based on line probes but we can add that later.

[A]: Deferred to v2. Line-probe-driven lambda resolution captured in Future work.

[H]: Yes, resolve.

[A]: Future work section added with line-probe-driven lambda resolution + wildcard support.

### Thread F: "Lazy cleanup" is design, not limitation (resolved by dropping bullet)

[H]: That's not a limitation, that's by design.

[A]: Agreed. Choice 3 already calls out the lazy property explicitly — bullet removed from Known limitations.

[H]: Resolve

[A]: Dropped.
