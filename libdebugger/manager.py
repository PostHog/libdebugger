"""
Hogtrace manager: free functions for program install / uninstall / reconcile,
plus ``HogTraceManager`` which polls PostHog for the active program list and
routes updates into those free functions.

The dispatch state lives in ``libdebugger.instrumentation``:

* ``_INSTALLED_PROGRAMS`` — source of truth, mutated under ``_LOCK``.
* ``_PROBE_INDEX`` — derived (qualname, kind) -> probes, atomic-rebound.
* ``_CODE_TO_QUALNAME`` — derived code-object routing table, atomic-rebound.
* ``_MONITORED_CODES`` — what ``sys.monitoring`` has enabled, mutated under
  ``_LOCK`` via ``_apply_monitoring`` from inside the rebuild.

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
    _ProbeMarker,
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

    Called under ``_LOCK``. Computes a fresh ``_PROBE_INDEX`` plus
    ``_CODE_TO_QUALNAME``, then asks ``_apply_monitoring`` to diff against
    the currently-enabled set of code objects. Tuple reuse on unchanged
    slots preserves the identity of probe tuples across reconciles where
    the underlying programs didn't change.

    Unresolvable specifiers still appear in ``_PROBE_INDEX`` (so the
    registry-consistency invariants keep holding) but contribute nothing
    to ``_CODE_TO_QUALNAME`` and aren't monitored — they simply never fire.
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

    new_code_routing: Dict[CodeType, str] = {}
    qualname_to_code: Dict[str, CodeType] = {}
    new_codes_to_kinds: Dict[CodeType, Set[str]] = {}
    for qualname, target in new_index:
        code = qualname_to_code.get(qualname)
        if code is None and qualname not in qualname_to_code:
            code = _resolve_code_for_specifier(qualname)
            qualname_to_code[qualname] = code  # cache None for fast re-skip
            if code is not None:
                new_code_routing[code] = qualname
        if code is None:
            continue
        new_codes_to_kinds.setdefault(code, set()).add(target)

    prev_routing = _instr_module._CODE_TO_QUALNAME
    departed_qualnames = {
        prev_routing[code] for code in prev_routing if code not in new_code_routing
    }

    _instr_module._PROBE_INDEX = new_index
    _instr_module._CODE_TO_QUALNAME = new_code_routing

    for qualname in departed_qualnames:
        _clear_marker(qualname)

    _instr_module._apply_monitoring(new_codes_to_kinds)


def _clear_marker(qualname: str) -> None:
    """Drop the ``__posthog_decorator`` sentinel from a no-longer-routed target."""
    fn = resolve_target(qualname)
    if fn is None:
        return
    underlying = fn.__func__ if inspect.ismethod(fn) else fn
    try:
        delattr(underlying, "__posthog_decorator")
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# Free functions: install / uninstall / update
# ---------------------------------------------------------------------------


def install_program(program: Program) -> None:
    """Register ``program`` and route its probes through the dispatch index.

    Side effects:
      1. ``_INSTALLED_PROGRAMS[program.id] = program`` (under ``_LOCK``).
      2. ``_rebuild_probe_index()`` rebuilds the dispatch tables and applies
         the ``sys.monitoring`` event diff (also under ``_LOCK``).
      3. Tags every resolvable target with the ``__posthog_decorator``
         sentinel — pytest-stress and a handful of property tests use the
         attribute to detect "this function is currently routed".

    The first successful install lazily registers the ``sys.monitoring``
    tool id.
    """
    with _LOCK:
        _instr_module._INSTALLED_PROGRAMS[program.id] = program
        _instr_module._ensure_tool_registered()
        _rebuild_probe_index()

    for probe in program.probes:
        fn = resolve_target(probe.spec.specifier)
        if fn is None:
            logger.warning(
                "Probe %s: target %s not resolvable; skipping",
                probe.id,
                probe.spec.specifier,
            )
            continue
        underlying = fn.__func__ if inspect.ismethod(fn) else fn
        if not hasattr(underlying, "__posthog_decorator"):
            try:
                underlying.__posthog_decorator = _ProbeMarker(underlying.__code__)
            except Exception:
                logger.exception(
                    "Failed to attach marker for %s on probe %s",
                    probe.spec.specifier,
                    probe.id,
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
        """Halt the poller, uninstall every program, release the tool id.

        Snapshot ids under ``_LOCK``, release the lock, then iterate
        ``uninstall_program`` — ``_LOCK`` is non-reentrant and each
        uninstall re-acquires it on its own. ``_release_tool`` runs last
        so a subsequent ``start()`` in the same process gets a clean
        ``sys.monitoring`` registration.
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
