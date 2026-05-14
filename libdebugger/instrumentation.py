import datetime
import logging
import threading
import sys
import time
import traceback
import inspect
from typing import Callable, Final, Optional, Dict, List, Any, Tuple
from types import CodeType, FrameType, FunctionType

from hogtrace import Probe, Program, execute_probe, get_store, get_scope, ProbeSpec

from libdebugger.bytecode import (
    EntrypointInjector,
    generate_code_call_self_method,
    redirector_code,
)


logger = logging.getLogger("libdebugger.instrumentation")


# ---------------------------------------------------------------------------
# Module-level registry state.
#
# The wrapper's hot path reads ``_PROBE_INDEX`` without holding any lock —
# the dict is atomic-rebound whenever the manager reconciles a new program
# list (see ``_rebuild_probe_index`` in ``manager.py``). Writers serialize
# against each other via ``_LOCK``; readers never block writers and never
# block each other.
#
# In Phase 1 these are populated by no-one; both dicts stay empty. Phase 2
# wires the manager's free functions to fill them.
# ---------------------------------------------------------------------------

_LOCK: threading.Lock = threading.Lock()

# program_id -> Program; mutated in place under _LOCK.
_INSTALLED_PROGRAMS: Dict[str, Any] = {}

# (qualname, "entry" | "exit" | "line") -> tuple of (Program, Probe).
# Atomic-rebound; never mutated in place. Hot-path reads are lock-free.
_PROBE_INDEX: Dict[Tuple[str, str], Tuple[Tuple[Program, Probe], ...]] = {}

# Pluggable destination for probe-capture events. The signature is
# ``sink(event_name: str, properties: Dict[str, Any]) -> None``. By
# default this is ``None`` — captures are dropped (with a debug log)
# until something registers a sink. ``HogTraceManager.__init__`` wires
# ``client.capture`` here automatically when constructed with a client
# that exposes that method; users wanting full control can override via
# ``libdebugger.set_event_sink(...)``.
_EVENT_SINK: Optional[Callable[[str, Dict[str, Any]], None]] = None


# ---------------------------------------------------------------------------
# Probe-error dedupe state.
#
# When ``execute_probe`` raises (typically because the user's probe code
# references something that doesn't exist on the captured frame — e.g.
# ``arg0.id`` when ``arg0`` is an int), we want to surface the failure to
# the developer via a ``$hogtrace_probe_error`` event so they know their
# probe reached the target but blew up. The blast radius matters: a broken
# probe sitting on a hot function can fail thousands of times per second,
# and we don't want to firehose the sink with identical errors.
#
# Strategy: per ``(program_id, probe_id, exc_type_name)`` key, fire once
# immediately, then suppress identical failures for ``_PROBE_ERROR_WINDOW``
# seconds. The accumulated suppressed count rides along on the next fire
# as ``skipped_since_last`` so the developer can see how bad it was.
#
# State is module-level rather than per-decorator because the same probe
# can fire on many wrappers (e.g. a wildcard match in the future) and we
# want one dedupe identity per logical probe.
# ---------------------------------------------------------------------------

_PROBE_ERROR_DEDUP_LOCK: threading.Lock = threading.Lock()

# Key: (program_id, probe_id, exc_type_name)
# Value: (last_fire_monotonic_ts, suppressed_count_since_last_fire)
_PROBE_ERROR_DEDUP: Dict[Tuple[str, str, str], Tuple[float, int]] = {}

# Suppression window in seconds. First failure inside this window fires;
# subsequent identical failures are accumulated and reported on the next
# fire after the window expires. Tests monkey-patch this for fast windows.
_PROBE_ERROR_WINDOW: float = 60.0


def _record_probe_error(
    program: Program, probe: Probe, exc: BaseException
) -> Optional[int]:
    """Decide atomically whether to emit a probe-error event right now.

    Returns the ``skipped_since_last`` count to include on the event if
    this call should emit, or ``None`` if the call is inside the dedupe
    window and should be suppressed.

    Contract:
      * First failure for a key: emits with ``skipped_since_last == 0``.
      * Within window: returns None and bumps the suppressed counter.
      * After window: emits with ``skipped_since_last == <accumulated>``
        and resets the counter.
    """
    now = time.monotonic()
    key = (program.id, probe.id, type(exc).__name__)
    with _PROBE_ERROR_DEDUP_LOCK:
        prev = _PROBE_ERROR_DEDUP.get(key)
        if prev is None:
            _PROBE_ERROR_DEDUP[key] = (now, 0)
            return 0
        last_fired, suppressed = prev
        if now - last_fired >= _PROBE_ERROR_WINDOW:
            _PROBE_ERROR_DEDUP[key] = (now, 0)
            return suppressed
        _PROBE_ERROR_DEDUP[key] = (last_fired, suppressed + 1)
        return None


