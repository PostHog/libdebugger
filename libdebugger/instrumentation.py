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
  manager. Atomic-rebound; kept for tests / tooling that key by specifier.
  Two specifiers can resolve to the same code object (aliases, inherited
  methods); this index keeps them separate.

* ``_CODE_PROBE_INDEX`` (``(CodeType, kind)`` -> tuple of ``(Program, Probe)``)
  is the actual dispatch table. Atomic-rebound alongside ``_PROBE_INDEX``.
  Aggregates probes from every specifier that resolves to a given code
  object so an aliased function fires all of its probes. The callbacks
  read this directly from the live code object the interpreter hands us.

* ``_MONITORED_CODES`` (``CodeType`` -> active event mask) tracks what we've
  enabled on each code object so a reconcile can compute the disable diff,
  AND is the source of truth for "is this function currently instrumented?"
  via ``is_instrumented(fn)``. Mutated under ``_LOCK``.

There is no per-function sentinel attribute — callers that need to detect
"is this function currently routed?" call ``is_instrumented(fn)`` which
checks ``_MONITORED_CODES`` directly. Earlier revisions kept a
``__posthog_decorator`` marker attribute purely so existing tests and
tooling could ``hasattr`` for it; that turned out to be code created just
to satisfy assertions and has been removed.
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

_CODE_PROBE_INDEX: Dict[Tuple[CodeType, str], Tuple[Tuple[Program, Probe], ...]] = {}

_MONITORED_CODES: Dict[CodeType, int] = {}

_EVENT_SINK: Optional[Callable[[str, Dict[str, Any]], None]] = None


# ---------------------------------------------------------------------------
# Tool-id lifecycle. PEP 669 reserves ids 0 (DEBUGGER_ID), 1 (COVERAGE_ID),
# 2 (PROFILER_ID), 5 (OPTIMIZER_ID); slots 3 and 4 are for ad-hoc tools.
# We prefer the ad-hoc slots first so we stay out of the way of pdb /
# debugpy / coverage by default, then fall through every reserved slot
# before giving up — refusing to install when even one slot might still
# be free would be worse than borrowing a reserved slot that isn't in
# use. Once acquired the slot is held for the lifetime of the process —
# ``_release_tool`` disables events but keeps ownership so a start/stop
# cycle doesn't churn callback registration or open a window where
# another tool can grab our slot.
# ---------------------------------------------------------------------------

_TOOL_NAME: Final[str] = "libdebugger"
_TOOL_CANDIDATES: Final[Tuple[int, ...]] = (
    3,
    4,
    sys.monitoring.DEBUGGER_ID,
    sys.monitoring.COVERAGE_ID,
    sys.monitoring.PROFILER_ID,
    sys.monitoring.OPTIMIZER_ID,
)
_TOOL_ID: int = -1  # populated by _ensure_tool_registered
_TOOL_REGISTERED: bool = False
_CALLBACKS_REGISTERED: bool = False

_EVENTS = sys.monitoring.events

# Per-code (local) events. PY_UNWIND is global-only in CPython's
# sys.monitoring; we enable it via set_events in _ensure_tool_registered
# and filter by code object inside the callback.
_ENTRY_EVENT_MASK: Final[int] = _EVENTS.PY_START | _EVENTS.PY_RESUME
_EXIT_EVENT_MASK: Final[int] = _EVENTS.PY_RETURN | _EVENTS.PY_YIELD
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
# Public helper: detect whether a callable is currently routed.
# ---------------------------------------------------------------------------


def is_instrumented(fn: Any) -> bool:
    """True iff ``fn`` is currently routed through the dispatch index.

    Reads ``_MONITORED_CODES`` lock-free. Handles bound methods by
    walking down to ``__func__``. Useful for the pytest-stress plugin
    and test assertions; the dispatch path itself uses the code object
    directly.
    """
    if inspect.ismethod(fn):
        fn = fn.__func__
    code = getattr(fn, "__code__", None)
    return code is not None and code in _MONITORED_CODES


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
    probes = _CODE_PROBE_INDEX.get((code, "entry"), ())
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
    probes = _CODE_PROBE_INDEX.get((code, "exit"), ())
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
    # Line probes are not implemented in v1 — the install path skips them
    # with a warning and the LINE event mask never gets enabled. This
    # callback exists only because ``register_callback`` requires one if
    # the event is ever toggled; nothing should reach it.
    return


