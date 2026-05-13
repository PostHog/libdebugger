"""
Hogtrace manager: free functions for program install / uninstall / reconcile,
plus the ``HogTraceManager`` class that polls PostHog for the active program
list and routes updates into those free functions.

The registry of installed programs lives in ``libdebugger.instrumentation``
(see ``_instr_module._INSTALLED_PROGRAMS``, ``_PROBE_INDEX``, ``_LOCK``). Manager-side
operations grab the lock, mutate ``_instr_module._INSTALLED_PROGRAMS`` in place, and rebuild
``_PROBE_INDEX`` via atomic-rebind. Wrappers in ``instrumentation.py`` read
``_PROBE_INDEX`` from the hot path without any locking and see new probes on
the next call.
"""

from __future__ import annotations

import importlib
import logging
from datetime import timedelta
from typing import Callable, Dict, FrozenSet, Optional, Tuple

from hogtrace import Probe, Program
from posthoganalytics import Posthog
from posthoganalytics.poller import Poller

from libdebugger.instrumentation import (
    _LOCK,
    InstrumentationDecorator,
)
from libdebugger import instrumentation as _instr_module


logger = logging.getLogger("libdebugger.manager")


# ---------------------------------------------------------------------------
# Function resolution
# ---------------------------------------------------------------------------


def resolve_target(specifier: str) -> Optional[Callable]:
    """Resolve a dotted-name probe specifier to a callable, or ``None``.

    Strategy:
      1. Try ``importlib.import_module(specifier)``. If that succeeds the
         specifier names a module, which is not a callable target — return
         ``None``.
      2. Walk shorter prefixes downward. For each prefix that imports as a
         module, ``getattr`` through the remaining attribute components.
      3. Return the first callable found.
      4. Return ``None`` if nothing resolves; callers log and skip.

    Handles ``module.function`` and ``module.Class.method``. Does NOT
    handle wildcards, instance attributes, lambdas, closures, or
    runtime-generated callables. See the spec's "Known limitations".
    """
    parts = specifier.split(".")
    if not parts or not all(parts):
        return None

    # Case 1: specifier names a module verbatim.
    try:
        importlib.import_module(specifier)
        # It is a module; modules aren't callable targets here.
        return None
    except ImportError:
        pass
    except Exception:
        # Importing user code can fail in many ways (SyntaxError on a
        # broken module, RuntimeError on side-effecting imports, etc.).
        # Treat any non-ImportError the same way ImportError is treated:
        # log via the caller and move on.
        logger.debug("import failed while resolving %s", specifier, exc_info=True)

    # Case 2: walk prefixes downward from longest-1 to length-1.
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
            # Found the attribute path but the terminal isn't callable.
            # Shorter prefixes can't reach a different terminal, so stop.
            return None
        return obj

    return None


# ---------------------------------------------------------------------------
# Probe-index rebuild
# ---------------------------------------------------------------------------


def _slot_ids(
    slot: Tuple[Tuple[Program, Probe], ...],
) -> FrozenSet[Tuple[str, str]]:
    """Return the identity set of ``(program.id, probe.id)`` pairs in a slot.

    Probe and Program objects from hogtrace don't implement ``__eq__`` —
    two wrappers around the same underlying probe are neither identity-
    nor value-equal. We compare slots by their stable string identifiers
    instead. ``probe.id`` is unique within a Program; ``(program.id, probe.id)``
    is globally unique across all installed programs.
    """
    return frozenset((program.id, probe.id) for program, probe in slot)