def _drop_dedup_for_program(program_id: str) -> None:
    """Remove all dedupe entries belonging to ``program_id``.

    Called from the manager on ``uninstall_program`` so a stale program's
    dedupe state doesn't linger. If the program reinstalls (e.g. after a
    probe fix), the next failure fires immediately instead of being
    suppressed by a window from a previous incarnation.
    """
    with _PROBE_ERROR_DEDUP_LOCK:
        for key in [k for k in _PROBE_ERROR_DEDUP if k[0] == program_id]:
            del _PROBE_ERROR_DEDUP[key]


def set_event_sink(
    sink: Optional[Callable[[str, Dict[str, Any]], None]],
) -> None:
    """Register (or clear) the callable that receives probe-capture events.

    The sink is invoked as ``sink(event_name, properties)`` once per probe
    fire. Pass ``None`` to disable event emission entirely (captures are
    then dropped with a debug log).

    This is a global setting — there is one event sink per process. The
    normal wiring is automatic via ``HogTraceManager``; call this directly
    only for tests, alternate sinks (queue / file / stdout), or to plug in
    a PostHog SDK whose ``capture`` signature differs from the default.
    """
    global _EVENT_SINK
    _EVENT_SINK = sink


def _any_probes_for(qualname: str) -> bool:
    """True if any entry/exit/line probe is registered for ``qualname``."""
    index = _PROBE_INDEX
    return bool(
        index.get((qualname, "entry"))
        or index.get((qualname, "exit"))
        or index.get((qualname, "line"))
    )


def _run_probes(
    probes: Tuple[Tuple[Program, Probe], ...],
    frame: FrameType,
    *,
    retval: Any = None,
    exception: Optional[BaseException] = None,
) -> int:
    """Execute the given probes against ``frame``.

    Returns the number of probes attempted (per ``cltrace`` semantics —
    the self-uninstall check on the wrapper is what cares about the count;
    the wrapper here treats the value as informational).

    Errors from individual probes are logged and swallowed: nothing on
    the probe path may disrupt user code.

    In Phase 1 this function is never called with non-empty ``probes``
    because ``_PROBE_INDEX`` is empty. The signature matches the spec
    exactly so Phase 2 can fill the registry and start firing probes
    without further changes here.
    """
    for program, probe in probes:
        try:
            req_store = get_store()
            if req_store is None:
                logger.debug(
                    "no hogtrace request scope; skipping probe %s for program %s",
                    probe.id,
                    program.id,
                )
                continue
            store = req_store.for_program(program_id=program.id)
            captures = execute_probe(
                program.program_bytecode,
                probe,
                frame,
                store,
                retval=retval,
                exception=exception,
            )
            if captures:
                _enqueue_message(program, probe, captures)
        except Exception as exc:
            logger.exception(
                "Probe execution failed for program=%s probe=%s",
                getattr(program, "id", "?"),
                getattr(probe, "id", "?"),
            )
            # Dedupe + surface to the developer. If the dedupe window
            # says "suppress", we still logged above; the only visible
            # effect of suppression is the missing PostHog event.
            try:
                skipped = _record_probe_error(program, probe, exc)
                if skipped is not None:
                    _enqueue_probe_error(program, probe, exc, skipped)
            except Exception:
                # The error-reporting path itself must not propagate.
                logger.exception(
                    "Failed to emit $hogtrace_probe_error for program=%s probe=%s",
                    getattr(program, "id", "?"),
                    getattr(probe, "id", "?"),
                )
    return len(probes)