# ---------------------------------------------------------------------------
# Tool-id lifecycle.
# ---------------------------------------------------------------------------


def _ensure_tool_registered() -> None:
    """Acquire a ``sys.monitoring`` slot and register every callback.

    Must be called under ``_LOCK``. Idempotent across repeated calls and
    across stop()/start() cycles — the slot is held for the lifetime of
    the process, so subsequent calls just flip events back on.

    Slot preference order: the ad-hoc slots (3, 4) first, then the
    reserved slots (``DEBUGGER_ID``, ``COVERAGE_ID``, ``PROFILER_ID``,
    ``OPTIMIZER_ID``) — see ``_TOOL_CANDIDATES`` for the canonical list.
    Raises ``RuntimeError`` only when every candidate slot is owned by
    some other tool; at that point we'd be fighting pdb / debugpy /
    coverage / profiler over a slot and would rather fail loudly.
    """
    global _TOOL_ID, _TOOL_REGISTERED, _CALLBACKS_REGISTERED
    if _TOOL_REGISTERED:
        return

    if _TOOL_ID == -1:
        chosen: Optional[int] = None
        for candidate in _TOOL_CANDIDATES:
            owner = sys.monitoring.get_tool(candidate)
            if owner is None:
                sys.monitoring.use_tool_id(candidate, _TOOL_NAME)
                chosen = candidate
                break
            if owner == _TOOL_NAME:
                chosen = candidate
                break
        if chosen is None:
            owners = [
                (cand, sys.monitoring.get_tool(cand)) for cand in _TOOL_CANDIDATES
            ]
            raise RuntimeError(
                "every candidate sys.monitoring tool slot is taken; "
                f"refusing to install libdebugger probes (owners={owners})"
            )
        _TOOL_ID = chosen

    if not _CALLBACKS_REGISTERED:
        sys.monitoring.register_callback(_TOOL_ID, _EVENTS.PY_START, _on_py_start)
        sys.monitoring.register_callback(_TOOL_ID, _EVENTS.PY_RESUME, _on_py_resume)
        sys.monitoring.register_callback(_TOOL_ID, _EVENTS.PY_RETURN, _on_py_return)
        sys.monitoring.register_callback(_TOOL_ID, _EVENTS.PY_YIELD, _on_py_yield)
        sys.monitoring.register_callback(_TOOL_ID, _EVENTS.PY_UNWIND, _on_py_unwind)
        sys.monitoring.register_callback(_TOOL_ID, _EVENTS.LINE, _on_line)
        _CALLBACKS_REGISTERED = True

    # PY_UNWIND is global-only — enable it for our tool id. The dispatch
    # callback short-circuits on codes that aren't in ``_CODE_PROBE_INDEX``,
    # so a permanently-global PY_UNWIND is effectively no-op when nothing
    # is monitored.
    sys.monitoring.set_events(_TOOL_ID, _GLOBAL_EVENT_MASK)

    _TOOL_REGISTERED = True


def _release_tool() -> None:
    """Disable every active event but retain ownership of the tool slot.

    Holding the slot for process lifetime avoids two problems: callback
    re-registration churn across start/stop cycles, and a window where
    another tool (pdb, coverage) can grab our slot between stop() and
    the next install_program(). Callers that genuinely want to free the
    slot — typically only the test harness — must call
    ``sys.monitoring.free_tool_id(_TOOL_ID)`` themselves.
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

    _TOOL_REGISTERED = False


def _apply_monitoring(new_codes_to_kinds: Dict[CodeType, Set[str]]) -> None:
    """Diff ``new_codes_to_kinds`` against ``_MONITORED_CODES`` and update.

    For each code that left the set: disable all events. For each code in
    the new set: compute the event mask from the kinds present in the
    probe index and call ``set_local_events`` to enable them. Line probes
    are intentionally NOT supported by this path — the manager skips them
    in the rebuild, so ``new_codes_to_kinds`` only carries ``entry`` /
    ``exit`` kinds.

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
