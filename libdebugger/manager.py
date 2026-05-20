"""
Hogtrace manager: free functions for program install / uninstall / reconcile,
plus ``HogTraceManager`` which polls PostHog for the active program list and
routes updates into those free functions.

The dispatch state lives in ``libdebugger.instrumentation``:

* ``_INSTALLED_PROGRAMS`` — source of truth, mutated under ``_LOCK``.
* ``_PROBE_INDEX`` — derived (qualname, kind) -> probes, atomic-rebound.
  Kept for tests / tooling that look up by specifier.
* ``_CODE_PROBE_INDEX`` — derived (code, kind) -> probes; aggregates every
  specifier that resolves to the same code. The dispatch reads this.
* ``_MONITORED_CODES`` — what ``sys.monitoring`` has enabled, mutated under
  ``_LOCK`` via ``_apply_monitoring`` from inside the rebuild. Also the
  source of truth for ``is_instrumented(fn)``.

The bytecode-rewriting decorator that backed earlier versions is gone;
``libdebugger/bytecode.py`` is retained for reference but unused at runtime.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from datetime import timedelta
from typing import Any, Callable, Dict, FrozenSet, Optional, Set, Tuple

import requests
from hogtrace import Probe, Program, ProgramList
from posthoganalytics import Posthog
from posthoganalytics.poller import Poller
from types import CodeType

from libdebugger import instrumentation as _instr_module
from libdebugger.instrumentation import (
    _LOCK,
    set_event_sink,
)


logger = logging.getLogger("libdebugger.manager")


# ---------------------------------------------------------------------------
# Function resolution
# ---------------------------------------------------------------------------


def resolve_target(specifier: str) -> Optional[Callable]:
    """Resolve a dotted-name probe specifier to a callable, or ``None``.

    Walks module-attribute paths only — wildcards, closures, lambdas,
    callables stored in containers, descriptor magic, and monkey-patched
    callables are all out of scope.
    """
    parts = specifier.split(".")
    if not parts or not all(parts):
        return None

    try:
        importlib.import_module(specifier)
        return None
    except ImportError:
        pass
    except Exception:
        logger.debug("import failed while resolving %s", specifier, exc_info=True)

    for split in range(len(parts) - 1, 0, -1):
        mod_path = ".".join(parts[:split])
        attr_path = parts[split:]
        try:
            obj = importlib.import_module(mod_path)
        except ImportError:
            continue
        except Exception:
            logger.debug("import failed for prefix %s", mod_path, exc_info=True)
            continue

        ok = True
        for attr in attr_path:
            if not hasattr(obj, attr):
                ok = False
                break
            obj = getattr(obj, attr)

        if not ok:
            continue
        if not callable(obj):
            return None
        return obj

    return None


# ---------------------------------------------------------------------------
# Probe-index rebuild
# ---------------------------------------------------------------------------


def _slot_ids(
    slot: Tuple[Tuple[Program, Probe], ...],
) -> FrozenSet[Tuple[str, str]]:
    """Identity set of ``(program.id, probe.id)`` pairs in a slot.

    Hogtrace ``Program`` / ``Probe`` lack ``__eq__``, so we compare on the
    stable string identifiers instead.
    """
    return frozenset((program.id, probe.id) for program, probe in slot)


def _resolve_code_for_specifier(specifier: str) -> Optional[CodeType]:
    fn = resolve_target(specifier)
    if fn is None:
        return None
    if inspect.ismethod(fn):
        fn = fn.__func__
    return getattr(fn, "__code__", None)


def _rebuild_probe_index() -> None:
    """Rebuild the derived dispatch state from ``_INSTALLED_PROGRAMS``.

    Called under ``_LOCK``. Produces two derived structures from a single
    pass over the installed programs:

      * ``_PROBE_INDEX`` keyed by ``(qualname, kind)`` for tests / tooling.
      * ``_CODE_PROBE_INDEX`` keyed by ``(code, kind)`` for dispatch —
        aggregates every specifier that resolves to the same code object so
        an aliased function fires every probe pointing at it.

    Line probes appear in ``_PROBE_INDEX`` (so registry-consistency
    invariants keep holding) but are filtered out of the dispatch table
    and monitoring mask — they would over-capture today, see Future-work
    in the design doc. A warning is logged once per probe id.
    """
    prev = _instr_module._PROBE_INDEX

    new_raw: Dict[Tuple[str, str], list] = {}
    new_ids: Dict[Tuple[str, str], set] = {}
    for program in _instr_module._INSTALLED_PROGRAMS.values():
        for probe in program.probes:
            qualname = probe.spec.specifier
            target = probe.spec.target  # "entry" | "exit" | "line"
            key = (qualname, target)
            new_raw.setdefault(key, []).append((program, probe))
            new_ids.setdefault(key, set()).add((program.id, probe.id))

    new_index: Dict[Tuple[str, str], Tuple[Tuple[Program, Probe], ...]] = {}
    for key, pairs in new_raw.items():
        new_tuple = tuple(pairs)
        existing = prev.get(key)
        if existing is not None and _slot_ids(existing) == frozenset(new_ids[key]):
            new_index[key] = existing
        else:
            new_index[key] = new_tuple

    qualname_to_resolved: Dict[str, Optional[Any]] = {}
    new_code_index: Dict[Tuple[CodeType, str], list] = {}
    new_codes_to_kinds: Dict[CodeType, Set[str]] = {}

    for (qualname, target), pairs in new_index.items():
        if target == "line":
            _maybe_warn_line_probes(pairs)
            continue
        fn = qualname_to_resolved.get(qualname)
        if fn is None and qualname not in qualname_to_resolved:
            fn = resolve_target(qualname)
            qualname_to_resolved[qualname] = fn
        if fn is None:
            continue
        underlying = fn.__func__ if inspect.ismethod(fn) else fn
        code = getattr(underlying, "__code__", None)
        if code is None:
            continue
        new_code_index.setdefault((code, target), []).extend(pairs)
        new_codes_to_kinds.setdefault(code, set()).add(target)

    final_code_index: Dict[Tuple[CodeType, str], Tuple[Tuple[Program, Probe], ...]] = {
        k: tuple(v) for k, v in new_code_index.items()
    }

    _instr_module._PROBE_INDEX = new_index
    _instr_module._CODE_PROBE_INDEX = final_code_index

    _instr_module._apply_monitoring(new_codes_to_kinds)


_LINE_PROBE_WARNED: Set[Tuple[str, str]] = set()


def _maybe_warn_line_probes(
    pairs: Tuple[Tuple[Program, Probe], ...],
) -> None:
    """Log a one-shot warning per (program_id, probe_id) for a line probe."""
    for program, probe in pairs:
        key = (program.id, probe.id)
        if key in _LINE_PROBE_WARNED:
            continue
        _LINE_PROBE_WARNED.add(key)
        logger.warning(
            "line probe %s on program %s is not supported in this version; "
            "skipping (entry/exit probes still install normally)",
            probe.id,
            program.id,
        )


# ---------------------------------------------------------------------------
# Free functions: install / uninstall / update
# ---------------------------------------------------------------------------


def install_program(program: Program) -> None:
    """Register ``program`` and route its probes through the dispatch index.

    Side effects (all under ``_LOCK``):
      1. ``_ensure_tool_registered`` runs FIRST. If acquisition fails (every
         candidate ``sys.monitoring`` slot is already owned by another tool),
         the registry stays untouched — otherwise a future reconcile sees
         the program as ``current`` and never retries.
      2. ``_INSTALLED_PROGRAMS[program.id] = program``.
      3. ``_rebuild_probe_index`` rebuilds the qualname / code dispatch
         tables and applies the ``sys.monitoring`` event-mask diff.

    Per-probe target resolution happens inside the rebuild's critical
    section; ``is_instrumented(fn)`` is the way for callers to check
    whether a particular function is currently routed.
    """
    with _LOCK:
        _instr_module._ensure_tool_registered()
        _instr_module._INSTALLED_PROGRAMS[program.id] = program
        _rebuild_probe_index()

    # Log per-probe resolution failures for visibility. The rebuild has
    # already done the actual resolution work and skipped unresolvable
    # specifiers; this loop is purely diagnostic.
    for probe in program.probes:
        if probe.spec.target == "line":
            continue
        if resolve_target(probe.spec.specifier) is None:
            logger.warning(
                "Probe %s: target %s not resolvable; skipping",
                probe.id,
                probe.spec.specifier,
            )


def uninstall_program(program_id: str) -> None:
    """Remove ``program_id`` and rebuild the dispatch tables.

    Silent no-op on an unknown id so reconcile-diff loops can issue
    uninstall unconditionally. Sentinel markers are cleared lazily inside
    the dispatch callbacks on the next call after the registry slot empties.
    """
    with _LOCK:
        _instr_module._INSTALLED_PROGRAMS.pop(program_id, None)
        _rebuild_probe_index()


def update_program(program: Program) -> None:
    """Replace any existing install of ``program.id`` with ``program``.

    Defined as uninstall + install so each step acquires ``_LOCK``
    independently — the lock is non-reentrant and ``install_program``
    re-acquires it on its own.
    """
    uninstall_program(program.id)
    install_program(program)


# ---------------------------------------------------------------------------
# Top-level manager (polling)
# ---------------------------------------------------------------------------


class HogTraceManager:
    """Polls PostHog for the active program list and reconciles the registry.

    ``start`` spawns a ``posthoganalytics.poller.Poller`` that periodically
    calls ``_fetch_programs``. ``_fetch_programs`` pulls the active
    ``ProgramList`` from the control plane, diffs against the registry,
    and routes per-program changes through ``install_program`` /
    ``uninstall_program`` / ``update_program``. Per-program failures are
    logged and skipped so one bad program can't kill a cycle; transport
    and parse errors are caught around the whole fetch.
    """

    client: Posthog
    poll_interval: int

    enabled: bool
    poller: Optional[Poller]

    def __init__(self, client: Posthog, poll_interval: int = 30):
        self.client = client
        self.poll_interval = poll_interval
        self.enabled = False
        self.poller = None

        client_capture = getattr(client, "capture", None)
        if callable(client_capture):

            def _sink(event_name: str, properties: Dict[str, Any]) -> None:
                client_capture(event=event_name, properties=properties)

            set_event_sink(_sink)
        else:
            logger.warning(
                "HogTraceManager client has no callable .capture; "
                "probe events will be dropped until libdebugger.set_event_sink "
                "is called",
            )

    def start(self):
        if self.enabled:
            logger.info("HogTraceManager already started")
            return

        if self.client.personal_api_key:
            self.poller = Poller(
                interval=timedelta(seconds=self.poll_interval),
                execute=self._fetch_programs,
            )
            self.poller.start()
            self.enabled = True
        else:
            logger.warning(
                "HogTraceManager.start called with no personal_api_key; "
                "no poller spawned"
            )

    def stop(self):
        """Halt the poller, uninstall every program, disable events.

        Snapshot ids under ``_LOCK``, release the lock, then iterate
        ``uninstall_program`` — ``_LOCK`` is non-reentrant and each
        uninstall re-acquires it on its own. ``_release_tool`` runs last;
        it disables monitoring events but DOES NOT free the slot, so a
        subsequent ``start()`` reuses the same slot without callback churn.
        """
        if self.poller:
            self.poller.stop()
        with _LOCK:
            pids = list(_instr_module._INSTALLED_PROGRAMS)
        for pid in pids:
            try:
                uninstall_program(pid)
            except Exception:
                logger.exception("Failed to uninstall program %s during stop", pid)
        with _LOCK:
            _instr_module._release_tool()
        self.enabled = False

    def _fetch_programs(self):
        """Fetch the active ``ProgramList`` and reconcile against the registry."""
        if not self.client.personal_api_key:
            logger.warning("No personal API key; skipping fetch")
            return
        try:
            host = (self.client.host or "").rstrip("/")
            url = host + "/api/projects/@current/live_debugger/programs/active"
            resp = requests.get(
                url,
                headers={
                    "Authorization": "Bearer " + self.client.personal_api_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            incoming = {p.id: p for p in ProgramList.from_bytes(resp.content).programs}
        except Exception:
            logger.exception("Failed to fetch programs")
            return

        current_ids = set(_instr_module._INSTALLED_PROGRAMS)
        incoming_ids = set(incoming)

        for pid in current_ids - incoming_ids:
            try:
                uninstall_program(pid)
            except Exception:
                logger.exception("Failed to uninstall program %s", pid)
        for pid in incoming_ids - current_ids:
            try:
                install_program(incoming[pid])
            except Exception:
                logger.exception("Failed to install program %s", pid)
        for pid in current_ids & incoming_ids:
            try:
                if _instr_module._INSTALLED_PROGRAMS[pid].hash != incoming[pid].hash:
                    update_program(incoming[pid])
            except Exception:
                logger.exception("Failed to update program %s", pid)