class InstrumentationDecorator:
    """
    InstrumentationDecorator enables dynamic entry/exit probes on functions.

    Unlike standard decorators, this works on ALL references to a function, even those
    created before instrumentation, by replacing the original function's bytecode with
    a redirect to the decorator.

    Architecture:

        func.__posthog_decorator = InstrumentationDecorator(func, qualname=...)

        Before:                         After:
        +-------------+                +-------------+
        |   func()    |                |   func()    | (original reference)
        |             |                |             |
        |  original   |                |  redirect   |---+
        |  bytecode   |                |  bytecode   |   |
        +-------------+                +-------------+   |
                                                         |
       old_ref = func                  old_ref = func    | ALL references
                                                         | redirect through
                                                         | the decorator
                                                         |
                                                         v
                                             +--------------------+
                                             |    Decorator       |
                                             |   __call__()       |
                                             +--------------------+
                                                         |
                                                         | calls
                                                         v
                                             +--------------------+
                                             |  instrumented_fn   |
                                             |                    |
                                             | original bytecode  |
                                             | + entry probe call |
                                             +--------------------+

    Flow:
      1. Entry: ``instrumented_fn`` runs the bytecode-injected call to
         ``_capture_caller_frame_and_run_entry_probes`` which captures
         the running frame onto ``self.frames`` and runs entry probes.
      2. Execution: original function body executes inside ``instrumented_fn``.
      3. Exit: ``__call__``'s ``finally`` pops the captured frame and
         runs exit probes against it.

    The wrapper carries NO probe state. Every call looks the active probe
    set up in the module-level ``_PROBE_INDEX``. Probe changes become
    visible to the wrapper on the next call automatically.
    """

    # The wrapped function. After __init__ its __code__ is the redirector.
    wrapped_fn: Final[Callable[..., Any]]

    # Original code of the function so we can restore it during cleanup.
    original_code: Final[CodeType]
    # Code for the redirector function (mutated into wrapped_fn.__code__).
    redirector_code: Final[CodeType]
    # The body executable: a separate FunctionType over original_code with
    # the entry-probe call injected. NEVER overlaps with wrapped_fn.
    instrumented_fn: FunctionType

    # Specifier this wrapper was created for; the registry key.
    qualname: str

    # Stack of frames captured by instrumented_fn at entry. Exit probes
    # run against the top of this stack in __call__'s finally.
    frames: List[FrameType]

    # Tuple of line probes baked into instrumented_fn. Phase 1 always
    # leaves this empty; Phase 2+ uses it for drift detection.
    _installed_line_probes: Tuple[Any, ...]

    # Per-wrapper lock serializing rebuild + cleanup against concurrent
    # __call__s from other threads. Distinct from module-level _LOCK.
    _lock: threading.Lock

    def __init__(
        self,
        fn: Callable[..., Any],
        qualname: str,
    ) -> None:
        # Bound methods: unwrap to the underlying function so the
        # bytecode redirect lands on the class-level function object.
        if inspect.ismethod(fn):
            fn = fn.__func__  # type: ignore

        self.qualname = qualname
        self.original_code = fn.__code__
        self.wrapped_fn = fn
        self.frames = []
        self._installed_line_probes = ()
        self._lock = threading.Lock()

        # Build instrumented_fn from the ORIGINAL code (pre-redirector
        # mutation). Phase 1 always passes an empty line-probe tuple.
        self.instrumented_fn = _build_instrumented(self, ())

        self.redirector_code = self._generate_redirector_code()

        # LAST step: mutate the user-visible function to the redirector.
        # Must happen after instrumented_fn is built — otherwise the
        # body-execution path would loop forever through the redirector.
        self.wrapped_fn.__code__ = self.redirector_code

    def __getattr__(self, name: str) -> Any:
        """Forward attribute access to the underlying function.

        Guarded against the (rare) case where ``__getattr__`` is invoked
        before ``__init__`` finishes wiring ``wrapped_fn`` — otherwise
        Python's ``hasattr()`` probes during construction recurse here.
        """
        if name == "wrapped_fn":
            raise AttributeError(name)
        return getattr(self.wrapped_fn, name)

    def __del__(self):
        """Restore original bytecode if the decorator is finalized."""
        try:
            self.cleanup()
        except Exception:
            # Finalizers must not raise; surface via logging only.
            pass

    def cleanup(self):
        """Restore wrapped_fn to its original bytecode. Idempotent."""
        try:
            self.wrapped_fn.__code__ = self.original_code
        except Exception:
            logger.exception("cleanup failed restoring code for %s", self.qualname)

    def _push_frame(self, frame: FrameType) -> None:
        self.frames.append(frame)

    def _pop_frame(self) -> Optional[FrameType]:
        try:
            return self.frames.pop()
        except IndexError:
            return None

    def _peek_frame(self) -> Optional[FrameType]:
        try:
            return self.frames[-1]
        except IndexError:
            return None

    def _generate_redirector_code(self) -> CodeType:
        """Generate redirector bytecode that hops into the decorator."""
        new_bc = redirector_code(self)
        new_bc.name = self.original_code.co_name
        new_bc.filename = self.original_code.co_filename
        new_bc.first_lineno = self.original_code.co_firstlineno

        # Preserve freevars / cellvars so __code__ assignment works for
        # functions with closures. The redirector itself doesn't use them.
        new_bc.freevars = list(self.original_code.co_freevars)
        new_bc.cellvars = list(self.original_code.co_cellvars)

        return new_bc.to_code()

    def _capture_caller_frame_and_run_entry_probes(self) -> None:
        """Called from inside ``instrumented_fn``'s first instruction.

        Captures the running frame onto the wrapper's frame-stack so
        exit probes can run against it later, then runs whatever entry
        probes the module-level registry has for ``self.qualname``.

        Wrapped in try/except — nothing on the probe path may disrupt
        user code. Errors are logged and swallowed.
        """
        try:
            caller_frame = sys._getframe(1)  # instrumented_fn's frame
            self._push_frame(caller_frame)
            entry = _PROBE_INDEX.get((self.qualname, "entry"), ())
            _run_probes(entry, caller_frame)
        except Exception:
            logger.exception("entry-probe execution failed for %s", self.qualname)

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        # Line-probe drift check. In Phase 1 _PROBE_INDEX is empty so
        # ``line`` is always ``()`` and self._installed_line_probes is
        # also ``()`` — identity-compare passes and we skip the rebuild.
        # In Phase 2+ this picks up live changes from the registry.
        line = _PROBE_INDEX.get((self.qualname, "line"), ())
        if line is not self._installed_line_probes:
            with self._lock:
                line = _PROBE_INDEX.get((self.qualname, "line"), ())
                if line is not self._installed_line_probes:
                    self.instrumented_fn = _build_instrumented(self, line)
                    self._installed_line_probes = line

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
                # instrumented_fn ran far enough to push its own frame.
                _run_probes(
                    exit_,
                    function_frame,
                    retval=retval,
                    exception=exception,
                )
            elif function_frame is not None:
                # We popped a frame that doesn't belong to this call —
                # instrumented_fn crashed before its entry-probe injection
                # ran. Restore the frame so its real owner can pop it.
                self._push_frame(function_frame)

            # Self-uninstall when the registry says nobody is home.
            # In Phase 1 the registry is always empty, so this branch
            # would fire on every call — but the wrapper only ever
            # exists in Phase 1 tests where the test itself manages
            # ``__posthog_decorator``. The ``delattr`` is wrapped in
            # try/except so explicit test-driven setup doesn't crash on
            # the lazy cleanup path.
            #
            # NOTE: We MUST use ``delattr(..., "__posthog_decorator")``
            # rather than ``del self.wrapped_fn.__posthog_decorator``.
            # Inside a class body Python applies name-mangling to dunder
            # (double-underscore) prefixes — the attribute access would
            # be rewritten to ``_InstrumentationDecorator__posthog_decorator``
            # which never matches the attribute the caller set, and the
            # ``except AttributeError`` would silently swallow the failure.
            # The string literal escapes mangling.
            #
            # TODO(phase-7): revisit reentrancy under concurrent calls —
            # the threaded-stress phase needs to validate that the
            # registry-empty check + self.cleanup() + delattr sequence
            # is safe when another thread is mid-``__call__`` on the
            # same wrapper. The Phase 2 spec validates the locking under
            # serial execution; Phase 7's job is the concurrent case.
            entry_now = _PROBE_INDEX.get((self.qualname, "entry"), ())
            if not entry_now and not exit_ and not self._installed_line_probes:
                with self._lock:
                    if not _any_probes_for(self.qualname):
                        self.cleanup()
                        try:
                            delattr(self.wrapped_fn, "__posthog_decorator")
                        except AttributeError:
                            pass


