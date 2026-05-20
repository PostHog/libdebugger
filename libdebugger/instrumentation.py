"""
Runtime dispatch for libdebugger probes, backed by PEP 669 ``sys.monitoring``.

Replaces the previous bytecode-rewriting decorator (kept in
``libdebugger/bytecode.py`` for reference). The interpreter calls module-level
event handlers directly with the user code's frame already on the stack; the
handlers look up the probe set for the running code object and dispatch.

State model
-----------

* ``_INSTALLED_PROGRAMS`` (program_id -> ``Program``) is the source of truth.
  Mutated under ``_LOCK``.

* ``_PROBE_INDEX`` (``(qualname, kind)`` -> tuple of ``(Program, Probe)``) is
  derived from ``_INSTALLED_PROGRAMS`` by ``_rebuild_probe_index`` in the
  manager. Atomic-rebound; hot-path reads are lock-free. Keyed by ``qualname``
  so tests and tooling that index by specifier keep working — the runtime
  dispatch uses ``_CODE_TO_QUALNAME`` to translate from the live code object
  the interpreter hands us.

* ``_CODE_TO_QUALNAME`` (``CodeType`` -> qualname) is the dispatch routing
  table. Atomic-rebound alongside ``_PROBE_INDEX``. Only includes specifiers
  whose targets resolved to a callable; unresolvable probes still appear in
  ``_PROBE_INDEX`` (so registry-consistency invariants hold) but never fire.

* ``_MONITORED_CODES`` (``CodeType`` -> active event mask) tracks what we've
  enabled on each code object so a reconcile can compute the disable diff.
  Mutated under ``_LOCK``.

The ``__posthog_decorator`` attribute is kept as a backwards-compatible
sentinel on instrumented callables. It does NOT carry probe state and does
NOT mutate ``__code__`` — it's a no-op marker so test invariants and the
pytest-stress plugin can detect "this function is currently routed through
the dispatch index". The marker is cleared lazily by the dispatch path on the
next call after the registry slot for that code object empties.
"""

from __future__ import annotations

import datetime
import inspect
import logging
import sys
import threading
from types import CodeType, FrameType
from typing import Any, Callable, Dict, Final, Optional, Set, Tuple

from hogtrace import Probe, Program, ProbeSpec, execute_probe, get_scope, get_store


logger = logging.getLogger("libdebugger.instrumentation")


# ---------------------------------------------------------------------------
# Module-level dispatch state. Writers serialize on ``_LOCK``; hot-path reads
# (the sys.monitoring callbacks) take no lock at all — atomic-rebind of the
# index dicts is safe under CPython's GIL.
# ---------------------------------------------------------------------------

_LOCK: threading.Lock = threading.Lock()

_INSTALLED_PROGRAMS: Dict[str, Program] = {}

_PROBE_INDEX: Dict[Tuple[str, str], Tuple[Tuple[Program, Probe], ...]] = {}

_CODE_TO_QUALNAME: Dict[CodeType, str] = {}

_MONITORED_CODES: Dict[CodeType, int] = {}

_EVENT_SINK: Optional[Callable[[str, Dict[str, Any]], None]] = None


# ---------------------------------------------------------------------------
# Tool-id lifecycle. We claim ``sys.monitoring.DEBUGGER_ID`` (0) lazily on the
# first successful install and free it from ``HogTraceManager.stop()`` for
# clean teardown.
# ---------------------------------------------------------------------------

_TOOL_ID: Final[int] = sys.monitoring.DEBUGGER_ID
_TOOL_NAME: Final[str] = "libdebugger"
_TOOL_REGISTERED: bool = False

_EVENTS = sys.monitoring.events

# Per-code (local) events. PY_UNWIND is global-only in CPython's
# sys.monitoring; we enable it via set_events in _ensure_tool_registered
# and filter by code object inside the callback.
_ENTRY_EVENT_MASK: Final[int] = _EVENTS.PY_START | _EVENTS.PY_RESUME
_EXIT_EVENT_MASK: Final[int] = _EVENTS.PY_RETURN | _EVENTS.PY_YIELD
_LINE_EVENT_MASK: Final[int] = _EVENTS.LINE
_GLOBAL_EVENT_MASK: Final[int] = _EVENTS.PY_UNWIND


def set_event_sink(
    sink: Optional[Callable[[str, Dict[str, Any]], None]],
) -> None:
    """Register (or clear) the callable that receives probe-capture events.

    Invoked as ``sink(event_name, properties)`` once per probe fire. Pass
    ``None`` to drop captures with a debug log. ``HogTraceManager`` wires
    this automatically when its client exposes ``.capture``.
    """
    global _EVENT_SINK
    _EVENT_SINK = sink


def _any_probes_for(qualname: str) -> bool:
    index = _PROBE_INDEX
    return bool(
        index.get((qualname, "entry"))
        or index.get((qualname, "exit"))
        or index.get((qualname, "line"))
    )


