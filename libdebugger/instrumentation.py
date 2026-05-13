import datetime
import logging
import threading
import sys
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
        except Exception:
            logger.exception(
                "Probe execution failed for program=%s probe=%s",
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
            # TODO(phase-2): wrap this cleanup block in the proper lock
            # discipline once we reason through reentrancy with live
            # probes. Phase 1 is single-threaded test setups so the
            # existing ``self._lock`` is sufficient.
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
    from posthoganalytics import capture

    scope = get_scope()
    assert scope is not None

    properties: Dict[str, Any] = {
        "program_id": program.id,
        "probe_id": probe.id,
        "context_id": scope.context_id,
        "probe_spec": serialize_probe_spec(probe.spec),
        "captures": captures,
        # "$stack_trace": stacktrace,
        "timestamp": datetime.datetime.now(),
        "thread_id": threading.current_thread().ident,
        "thread_name": threading.current_thread().name,
    }

    capture(
        "$hogtrace_capture",
        properties=properties,
        timestamp=properties["timestamp"],
    )


def serialize_probe_spec(spec: ProbeSpec) -> Dict[str, str]:
    return {
        "specifier": spec.specifier,
        "target": spec.target,
    }