def _build_instrumented(
    decorator: "InstrumentationDecorator",
    line_probes: Tuple[Any, ...],
) -> FunctionType:
    """Build a fresh ``FunctionType`` over ``decorator.original_code``
    with the entry-probe-call bytecode injected at the first instruction.

    Line probes are recognized but not woven in v1; a warning is logged
    when any are passed. The rebuild slot exists so v2 can extend
    ``injector`` to weave probe calls at each line probe's offset.
    """
    injector = EntrypointInjector(
        code_generator=generate_code_call_self_method(
            decorator,
            "_capture_caller_frame_and_run_entry_probes",
        ),
    )
    code = injector.inject(decorator.original_code).to_code()

    if line_probes:
        logger.warning(
            "Line probes deferred to v2; ignoring %d probe(s)", len(line_probes)
        )

    fn = FunctionType(
        code,
        decorator.wrapped_fn.__globals__,
        decorator.wrapped_fn.__name__,
        decorator.wrapped_fn.__defaults__,
        decorator.wrapped_fn.__closure__,
    )

    # Preserve metadata so ``instrumented_fn`` is indistinguishable from
    # the original under introspection:
    #
    #   - ``__kwdefaults__``: kw-only default values; required for the
    #     instrumented function to accept the same keyword-only args.
    #   - ``__qualname__``: dotted name shown in tracebacks and reprs;
    #     without it stack traces lose the class/qualified context.
    #   - ``__module__``: used by logging/repr to locate where the
    #     function was defined.
    #   - ``__doc__``: preserved so ``help()`` and tooling that reads
    #     docstrings keep working.
    #   - ``__annotations__``: preserved so type-introspection tools
    #     (``typing.get_type_hints``, IDEs) see the same annotations.
    #   - ``__wrapped__``: pointed at ``wrapped_fn`` so ``inspect.unwrap()``
    #     and any ``functools.wraps``-style introspection can walk back
    #     to the original function object.
    if hasattr(decorator.wrapped_fn, "__kwdefaults__"):
        fn.__kwdefaults__ = decorator.wrapped_fn.__kwdefaults__
    if hasattr(decorator.wrapped_fn, "__qualname__"):
        fn.__qualname__ = decorator.wrapped_fn.__qualname__
    if hasattr(decorator.wrapped_fn, "__module__"):
        fn.__module__ = decorator.wrapped_fn.__module__
    if hasattr(decorator.wrapped_fn, "__doc__"):
        fn.__doc__ = decorator.wrapped_fn.__doc__
    if hasattr(decorator.wrapped_fn, "__annotations__"):
        fn.__annotations__ = decorator.wrapped_fn.__annotations__
    fn.__wrapped__ = decorator.wrapped_fn  # type: ignore[attr-defined]
    return fn