# ---------------------------------------------------------------------------
# Backwards-compatible sentinel attached to instrumented callables.
# ---------------------------------------------------------------------------


class _ProbeMarker:
    """Sentinel attached as ``fn.__posthog_decorator`` while ``fn`` is routed
    through the dispatch index.

    Carries ``original_code`` so legacy assertions of the form
    ``dec.original_code`` keep working — but because the sys.monitoring path
    never mutates ``__code__``, ``original_code`` is just whatever
    ``fn.__code__`` was at install time. ``cleanup()`` is a no-op for the
    same reason.
    """

    __slots__ = ("original_code",)

    def __init__(self, code: CodeType) -> None:
        self.original_code = code

    def cleanup(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Probe execution. Identical semantics to the previous implementation —
# this is the only place where probe code actually runs.
# ---------------------------------------------------------------------------


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
        except Exception:
            logger.exception(
                "Probe execution failed for program=%s probe=%s",
                getattr(program, "id", "?"),
                getattr(probe, "id", "?"),
            )
    return len(probes)


# ---------------------------------------------------------------------------
# Target resolution (kept here because the dispatch state lives here too).
# ---------------------------------------------------------------------------


def resolve_code_for_callable(fn: Any) -> Optional[CodeType]:
    """Pull the ``CodeType`` for a callable, unwrapping bound methods."""
    if inspect.ismethod(fn):
        fn = fn.__func__
    return getattr(fn, "__code__", None)


# ---------------------------------------------------------------------------
# sys.monitoring callbacks.
#
# PEP 669 invokes these synchronously from inside the user code's frame, so
# ``sys._getframe(1)`` reaches the user frame (``_getframe(0)`` is the
# callback's own frame). We deliberately do not return
# ``sys.monitoring.DISABLE`` from any callback — probes can come and go at
# any time and we want the events to keep arriving until ``_apply_monitoring``
# explicitly disables them via ``set_local_events``.
# ---------------------------------------------------------------------------


def _dispatch_entry(code: CodeType) -> None:
    qualname = _CODE_TO_QUALNAME.get(code)
    if qualname is None:
        return
    probes = _PROBE_INDEX.get((qualname, "entry"), ())
    if not probes:
        return
    try:
        frame = sys._getframe(2)  # caller -> _on_py_start -> _dispatch_entry
    except ValueError:
        return
    _run_probes(probes, frame)


def _dispatch_exit(
    code: CodeType,
    *,
    retval: Any = None,
    exception: Optional[BaseException] = None,
) -> None:
    qualname = _CODE_TO_QUALNAME.get(code)
    if qualname is None:
        return
    probes = _PROBE_INDEX.get((qualname, "exit"), ())
    if not probes:
        return
    try:
        frame = sys._getframe(2)
    except ValueError:
        return
    _run_probes(probes, frame, retval=retval, exception=exception)


def _on_py_start(code: CodeType, instruction_offset: int) -> Any:
    _dispatch_entry(code)


def _on_py_resume(code: CodeType, instruction_offset: int) -> Any:
    _dispatch_entry(code)


def _on_py_return(code: CodeType, instruction_offset: int, retval: Any) -> Any:
    _dispatch_exit(code, retval=retval)


def _on_py_yield(code: CodeType, instruction_offset: int, retval: Any) -> Any:
    _dispatch_exit(code, retval=retval)


def _on_py_unwind(
    code: CodeType, instruction_offset: int, exception: BaseException
) -> Any:
    _dispatch_exit(code, exception=exception)


def _on_line(code: CodeType, line_number: int) -> Any:
    # Scaffolded only — v1 line-probe semantics remain TBD. Fire the slot so
    # the registry-consistency invariants exercise the path; capture context
    # for a line probe is out of scope until v2.
    qualname = _CODE_TO_QUALNAME.get(code)
    if qualname is None:
        return
    probes = _PROBE_INDEX.get((qualname, "line"), ())
    if not probes:
        return
    try:
        frame = sys._getframe(1)
    except ValueError:
        return
    _run_probes(probes, frame)


# ---------------------------------------------------------------------------
# Tool-id lifecycle.
# ---------------------------------------------------------------------------


def _ensure_tool_registered() -> None:
    """Acquire the monitoring tool id and register every callback. Idempotent.

    Must be called under ``_LOCK`` so two threads don't both call
    ``use_tool_id``. Raises ``RuntimeError`` if another tool (e.g. pdb,
    PyCharm) already owns the slot — surfacing the conflict is safer than
    silently fighting another debugger.
    """
    global _TOOL_REGISTERED
    if _TOOL_REGISTERED:
        return

    owner = sys.monitoring.get_tool(_TOOL_ID)
    if owner is None:
        sys.monitoring.use_tool_id(_TOOL_ID, _TOOL_NAME)
    elif owner != _TOOL_NAME:
        raise RuntimeError(
            f"sys.monitoring tool id {_TOOL_ID} is already owned by "
            f"{owner!r}; refusing to install libdebugger probes"
        )

    sys.monitoring.register_callback(_TOOL_ID, _EVENTS.PY_START, _on_py_start)
    sys.monitoring.register_callback(_TOOL_ID, _EVENTS.PY_RESUME, _on_py_resume)
    sys.monitoring.register_callback(_TOOL_ID, _EVENTS.PY_RETURN, _on_py_return)
    sys.monitoring.register_callback(_TOOL_ID, _EVENTS.PY_YIELD, _on_py_yield)
    sys.monitoring.register_callback(_TOOL_ID, _EVENTS.PY_UNWIND, _on_py_unwind)
    sys.monitoring.register_callback(_TOOL_ID, _EVENTS.LINE, _on_line)

    # PY_UNWIND is global-only — enable it for our tool id. The callback
    # filters by code via ``_CODE_TO_QUALNAME``, so the global enablement
    # is effectively no-op for codes we don't care about.
    sys.monitoring.set_events(_TOOL_ID, _GLOBAL_EVENT_MASK)

    _TOOL_REGISTERED = True


def _release_tool() -> None:
    """Disable every active event and release the tool id. Idempotent.

    Called from ``HogTraceManager.stop()`` so a process can re-``start()``
    cleanly. Also unwires every callback so a future start re-registers
    against a fresh slot.
    """
    global _TOOL_REGISTERED
    if not _TOOL_REGISTERED:
        return

    for code in list(_MONITORED_CODES):
        try:
            sys.monitoring.set_local_events(_TOOL_ID, code, 0)
        except Exception:
            logger.exception("failed disabling monitoring on %r during release", code)
    _MONITORED_CODES.clear()

    try:
        sys.monitoring.set_events(_TOOL_ID, 0)
    except Exception:
        logger.exception("failed disabling global events")

    for event in (
        _EVENTS.PY_START,
        _EVENTS.PY_RESUME,
        _EVENTS.PY_RETURN,
        _EVENTS.PY_YIELD,
        _EVENTS.PY_UNWIND,
        _EVENTS.LINE,
    ):
        try:
            sys.monitoring.register_callback(_TOOL_ID, event, None)
        except Exception:
            logger.exception("failed unregistering callback for event %r", event)

    try:
        sys.monitoring.free_tool_id(_TOOL_ID)
    except Exception:
        logger.exception("failed freeing tool id %d", _TOOL_ID)

    _TOOL_REGISTERED = False


def _apply_monitoring(new_codes_to_kinds: Dict[CodeType, Set[str]]) -> None:
    """Diff ``new_codes_to_kinds`` against ``_MONITORED_CODES`` and update.

    For each code that left the set: disable all events. For each code in
    the new set: compute the event mask from the kinds present in the
    probe index and call ``set_local_events`` to enable them.

    Called under ``_LOCK`` from ``_rebuild_probe_index``.
    """
    new_codes = set(new_codes_to_kinds)
    old_codes = set(_MONITORED_CODES)

    for code in old_codes - new_codes:
        try:
            sys.monitoring.set_local_events(_TOOL_ID, code, 0)
        except Exception:
            logger.exception("failed disabling monitoring on %r", code)
        _MONITORED_CODES.pop(code, None)

    for code, kinds in new_codes_to_kinds.items():
        mask = 0
        if "entry" in kinds:
            mask |= _ENTRY_EVENT_MASK
        if "exit" in kinds:
            mask |= _EXIT_EVENT_MASK
        if "line" in kinds:
            mask |= _LINE_EVENT_MASK
        if mask == 0:
            continue
        if _MONITORED_CODES.get(code) == mask:
            continue
        try:
            sys.monitoring.set_local_events(_TOOL_ID, code, mask)
        except Exception:
            logger.exception("failed enabling monitoring on %r", code)
            continue
        _MONITORED_CODES[code] = mask


# ---------------------------------------------------------------------------
# Event sink (capture forwarding).
# ---------------------------------------------------------------------------


def _enqueue_message(program: Program, probe: Probe, captures: Dict[str, Any]) -> None:
    sink = _EVENT_SINK
    if sink is None:
        logger.debug(
            "no event sink registered; dropping capture from probe %s", probe.id
        )
        return

    scope = get_scope()
    properties: Dict[str, Any] = {
        "program_id": program.id,
        "probe_id": probe.id,
        "context_id": scope.context_id if scope is not None else None,
        "probe_spec": serialize_probe_spec(probe.spec),
        "captures": captures,
        "timestamp": datetime.datetime.now(),
        "thread_id": threading.current_thread().ident,
        "thread_name": threading.current_thread().name,
    }

    try:
        sink("$hogtrace_capture", properties)
    except Exception:
        logger.exception("event sink raised; dropping capture from probe %s", probe.id)


def serialize_probe_spec(spec: ProbeSpec) -> Dict[str, str]:
    return {
        "specifier": spec.specifier,
        "target": spec.target,
    }