def _rebuild_probe_index() -> None:
    """Rebuild ``instrumentation._PROBE_INDEX`` from ``_instr_module._INSTALLED_PROGRAMS``.

    Called under ``_LOCK``. Walks every probe of every installed program,
    groups by ``(specifier, target)``, and atomic-rebinds the global to the
    new dict. Crucially, when the new slot for a key holds the same
    ``(program.id, probe.id)`` identity-set as the previous slot, we reuse
    the previous tuple object so that the wrapper's hot-path identity-compare
    on line probes stays stable across reconciles that don't actually change
    the probe set.
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
        # Reuse the previous tuple object when the identity-set of probes
        # is unchanged — phase 6 (line-probe drift detection) identity-
        # compares slots in the hot path and we want stable identity when
        # the underlying probe set hasn't changed. Probe/Program lack
        # ``__eq__`` so we can't rely on tuple equality here.
        if existing is not None and _slot_ids(existing) == frozenset(new_ids[key]):
            new_index[key] = existing
        else:
            new_index[key] = new_tuple

    # Atomic-rebind via the module attribute. Readers in
    # ``instrumentation.__call__`` grab a local reference and never iterate
    # the dict, so the rebind is invisible to in-flight calls.
    _instr_module._PROBE_INDEX = new_index


# ---------------------------------------------------------------------------
# Free functions: install / uninstall / update
# ---------------------------------------------------------------------------


def install_program(program: Program) -> None:
    """Register ``program`` and ensure each of its probe targets is wrapped.

    Side effects:
      1. ``_instr_module._INSTALLED_PROGRAMS[program.id] = program`` (under ``_LOCK``).
      2. ``_rebuild_probe_index()`` (under ``_LOCK``).
      3. For each probe in the program, resolve the target callable. If
         resolution fails, log a warning and skip — the registry still
         records the program for the next reconcile to retry. If the
         callable already has a ``__posthog_decorator``, leave it alone;
         the wrapper sees new probes on next call automatically.

    No-op rebuild side: if the target function is already wrapped, we do
    not rebuild ``instrumented_fn``. The wrapper's per-call drift detection
    will rebuild on the next invocation if line probes change.
    """
    with _LOCK:
        _instr_module._INSTALLED_PROGRAMS[program.id] = program
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
        if not hasattr(fn, "__posthog_decorator"):
            try:
                fn.__posthog_decorator = InstrumentationDecorator(
                    fn, qualname=probe.spec.specifier
                )
            except Exception:
                logger.exception(
                    "Failed to wrap %s for probe %s",
                    probe.spec.specifier,
                    probe.id,
                )


def uninstall_program(program_id: str) -> None:
    """Stub: raises NotImplementedError until Phase 3 lands.

    Phase 3 will implement: remove ``program_id`` from ``_INSTALLED_PROGRAMS``,
    rebuild ``_PROBE_INDEX``, and let wrappers self-clean on next call.
    """
    raise NotImplementedError("uninstall_program lands in Phase 3")


def update_program(program: Program) -> None:
    """Stub: raises NotImplementedError until Phase 3 lands.

    Phase 3 will implement the spec definition
    ``uninstall_program(program.id); install_program(program)`` once
    ``uninstall_program`` is real.
    """
    raise NotImplementedError("update_program lands in Phase 3")


# ---------------------------------------------------------------------------
# Top-level manager (polling)
# ---------------------------------------------------------------------------


class HogTraceManager:
    """Polls PostHog for the active program list and reconciles the registry.

    Phase 2 leaves ``_fetch_programs`` mostly stubbed — the HTTP body is wired
    up in Phase 8. ``start`` / ``stop`` and logging use the module-level
    ``logger`` instead of the previously-uncalled ``self.log_info`` /
    ``self.log_warning``.
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

    def stop(self):
        if self.poller:
            self.poller.stop()
        # Phase 8 will wire reconcile-to-empty-state into stop(). Phase 2 simply
        # halts the poller; wrapper self-uninstall remains driven by the empty
        # registry check.
        self.enabled = False

    def _fetch_programs(self):
        """Fetch active programs and reconcile. HTTP body lands in Phase 8."""
        if not self.client.personal_api_key:
            logger.warning("No personal API key; skipping fetch")
            return
        # Phase 8 implementation: parse ProgramList, diff against
        # _instr_module._INSTALLED_PROGRAMS, route to install_program / uninstall_program /
        # update_program. For Phase 2 we leave the body empty so the poller
        # can still spin without raising.