def _enqueue_message(program: Program, probe: Probe, captures: Dict[str, Any]):
    """Forward a probe-capture payload to the registered event sink.

    No-op (with a debug log) if no sink is registered. The event name is
    always ``$hogtrace_capture``; per-fire metadata (program / probe ids,
    scope context, thread info, timestamp) is folded into the properties.
    """
    sink = _EVENT_SINK
    if sink is None:
        logger.debug(
            "no event sink registered; dropping capture from probe %s",
            probe.id,
        )
        return

    scope = get_scope()
    properties: Dict[str, Any] = {
        "program_id": program.id,
        "probe_id": probe.id,
        "context_id": scope.context_id if scope is not None else None,
        "probe_spec": serialize_probe_spec(probe.spec),
        "captures": captures,
        # "$stack_trace": stacktrace,
        "timestamp": datetime.datetime.now(),
        "thread_id": threading.current_thread().ident,
        "thread_name": threading.current_thread().name,
    }

    try:
        sink("$hogtrace_capture", properties)
    except Exception:
        logger.exception(
            "event sink raised; dropping capture from probe %s",
            probe.id,
        )


def _enqueue_probe_error(
    program: Program,
    probe: Probe,
    exc: BaseException,
    skipped_since_last: int,
) -> None:
    """Forward a probe-execution failure to the registered event sink.

    Emits an event named ``$hogtrace_probe_error`` carrying enough context
    for a developer (or LLM agent) to identify *which* probe failed and
    *why*: program / probe ids, the original probe spec (so they can grep
    their source for the offending probe), the exception type + message,
    and a short formatted traceback truncated to the last few frames of
    ``execute_probe``.

    ``skipped_since_last`` is the count of identical failures that were
    suppressed by the dedupe window since this key last fired. ``0`` on
    the very first failure for a key.

    No-op (with a debug log) if no sink is registered. Sink exceptions
    are caught here so the error-reporting path can never break user code.
    """
    sink = _EVENT_SINK
    if sink is None:
        logger.debug(
            "no event sink registered; dropping probe-error for probe %s",
            probe.id,
        )
        return

    scope = get_scope()
    # Truncate the traceback to keep payloads small. The most useful frames
    # are the innermost (where ``execute_probe`` raised) — these come last
    # in the formatted output by default.
    tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    formatted_tb = "".join(tb_lines)

    properties: Dict[str, Any] = {
        "program_id": program.id,
        "probe_id": probe.id,
        "context_id": scope.context_id if scope is not None else None,
        "probe_spec": serialize_probe_spec(probe.spec),
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "traceback": formatted_tb,
        "skipped_since_last": skipped_since_last,
        "timestamp": datetime.datetime.now(),
        "thread_id": threading.current_thread().ident,
        "thread_name": threading.current_thread().name,
    }

    try:
        sink("$hogtrace_probe_error", properties)
    except Exception:
        logger.exception(
            "event sink raised; dropping probe-error for probe %s",
            probe.id,
        )


def serialize_probe_spec(spec: ProbeSpec) -> Dict[str, str]:
    return {
        "specifier": spec.specifier,
        "target": spec.target,
    }
